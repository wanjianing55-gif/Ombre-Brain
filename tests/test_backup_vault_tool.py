"""tools/backup_vault.py：卷级备份必须带上 _media 照片原图，且不打包会话/锁。

Dashboard ZIP 导出与 GitHub 同步都不带 _media，这个脚本是照片的唯一异地副本来源。
"""

import importlib.util
import sqlite3
import tarfile
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "backup_vault", Path(__file__).resolve().parent.parent / "tools" / "backup_vault.py"
)
backup_vault = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup_vault)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "dynamic").mkdir(parents=True)
    (vault / "dynamic" / "a.md").write_text("---\nid: a\n---\n正文", encoding="utf-8")
    (vault / "_media" / "a").mkdir(parents=True)
    (vault / "_media" / "a" / ("0" * 64 + ".jpg")).write_bytes(b"\xff\xd8\xffphoto")
    (vault / ".logs").mkdir()
    (vault / ".logs" / "server.log").write_text("log")
    (vault / ".dashboard_sessions.json").write_text("{}")
    (vault / "config.yaml").write_text("dehydration: {api_key: secret}")
    with sqlite3.connect(vault / "embeddings.db") as db:
        db.execute("create table t (x)")
        db.execute("insert into t values (1)")
    return vault


def test_backup_includes_media_and_skips_ephemeral(tmp_path):
    vault = _vault(tmp_path)
    out = tmp_path / "b.tar.gz"
    counts = backup_vault.backup(str(vault), str(out))
    names = set(tarfile.open(out).getnames())
    assert "_media/a/" + "0" * 64 + ".jpg" in names
    assert "dynamic/a.md" in names and "embeddings.db" in names and "config.yaml" in names
    assert not any(n.startswith(".logs") for n in names)
    assert ".dashboard_sessions.json" not in names
    assert counts["media"] == 1 and counts["sqlite"] == 1

    restored = tmp_path / "restored"
    tarfile.open(out).extractall(restored, filter="data")
    with sqlite3.connect(restored / "embeddings.db") as db:
        assert db.execute("select x from t").fetchone() == (1,)


def test_exclude_secrets_and_refuse_output_inside_vault(tmp_path):
    vault = _vault(tmp_path)
    out = tmp_path / "b.tar.gz"
    backup_vault.backup(str(vault), str(out), exclude_secrets=True)
    assert "config.yaml" not in set(tarfile.open(out).getnames())
    with pytest.raises(SystemExit):
        backup_vault.backup(str(vault), str(vault / "inside.tar.gz"))
