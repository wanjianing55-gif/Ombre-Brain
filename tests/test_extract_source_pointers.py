import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "extract_source_pointers.py"


def _msg(i, sender):
    return {
        "sender": sender,
        "created_at": f"2026-09-20T14:{i:02d}:00Z",
        "content": [{"type": "text", "text": f"message {i}"}],
    }


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=True)


def test_extracts_pointer_ranges_outside_vault(tmp_path):
    vault = tmp_path / "vault"
    bucket_dir = vault / "dynamic" / "relationship"
    bucket_dir.mkdir(parents=True)
    (bucket_dir / "note_abc123.md").write_text(
        "---\nid: abc123\nname: 测试\ntags: [亲密]\n---\n【原文 dreams #1-#2】【原文 dreams #4】\n",
        encoding="utf-8",
    )
    (bucket_dir / "other_def456.md").write_text("---\nid: def456\nname: 无\n---\n正文\n", encoding="utf-8")
    export = tmp_path / "conversations.json"
    msgs = [_msg(i, "human" if i % 2 == 0 else "assistant") for i in range(6)]
    export.write_text(json.dumps([{"name": "dreams", "uuid": "u1", "chat_messages": msgs}]), encoding="utf-8")

    result = _run("--vault", str(vault), "--export", str(export), "--tz", "UTC")
    assert "wrote 1 files" in result.stdout

    out = tmp_path / "vault-原始记录" / "dreams" / "abc123.md"
    text = out.read_text(encoding="utf-8")
    assert "**#1 Claude** · 2026-09-20 14:01" in text
    assert "message 2" in text and "message 4" in text
    assert "message 3" not in text and "message 0" not in text
    assert not (tmp_path / "vault-原始记录" / "dreams" / "def456.md").exists()


def test_only_tag_filter_and_dry_run(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a_x1.md").write_text("---\nid: x1\ntags: [日常]\n---\n【原文 dreams #0】\n", encoding="utf-8")
    export = tmp_path / "c.json"
    export.write_text(json.dumps([{"name": "dreams", "chat_messages": [_msg(0, "human")]}]), encoding="utf-8")

    result = _run("--vault", str(vault), "--export", str(export), "--only-tag", "亲密")
    assert "no 【原文" in result.stdout

    result = _run("--vault", str(vault), "--export", str(export), "--dry-run")
    assert "x1.md" in result.stdout
    assert not (tmp_path / "vault-原始记录").exists()
