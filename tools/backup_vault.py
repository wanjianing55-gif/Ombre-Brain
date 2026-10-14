"""整库冷备份：把数据目录（含 _media 照片原图）打成一个 tar.gz。

为什么还要它：Dashboard 的 ZIP 导出和 GitHub 同步只带 .md、_sources 原文证据和
You 快照，**不带 _media/ 里的照片原图**。照片记忆一旦只剩一份磁盘，坏盘或误删
就找不回来。这个脚本做卷级备份，补上那一块。

只读：不改 vault 里任何文件。SQLite 用 backup API 取一致性快照（服务运行中也
安全），日志、锁、登录会话和镜像代码目录 _app 不打包。

注意：归档里会带 config.yaml（可能含 API Key）和 OAuth/Tunnel 等凭据文件，
请当作密钥保管；加 --exclude-secrets 则跳过 config.yaml 与名字含 token/oauth/
tunnel/secret 的文件（恢复后需重新填 Key、重新授权）。

用法：
    python tools/backup_vault.py /path/to/buckets /path/to/ombre-backup.tar.gz
    docker exec ombre-brain python /app/tools/backup_vault.py /app/buckets /app/buckets/../backup.tar.gz
恢复：停服务 → 解压到空的数据目录 → 启动。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tarfile
import tempfile

_SKIP_DIRS = {".logs", ".locks", "_app", "__pycache__"}
_SKIP_FILES = {".dashboard_sessions.json"}
_SECRET_MARKERS = ("token", "oauth", "tunnel", "secret")
_SQLITE_HEADER = b"SQLite format 3\x00"


def _is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == _SQLITE_HEADER
    except OSError:
        return False


def _skip_file(name: str, *, exclude_secrets: bool) -> bool:
    if name in _SKIP_FILES or name.endswith((".lock", "-wal", "-shm", "-journal")):
        return True
    if ".corrupt-" in name:
        return True
    if exclude_secrets:
        lowered = name.lower()
        if lowered == "config.yaml" or any(m in lowered for m in _SECRET_MARKERS):
            return True
    return False


def backup(vault_dir: str, out_path: str, *, exclude_secrets: bool = False) -> dict:
    vault_dir = os.path.abspath(vault_dir)
    out_path = os.path.abspath(out_path)
    if not os.path.isdir(os.path.join(vault_dir, "dynamic")):
        raise SystemExit(f"{vault_dir} 不像 Ombre Brain 数据目录（缺 dynamic/）")
    if out_path.startswith(vault_dir + os.sep):
        raise SystemExit("备份文件不要放进数据目录本身，否则下次备份会把它也打进去")

    counts = {"markdown": 0, "media": 0, "sqlite": 0, "other": 0}
    partial = out_path + ".partial"
    with tempfile.TemporaryDirectory(prefix="ombre-backup-") as scratch, tarfile.open(partial, "w:gz") as tar:
        for root, dirs, files in os.walk(vault_dir, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("backup_"))
            rel_root = os.path.relpath(root, vault_dir)
            for name in sorted(files):
                full = os.path.join(root, name)
                if os.path.islink(full) or _skip_file(name, exclude_secrets=exclude_secrets):
                    continue
                arcname = os.path.normpath(os.path.join(rel_root, name))
                if _is_sqlite(full):
                    snapshot = os.path.join(scratch, f"{counts['sqlite']}.db")
                    with sqlite3.connect(f"file:{full}?mode=ro", uri=True) as src, sqlite3.connect(snapshot) as dst:
                        src.backup(dst)
                    tar.add(snapshot, arcname=arcname)
                    counts["sqlite"] += 1
                    continue
                tar.add(full, arcname=arcname)
                if name.endswith(".md"):
                    counts["markdown"] += 1
                elif arcname.startswith("_media" + os.sep):
                    counts["media"] += 1
                else:
                    counts["other"] += 1
    os.replace(partial, out_path)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Ombre Brain 整库冷备份（含 _media 照片原图）")
    parser.add_argument("vault_dir")
    parser.add_argument("out_path")
    parser.add_argument("--exclude-secrets", action="store_true", help="不打包 config.yaml 与凭据文件")
    args = parser.parse_args()
    counts = backup(args.vault_dir, args.out_path, exclude_secrets=args.exclude_secrets)
    size = os.path.getsize(args.out_path)
    print(
        f"已写入 {args.out_path}（{size // 1024} KB）：记忆 {counts['markdown']} 个 .md，"
        f"照片/媒体 {counts['media']} 个，SQLite 快照 {counts['sqlite']} 个，其他 {counts['other']} 个"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
