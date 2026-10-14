"""本地端到端自测：像 Claude Code 一样通过 MCP 调用正在运行的 Ombre Brain。

验证三件事：
1. 文字记忆：hold 保存 → breath_search 搜得到；
2. 照片：hold(media=[临时路径]) 存入 → 原图落在 <数据目录>/_media/；
3. media_read：索引里查得到 → 按 ref 读回的字节与原图 SHA-256 完全一致。

写入的都是 test_data=True 的测试桶，结束时用 trace(hard_delete=True) 删除，并清掉
对应的 _media/<bucket_id>/；加 --keep 则保留，方便去 Dashboard / Obsidian 里看。

用法（由 deploy/mac_local.sh smoke 调用）：
    python deploy/local_smoke_test.py --url http://127.0.0.1:18001/mcp \
        --token-file ~/OmbreBrain/.mcp_token --data-dir ~/OmbreBrain [照片路径] [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

_VIEWABLE = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def _sample_png() -> bytes:
    raw = b"".join(b"\x00" + bytes((30, 120, 200)) * 64 for _ in range(48))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 48, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _viewable(data: bytes) -> bool:
    return data.startswith(_VIEWABLE) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


class Check:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, ok: bool, label: str, detail: str = "") -> bool:
        print(f"{'✓' if ok else '✗'} {label}" + (f"\n    {detail}" if detail and not ok else ""))
        if not ok:
            self.failed += 1
        return ok


async def run(args) -> int:
    token = Path(args.token_file).expanduser().read_text().strip()
    data_dir = Path(args.data_dir).expanduser()
    marker = f"自测{uuid.uuid4().hex[:8]}"
    check = Check()
    created: list[str] = []

    if args.photo:
        original = Path(args.photo).expanduser().read_bytes()
        photo_name = Path(args.photo).name
        if not _viewable(original):
            print(f"✗ {args.photo} 不是 JPEG/PNG/GIF/WebP。HEIC 可先转换：\n"
                  f"    sips -s format jpeg '{args.photo}' --out ~/Desktop/test.jpg")
            return 2
    else:
        original, photo_name = _sample_png(), "sample.png"
    digest = hashlib.sha256(original).hexdigest()

    # 服务端只从临时目录收 path（防止一次调用读走任意文件），所以先复制进 /tmp。
    tmp_root = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()  # nosec B108 - 与服务端白名单一致
    staged = Path(tmp_root) / f"ombre-smoke-{marker}{Path(photo_name).suffix.lower() or '.png'}"
    staged.write_bytes(original)

    try:
        async with streamablehttp_client(args.url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                check({"breath", "breath_search", "hold", "grow", "trace", "media_read"} <= names,
                      f"MCP 已连接，工具 {len(names)} 个（含 media_read）", f"实际：{sorted(names)}")

                # 1. 文字记忆
                res = await s.call_tool("hold", {
                    "content": f"{marker} 今天在 Mac 上把 Ombre Brain 跑起来了，记忆存在本地。",
                    "test_data": True,
                })
                m = re.search(r"→([0-9a-f]{12})", _text(res))
                if check(not res.isError and bool(m), "hold 保存文字记忆", _text(res)):
                    created.append(m.group(1))
                res = await s.call_tool("breath_search", {"query": marker})
                check(not res.isError and marker in _text(res), "breath_search 搜到刚存的记忆", _text(res)[:300])

                # 2. 照片
                res = await s.call_tool("hold", {
                    "content": f"{marker} 照片测试：{photo_name}",
                    "test_data": True,
                    "media": [{"path": str(staged), "title": f"{marker} 测试照片"}],
                })
                m = re.search(r"→([0-9a-f]{12})", _text(res))
                photo_bucket = m.group(1) if m else ""
                if check(not res.isError and bool(m), "hold 带照片保存", _text(res)):
                    created.append(photo_bucket)
                stored = list((data_dir / "_media" / photo_bucket).glob(f"{digest}.*")) if photo_bucket else []
                check(bool(stored), "原图已落在本地数据目录", f"没找到 {data_dir}/_media/{photo_bucket}/{digest[:12]}…")
                if stored:
                    print(f"    {stored[0]}")

                # 3. media_read
                res = await s.call_tool("media_read", {"query": marker})
                ref_match = re.search(r"ref=([0-9a-f]{8,64})", _text(res))
                check(not res.isError and bool(ref_match), "media_read 索引查到这张照片", _text(res)[:300])
                if ref_match:
                    res = await s.call_tool("media_read", {"ref": ref_match.group(1)})
                    images = [c for c in res.content if c.type == "image"]
                    got = base64.b64decode(images[0].data) if images else b""
                    check(not res.isError and hashlib.sha256(got).hexdigest() == digest,
                          f"media_read 读回原图，SHA-256 与原文件一致（{len(got) // 1024} KB）", _text(res)[:300])

                if not args.keep:
                    for bucket_id in created:
                        await s.call_tool("trace", {
                            "bucket_id": bucket_id, "hard_delete": True, "delete_reason": "local smoke test",
                        })
                        media_dir = data_dir / "_media" / bucket_id
                        if media_dir.is_dir():
                            shutil.rmtree(media_dir)
                    print(f"  （已清理 {len(created)} 条测试记忆；加 --keep 可保留）")
                else:
                    print(f"  （已保留测试记忆：{', '.join(created)}）")
    finally:
        staged.unlink(missing_ok=True)

    print("全部通过 ✓" if not check.failed else f"{check.failed} 项失败 ✗")
    return 1 if check.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ombre Brain 本地端到端自测")
    parser.add_argument("photo", nargs="?", help="用来测试的照片（JPEG/PNG/GIF/WebP）；不给就用内置小图")
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--keep", action="store_true", help="不清理测试记忆")
    args = parser.parse_args()
    started = time.monotonic()
    code = asyncio.run(run(args))
    print(f"  用时 {time.monotonic() - started:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
