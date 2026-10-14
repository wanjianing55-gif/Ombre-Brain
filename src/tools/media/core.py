"""
========================================
tools/media/core.py — 照片索引与原图读回
========================================

两件事：
1. collect_media_index：扫一遍桶的 frontmatter.media，拼成「照片 → 所在记忆」
   的索引。真源仍是桶文件，这里不建第二份库——索引永远和 Markdown 一致，
   手改 Obsidian、GitHub 恢复之后也不需要重建。
2. media_read：无 ref 时返回索引（可按 query / bucket_id 过滤）；给了 ref
   （sha256、≥8 位前缀或 _media/ 路径）就读出原图字节，校验 SHA-256 后作为
   MCP 图片内容返回——模型看到的是那张照片本身，不是文字摘要。

不做什么（边界）：
- 不写任何东西：不 touch、不强化、不改桶。读照片不是「回忆」那条路径。
- 不缩放、不转码：原图就是原图。超过 media_read_max_bytes 或不是 Claude
  能看的格式（JPEG/PNG/GIF/WebP）时明确拒绝，原文件留在磁盘上。
- 不跟随符号链接，不读 media_dir 之外的任何文件——frontmatter 是可手改的
  外部输入，path 字段不能被当成任意文件读取器。
- 不返回信件桶上的媒体：信件有时间锁，锁的判定不在这里重做一遍。

对外暴露：collect_media_index / media_read
========================================
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from errors import ToolInputError

from .. import _runtime as rt

_DEFAULT_READ_MAX_BYTES = 5 * 1024 * 1024
_MAX_LIST = 50
_SHA_RE = re.compile(r"^[0-9a-f]{8,64}$")

# 按文件头判定格式，不信 frontmatter 里的 type：那是调用方随手写的。
_IMAGE_SIGNATURES: tuple[tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
)


def sniff_image_mime(data: bytes) -> str:
    """返回 Claude 可直接查看的图片 MIME；不是这四种之一时返回空串。"""
    for signature, offset, mime in _IMAGE_SIGNATURES:
        if data[offset:offset + len(signature)] == signature:
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _is_letter(bucket: dict) -> bool:
    meta = bucket.get("metadata") or {}
    return str(meta.get("type") or "").lower() == "letter"


def _haystack(bucket: dict, item: dict) -> str:
    meta = bucket.get("metadata") or {}
    parts = [
        str(meta.get("name") or ""),
        " ".join(str(t) for t in meta.get("tags") or []),
        " ".join(str(d) for d in meta.get("domain") or []),
        str(item.get("title") or ""),
        str(item.get("note") or ""),
        str(bucket.get("content") or ""),
    ]
    return "\n".join(parts).casefold()


async def collect_media_index(
    *,
    query: str = "",
    bucket_id: str = "",
    include_archive: bool = False,
) -> list[dict[str, Any]]:
    """列出所有挂了媒体的记忆，按创建时间倒序。query 是空格分隔的 AND 关键词。"""
    buckets = await rt.bucket_mgr.list_all(include_archive=include_archive)
    terms = [t for t in str(query or "").casefold().split() if t]
    wanted_bucket = str(bucket_id or "").strip()
    entries: list[dict[str, Any]] = []
    for bucket in buckets:
        if _is_letter(bucket):
            continue
        if wanted_bucket and str(bucket.get("id")) != wanted_bucket:
            continue
        meta = bucket.get("metadata") or {}
        for item in meta.get("media") or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            if terms:
                hay = _haystack(bucket, item)
                if not all(term in hay for term in terms):
                    continue
            entries.append({
                "bucket_id": str(bucket.get("id") or ""),
                "bucket_name": str(meta.get("name") or ""),
                "created": str(meta.get("created") or ""),
                "path": str(item.get("path")),
                "sha256": str(item.get("sha256") or ""),
                "size": item.get("size"),
                "type": str(item.get("type") or ""),
                "title": str(item.get("title") or ""),
                "note": str(item.get("note") or ""),
            })
    entries.sort(key=lambda e: e["created"], reverse=True)
    return entries


def _short_ref(entry: dict) -> str:
    sha = entry.get("sha256") or ""
    if sha:
        return sha[:12]
    return Path(entry["path"]).stem[:12]


def _format_index(entries: list[dict], *, total: int, query: str, bucket_id: str) -> str:
    scope = []
    if query:
        scope.append(f"query={query!r}")
    if bucket_id:
        scope.append(f"bucket_id={bucket_id}")
    header = f"=== 照片索引 === 共 {total} 张" + (f"（{', '.join(scope)}）" if scope else "")
    if not entries:
        return header + "\n没有匹配的照片。照片要先经 hold(media=...) / trace(media_append=...) 或 Dashboard 上传挂到记忆上。"
    lines = [header]
    for e in entries:
        label = " / ".join(x for x in (e["title"], e["note"]) if x)
        size = f" {int(e['size']) // 1024}KB" if isinstance(e.get("size"), int) else ""
        lines.append(
            f"🖼️ ref={_short_ref(e)} [bucket_id:{e['bucket_id']}] 《{e['bucket_name']}》"
            f" {e['created'][:10]}{size}" + (f" — {label}" if label else "")
        )
    if total > len(entries):
        lines.append(f"……另有 {total - len(entries)} 张未列出，加 query 或 bucket_id 缩小范围。")
    lines.append("看原图：media_read(ref=\"<ref>\")")
    return "\n".join(lines)


def _match_ref(entries: list[dict], ref: str) -> list[dict]:
    needle = ref.strip()
    lowered = needle.lower()
    if _SHA_RE.fullmatch(lowered):
        hits = [
            e for e in entries
            if (e["sha256"] or Path(e["path"]).stem.lower()).startswith(lowered)
        ]
    else:
        hits = [e for e in entries if e["path"] == needle or e["path"].endswith("/" + needle)]
    # 同一张照片挂在多条记忆上是正常的（同一 sha、不同桶目录）；按 sha 去重只留一份。
    unique: dict[str, dict] = {}
    for e in hits:
        unique.setdefault(e["sha256"] or e["path"], e)
    return list(unique.values())


def _resolve_media_file(path_value: str) -> Path:
    store = rt.bucket_mgr.media_store
    media_dir: Path = store.media_dir
    raw = Path(path_value)
    candidate = raw if raw.is_absolute() else store.vault_dir / raw
    # 先按字面路径拒绝符号链接，再按解析后路径判定落在 media_dir 内。
    try:
        st = os.lstat(candidate)
    except OSError as exc:
        raise ToolInputError(f"照片文件不在磁盘上：{path_value}。可能是数据卷没挂好或文件被手动删了。") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ToolInputError(f"照片路径不是普通文件，已拒绝读取：{path_value}")
    resolved = candidate.resolve()
    if media_dir not in resolved.parents:
        raise ToolInputError(f"照片路径不在媒体目录内，已拒绝读取：{path_value}")
    return resolved


def _read_verified(entry: dict, max_bytes: int) -> tuple[bytes, str]:
    target = _resolve_media_file(entry["path"])
    size = target.stat().st_size
    if size > max_bytes:
        raise ToolInputError(
            f"这张照片 {size // 1024}KB，超过单次读回上限 {max_bytes // 1024}KB（media_read_max_bytes）。"
            "原图仍完整保存在磁盘上，可在 Dashboard 或数据目录里查看。"
        )
    data = target.read_bytes()
    expected = entry.get("sha256") or ""
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise ToolInputError(
            f"照片 {_short_ref(entry)} 的 SHA-256 与记录不符，文件可能已损坏或被替换；未返回内容。"
        )
    mime = sniff_image_mime(data)
    if not mime:
        raise ToolInputError(
            f"{entry['path']} 不是 JPEG/PNG/GIF/WebP，模型无法直接查看（例如 iPhone 的 HEIC 需先转成 JPEG）。"
        )
    return data, mime


async def media_read(
    ref: str = "",
    query: str = "",
    bucket_id: str = "",
    limit: int = 20,
) -> tuple[str, list[tuple[bytes, str]]]:
    """返回 (说明文字, [(原图字节, MIME), ...])；后者只在给了 ref 时非空。"""
    ref = str(ref or "").strip()
    query = str(query or "").strip()
    bucket_id = str(bucket_id or "").strip()
    try:
        limit = int(limit or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, _MAX_LIST))

    if not ref:
        entries = await collect_media_index(query=query, bucket_id=bucket_id)
        return _format_index(entries[:limit], total=len(entries), query=query, bucket_id=bucket_id), []

    # 显式按 ref 读原图时也看归档区：照片是证据，不该因为记忆淡出就再也打不开。
    entries = await collect_media_index(bucket_id=bucket_id, include_archive=True)
    hits = _match_ref(entries, ref)
    if not hits:
        raise ToolInputError(f"没找到 ref={ref} 的照片。先用 media_read(query=...) 看索引里的 ref。")
    if len(hits) > 1:
        preview = "、".join(_short_ref(e) for e in hits[:5])
        raise ToolInputError(f"ref={ref} 同时匹配 {len(hits)} 张照片（{preview}），请给更长的 ref。")

    entry = hits[0]
    max_bytes = int((rt.config or {}).get("media_read_max_bytes") or _DEFAULT_READ_MAX_BYTES)
    data, mime = _read_verified(entry, max_bytes)
    label = " / ".join(x for x in (entry["title"], entry["note"]) if x)
    header = (
        f"🖼️ ref={_short_ref(entry)} 来自记忆 [bucket_id:{entry['bucket_id']}] 《{entry['bucket_name']}》"
        f" {entry['created'][:10]} · {mime} · {len(data) // 1024}KB · SHA-256 已校验"
        + (f"\n{label}" if label else "")
        + f"\n要看这条记忆的正文：breath_search(query=\"{entry['bucket_id']}\")"
    )
    return header, [(data, mime)]
