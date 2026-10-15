"""/api/media/upload 与 /api/media/{bucket_id}/{filename}。

Claude.ai 里模型看得见用户发的照片，却拿不到字节，所以原图由人从 Dashboard
上传。这里钉住：只认模型能看的位图、只挂到已有的非信件记忆、读回只认
frontmatter 登记过的文件，且不会把 SVG/HTML 以同源返回。
"""

import base64
import json
import struct
import zlib

import pytest
from starlette.requests import Request

from web import media as web_media


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


def _png() -> bytes:
    raw = b"\x00\xff\x00\x00\xff\x00\x00"

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _multipart(fields: dict, file_bytes: bytes, filename: str, content_type: str):
    boundary = "ombreboundary"
    parts = []
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/media/upload",
        "headers": [
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
            (b"content-length", str(len(body)).encode()),
        ],
        "query_string": b"",
    }
    return Request(scope, receive)


def _get_request(bucket_id, filename):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    request = Request(scope, receive)
    request.scope["path_params"] = {"bucket_id": bucket_id, "filename": filename}
    return request


@pytest.fixture
def routes(bucket_mgr, monkeypatch):
    monkeypatch.setattr(web_media.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(web_media.sh, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(web_media.sh, "config", {"media_max_bytes": 1024 * 1024}, raising=False)
    mcp = FakeMCP()
    web_media.register(mcp)
    return mcp.routes


@pytest.mark.asyncio
async def test_upload_attaches_photo_and_get_returns_it(routes, bucket_mgr):
    bucket_id = await bucket_mgr.create(content="部署那天", test_data=True)
    data = _png()
    resp = await routes[("POST", "/api/media/upload")](
        _multipart({"bucket_id": bucket_id, "title": "截图"}, data, "a.png", "application/octet-stream")
    )
    assert resp.status_code == 200, resp.body
    item = json.loads(resp.body)["media"][0]
    assert item["type"] == "image/png" and item["title"] == "截图"

    filename = item["path"].rsplit("/", 1)[-1]
    got = await routes[("GET", "/api/media/{bucket_id}/{filename}")](_get_request(bucket_id, filename))
    assert got.status_code == 200
    assert got.body == data
    assert got.headers["content-type"] == "image/png"
    assert "sandbox" in got.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_upload_rejects_non_raster(routes, bucket_mgr):
    bucket_id = await bucket_mgr.create(content="x", test_data=True)
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    resp = await routes[("POST", "/api/media/upload")](
        _multipart({"bucket_id": bucket_id}, svg, "x.svg", "image/svg+xml")
    )
    assert resp.status_code == 415
    assert not (await bucket_mgr.get(bucket_id))["metadata"].get("media")


@pytest.mark.asyncio
async def test_upload_needs_existing_non_letter_bucket(routes, bucket_mgr):
    resp = await routes[("POST", "/api/media/upload")](
        _multipart({"bucket_id": "missing"}, _png(), "a.png", "image/png")
    )
    assert resp.status_code == 404

    letter_id = await bucket_mgr.create(content="信", bucket_type="letter", test_data=True)
    resp = await routes[("POST", "/api/media/upload")](
        _multipart({"bucket_id": letter_id}, _png(), "a.png", "image/png")
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_only_serves_registered_rasters(routes, bucket_mgr):
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    bucket_id = await bucket_mgr.create(
        content="x",
        media=[{"data_base64": base64.b64encode(svg).decode(), "filename": "x.svg"}],
        test_data=True,
    )
    path = (await bucket_mgr.get(bucket_id))["metadata"]["media"][0]["path"]
    get = routes[("GET", "/api/media/{bucket_id}/{filename}")]
    assert (await get(_get_request(bucket_id, path.rsplit("/", 1)[-1]))).status_code == 404
    assert (await get(_get_request(bucket_id, "../../config.yaml"))).status_code == 404
    assert (await get(_get_request(bucket_id, "0" * 64 + ".png"))).status_code == 404
