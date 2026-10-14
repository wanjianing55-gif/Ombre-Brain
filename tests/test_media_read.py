"""media_read：照片索引 + 原图读回。

hold(media=...) 早就能把照片存进 _media/，但读取侧只回 path 字符串——模型看见的
是文件名，不是照片。这里钉住补上的那一半：

- 索引来自桶 frontmatter，能按 query / bucket_id 过滤，信件上的媒体不出现；
- 按 ref 读回的就是存进去的那份字节（SHA-256 一致），不是摘要；
- frontmatter 是可手改的外部输入：被替换的文件、符号链接、越出 media_dir 的
  路径、不是模型能看的格式、超过读回上限，都要明确拒绝而不是照读。
"""

import base64
import hashlib
import os
import struct
import zlib
from unittest.mock import MagicMock

import pytest

from errors import ToolInputError


def _png(rgb=(255, 0, 0)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * 2 for _ in range(2))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def media_env(bucket_mgr, monkeypatch):
    import tools._runtime as rt

    monkeypatch.setattr(rt, "config", {"media_read_max_bytes": 5 * 1024 * 1024})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "logger", MagicMock())
    return bucket_mgr


async def _photo_bucket(mgr, content, data, **item):
    entry = {"data_base64": base64.b64encode(data).decode(), "filename": "p.png"}
    entry.update(item)
    bucket_id = await mgr.create(content=content, media=[entry], test_data=True)
    bucket = await mgr.get(bucket_id)
    return bucket_id, bucket["metadata"]["media"][0]


@pytest.mark.asyncio
async def test_index_lists_and_filters(media_env):
    from tools.media import media_read

    beach, _ = await _photo_bucket(media_env, "那天去了海边", _png(), title="海边", note="日落")
    await _photo_bucket(media_env, "在家做饭", _png((0, 255, 0)), title="晚饭")
    await media_env.create(content="没有照片的记忆", test_data=True)

    text, images = await media_read()
    assert images == []
    assert "共 2 张" in text

    text, _ = await media_read(query="海边 日落")
    assert "共 1 张" in text and beach in text

    text, _ = await media_read(query="不存在的词")
    assert "没有匹配的照片" in text


@pytest.mark.asyncio
async def test_ref_returns_original_bytes(media_env):
    from tools.media import media_read

    data = _png()
    bucket_id, item = await _photo_bucket(media_env, "海边", data)
    for ref in (item["sha256"][:12], item["sha256"], item["path"]):
        text, images = await media_read(ref=ref)
        assert len(images) == 1
        got, mime = images[0]
        assert mime == "image/png"
        assert hashlib.sha256(got).hexdigest() == item["sha256"]
        assert bucket_id in text


@pytest.mark.asyncio
async def test_unknown_ref_is_an_error(media_env):
    from tools.media import media_read

    await _photo_bucket(media_env, "海边", _png())
    with pytest.raises(ToolInputError):
        await media_read(ref="deadbeefdeadbeef")
    with pytest.raises(ToolInputError):
        await media_read(ref="../../config.yaml")


@pytest.mark.asyncio
async def test_tampered_file_is_refused(media_env):
    from tools.media import media_read

    _, item = await _photo_bucket(media_env, "海边", _png())
    target = media_env.media_store.vault_dir / item["path"]
    target.write_bytes(_png((0, 0, 255)))
    with pytest.raises(ToolInputError, match="SHA-256"):
        await media_read(ref=item["sha256"])


@pytest.mark.asyncio
async def test_symlinked_file_is_refused(media_env, tmp_path):
    from tools.media import media_read

    _, item = await _photo_bucket(media_env, "海边", _png())
    target = media_env.media_store.vault_dir / item["path"]
    outside = tmp_path / "outside.png"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    os.symlink(outside, target)
    with pytest.raises(ToolInputError, match="不是普通文件"):
        await media_read(ref=item["sha256"])


@pytest.mark.asyncio
async def test_path_outside_media_dir_is_refused(media_env):
    from tools.media import media_read

    bucket_id, _ = await _photo_bucket(media_env, "海边", _png())
    import frontmatter

    # 手改 frontmatter（Obsidian / 恢复）：把 path 指向 vault 里 _media 之外的文件。
    stray = media_env.media_store.vault_dir / "stray.png"
    stray.write_bytes(_png())
    md_path = (await media_env.get(bucket_id))["path"]
    post = frontmatter.load(md_path)
    post["media"] = [{"path": "stray.png", "sha256": hashlib.sha256(_png()).hexdigest()}]
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(frontmatter.dumps(post))
    media_env.external_change_poll_seconds = 0

    from tools.media import collect_media_index

    assert [e["path"] for e in await collect_media_index()] == ["stray.png"]
    with pytest.raises(ToolInputError, match="不在媒体目录内"):
        await media_read(ref="stray.png")


@pytest.mark.asyncio
async def test_non_image_and_oversize_are_refused(media_env, monkeypatch):
    import tools._runtime as rt
    from tools.media import media_read

    _, doc = await _photo_bucket(media_env, "文档", b"%PDF-1.4 not an image", filename="a.pdf")
    with pytest.raises(ToolInputError, match="JPEG/PNG"):
        await media_read(ref=doc["sha256"])

    _, big = await _photo_bucket(media_env, "大图", _png())
    monkeypatch.setattr(rt, "config", {"media_read_max_bytes": 10})
    with pytest.raises(ToolInputError, match="上限"):
        await media_read(ref=big["sha256"])


@pytest.mark.asyncio
async def test_letter_media_is_not_indexed(media_env):
    from tools.media import collect_media_index

    await _photo_bucket(media_env, "普通记忆", _png())
    letter_id = await media_env.create(content="一封信", bucket_type="letter", test_data=True)
    await media_env.update(
        letter_id,
        media=[{"data_base64": base64.b64encode(_png((1, 2, 3))).decode(), "filename": "l.png"}],
    )
    entries = await collect_media_index()
    assert {e["bucket_id"] for e in entries} and letter_id not in {e["bucket_id"] for e in entries}


def test_sniff_only_accepts_viewable_rasters():
    from tools.media.core import sniff_image_mime

    assert sniff_image_mime(_png()) == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0....") == "image/jpeg"
    assert sniff_image_mime(b"GIF89a....") == "image/gif"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_mime(b"<svg xmlns='http://www.w3.org/2000/svg'/>") == ""
    assert sniff_image_mime(b"\x00\x00\x00\x18ftypheic") == ""


def test_tool_is_off_by_default(monkeypatch, tmp_path):
    from utils import load_config

    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("OMBRE_MEDIA_READ_TOOL", raising=False)
    assert load_config()["media_read_tool"] is False
    monkeypatch.setenv("OMBRE_MEDIA_READ_TOOL", "true")
    assert load_config()["media_read_tool"] is True
    monkeypatch.setenv("OMBRE_MEDIA_READ_TOOL", "不是布尔值")
    assert load_config()["media_read_tool"] is False
