#!/usr/bin/env python3
"""Extract verbatim conversation excerpts referenced by memory pointers.

Memories written from a claude.ai export can carry pointers such as
``【原文 dreams #5093-#5160】`` instead of copying the original text. This
tool scans a vault for those pointers, looks the referenced messages up in
the export's ``conversations.json`` and writes one Markdown file per memory
bucket containing the original messages, word for word.

The output directory should live OUTSIDE the vault (the default is a sibling
folder), so the excerpts are never indexed as memories, embedded, synced or
surfaced by ``breath``. Nothing in the vault is modified.

Message numbers are 0-based indexes into the conversation's
``chat_messages`` list, in export order.

Usage:
    python3 tools/extract_source_pointers.py \
        --vault ~/OmbreBrain \
        --export ~/Downloads/conversations.json \
        [--out ~/OmbreBrain-原始记录] [--tz Australia/Melbourne] \
        [--only-tag 亲密] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

POINTER_RE = re.compile(r"【原文\s+(?P<conv>[^\s】#]+)\s+(?P<ranges>[^】]+)】")
RANGE_RE = re.compile(r"#(\d+)(?:\s*-\s*#?(\d+))?")
ID_RE = re.compile(r"^id:\s*['\"]?([0-9a-zA-Z_-]+)['\"]?\s*$", re.M)
TITLE_RE = re.compile(r"^(?:name|title):\s*['\"]?(.+?)['\"]?\s*$", re.M)
TAGS_RE = re.compile(r"^tags:\s*(.*)$", re.M)


def read_bucket(path: str) -> tuple[str, str, str, str]:
    """Return (bucket_id, title, tags_line, full_text) for one .md file."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    front = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            front = text[3:end]
    m = ID_RE.search(front)
    stem = os.path.splitext(os.path.basename(path))[0]
    bucket_id = m.group(1) if m else stem.rsplit("_", 1)[-1]
    t = TITLE_RE.search(front)
    tags = TAGS_RE.search(front)
    return bucket_id, (t.group(1) if t else stem), (tags.group(1) if tags else ""), text


def collect_pointers(vault: str, out_dir: str, only_tag: str | None):
    """Yield (bucket_id, title, conv_name, [(start, end), ...]) for every pointer."""
    out_abs = os.path.abspath(out_dir)
    for root, dirs, files in os.walk(vault):
        # skip derived/private folders and the output folder itself
        dirs[:] = [
            d for d in dirs
            if not d.startswith((".", "_")) and os.path.abspath(os.path.join(root, d)) != out_abs
        ]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            path = os.path.join(root, fname)
            try:
                bucket_id, title, tags, text = read_bucket(path)
            except (OSError, UnicodeDecodeError) as exc:
                print(f"skip {path}: {exc}", file=sys.stderr)
                continue
            if only_tag and only_tag not in tags and only_tag not in text.split("\n---", 2)[0]:
                continue
            for pm in POINTER_RE.finditer(text):
                ranges = []
                for rm in RANGE_RE.finditer(pm.group("ranges")):
                    a = int(rm.group(1))
                    b = int(rm.group(2)) if rm.group(2) else a
                    ranges.append((min(a, b), max(a, b)))
                if ranges:
                    yield bucket_id, title, pm.group("conv"), ranges


def load_conversations(export_path: str) -> dict[str, list[dict]]:
    with open(export_path, encoding="utf-8") as fh:
        data = json.load(fh)
    by_key: dict[str, list[dict]] = {}
    for conv in data:
        msgs = conv.get("chat_messages") or []
        for key in (conv.get("name"), conv.get("uuid")):
            if key and key not in by_key:  # first conversation with a name wins
                by_key[key] = msgs
    return by_key


def message_text(msg: dict) -> str:
    parts = [
        block.get("text") or ""
        for block in (msg.get("content") or [])
        if block.get("type") == "text"
    ]
    text = "\n".join(parts).strip()
    return text or (msg.get("text") or "").strip()


def fmt_time(iso: str, tz: ZoneInfo) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return iso or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", required=True, help="Ombre Brain vault directory (e.g. ~/OmbreBrain)")
    ap.add_argument("--export", required=True, help="conversations.json from the claude.ai export")
    ap.add_argument("--out", help="output directory (default: <vault>-原始记录, next to the vault)")
    ap.add_argument("--tz", default="Australia/Melbourne", help="timezone for timestamps")
    ap.add_argument("--only-tag", help="only memories whose frontmatter/tags contain this text, e.g. 亲密")
    ap.add_argument("--human", default="Joan", help="label for the human side")
    ap.add_argument("--assistant", default="Claude", help="label for the assistant side")
    ap.add_argument("--dry-run", action="store_true", help="list what would be written, write nothing")
    args = ap.parse_args()

    vault = os.path.abspath(os.path.expanduser(args.vault))
    out_dir = os.path.abspath(os.path.expanduser(args.out or vault.rstrip("/") + "-原始记录"))
    if os.path.commonpath([vault, out_dir]) == vault:
        print("warning: output is inside the vault; prefer a folder next to it", file=sys.stderr)
    tz = ZoneInfo(args.tz)

    convs = load_conversations(os.path.expanduser(args.export))
    grouped: dict[tuple[str, str], dict] = {}
    for bucket_id, title, conv, ranges in collect_pointers(vault, out_dir, args.only_tag):
        entry = grouped.setdefault((conv, bucket_id), {"title": title, "ranges": []})
        entry["ranges"].extend(ranges)

    if not grouped:
        print("no 【原文 …】 pointers found")
        return 0

    written = 0
    missing = defaultdict(int)
    for (conv, bucket_id), entry in sorted(grouped.items()):
        msgs = convs.get(conv)
        if msgs is None:
            missing[conv] += 1
            continue
        ranges = sorted(set(entry["ranges"]))
        target = os.path.join(out_dir, conv, f"{bucket_id}.md")
        if args.dry_run:
            print(f"{target}  <- {', '.join(f'#{a}-#{b}' for a, b in ranges)}  ({entry['title']})")
            continue
        lines = [
            f"# {entry['title']}",
            "",
            f"- 记忆 ID：{bucket_id}",
            f"- 对话：{conv}",
            f"- 范围：{', '.join(f'#{a}-#{b}' for a, b in ranges)}",
            f"- 时区：{args.tz}",
            "",
        ]
        for a, b in ranges:
            lines += ["---", f"## #{a}-#{b}", ""]
            for i in range(a, min(b, len(msgs) - 1) + 1):
                msg = msgs[i]
                who = args.human if msg.get("sender") == "human" else args.assistant
                lines.append(f"**#{i} {who}** · {fmt_time(msg.get('created_at', ''), tz)}")
                lines.append("")
                lines.append(message_text(msg))
                lines.append("")
            if b >= len(msgs):
                lines.append(f"(注意：对话只有 {len(msgs)} 条消息，#{b} 超出范围)")
                lines.append("")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        written += 1

    for conv, n in missing.items():
        print(f"warning: conversation '{conv}' not found in export ({n} memories skipped)", file=sys.stderr)
    if not args.dry_run:
        print(f"wrote {written} files to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
