#!/usr/bin/env bash
# Ombre Brain — Mac 本地运行（不用 Docker、不需要 API Key）
#
#   ./deploy/mac_local.sh setup     建 .venv、装依赖、建数据目录、生成本机 MCP Token
#   ./deploy/mac_local.sh start     后台启动（只监听 127.0.0.1）
#   ./deploy/mac_local.sh stop      停止
#   ./deploy/mac_local.sh status    是否在跑
#   ./deploy/mac_local.sh claude    打印连接 Claude Code 的命令
#   ./deploy/mac_local.sh smoke [照片路径]   端到端自测：文字保存/搜索 + 照片存入/读回原图
#   ./deploy/mac_local.sh logs      跟随日志
#
# 数据目录默认 ~/OmbreBrain（记忆 .md + _media/ 照片原图 + 配置），可用 OMBRE_HOME 覆盖。
# 不要放在 iCloud 同步的「文稿/桌面」里：同步冲突会产生重复文件。
# 端口默认 18001，可用 OMBRE_LOCAL_PORT 覆盖。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${OMBRE_HOME:-$HOME/OmbreBrain}"
PORT="${OMBRE_LOCAL_PORT:-18001}"
VENV="$REPO_DIR/.venv"
PY="$VENV/bin/python"
RUN_DIR="$DATA_DIR/.local_run"
PID_FILE="$RUN_DIR/server.pid"
LOG_FILE="$RUN_DIR/server.out"
TOKEN_FILE="$DATA_DIR/.mcp_token"
HOOK_TOKEN_FILE="$DATA_DIR/.hook_token"
URL="http://127.0.0.1:$PORT"

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

find_python() {
  for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

new_token() { "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))'; }

cmd_setup() {
  if [ ! -x "$PY" ]; then
    if command -v uv >/dev/null 2>&1; then
      say "→ 用 uv 创建 Python 3.12 虚拟环境"
      uv venv --seed --python 3.12 "$VENV"
    elif base_py="$(find_python)"; then
      say "→ 用 $base_py 创建虚拟环境"
      "$base_py" -m venv "$VENV"
    else
      die "需要 Python 3.12+。macOS 自带的 python3 是 3.9。请先执行其一：
    brew install uv          （推荐，之后重跑 setup 会自动下载 Python 3.12）
    brew install python@3.12"
    fi
  fi
  say "→ 安装锁定依赖（第一次约 1–3 分钟）"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet --require-hashes -r "$REPO_DIR/requirements.lock.txt"

  mkdir -p "$DATA_DIR" "$RUN_DIR"
  chmod 700 "$DATA_DIR"
  for f in "$TOKEN_FILE" "$HOOK_TOKEN_FILE"; do
    if [ ! -s "$f" ]; then
      (umask 077; new_token > "$f")
    fi
  done
  say "✓ setup 完成。数据目录：$DATA_DIR"
  say "  下一步：./deploy/mac_local.sh start"
}

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

cmd_start() {
  [ -x "$PY" ] || die "还没 setup：./deploy/mac_local.sh setup"
  [ -s "$TOKEN_FILE" ] || die "缺少 $TOKEN_FILE，请先 setup"
  if is_running; then say "已在运行（pid $(cat "$PID_FILE")）：$URL"; return 0; fi
  mkdir -p "$RUN_DIR"
  (
    cd "$REPO_DIR"
    export OMBRE_VAULT_DIR="$DATA_DIR"
    export OMBRE_CONFIG_PATH="$DATA_DIR/config.yaml"
    export OMBRE_TRANSPORT=streamable-http
    export OMBRE_PORT="$PORT"
    export OMBRE_BIND_HOST=127.0.0.1
    # 只认本机 Token：不开 OAuth 路由，Claude Code 用请求头带 Token。
    export OMBRE_MCP_AUTH_MODE=token
    OMBRE_MCP_TOKEN="$(cat "$TOKEN_FILE")"; export OMBRE_MCP_TOKEN
    OMBRE_HOOK_TOKEN="$(cat "$HOOK_TOKEN_FILE")"; export OMBRE_HOOK_TOKEN
    export OMBRE_MEDIA_READ_TOOL=true
    nohup "$PY" src/server.py >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
  )
  for _ in $(seq 1 60); do
    if curl -fsS "$URL/health" >/dev/null 2>&1; then
      say "✓ 已启动：$URL   （Dashboard 首次打开会让你设置密码）"
      say "  MCP：$URL/mcp   日志：$LOG_FILE"
      return 0
    fi
    is_running || break
    sleep 1
  done
  tail -n 30 "$LOG_FILE" >&2 || true
  die "启动失败，见上面的日志"
}

cmd_stop() {
  if is_running; then
    kill "$(cat "$PID_FILE")"
    for _ in $(seq 1 20); do is_running || break; sleep 0.5; done
    say "✓ 已停止"
  else
    say "没有在运行"
  fi
  rm -f "$PID_FILE"
}

cmd_status() {
  if is_running && curl -fsS "$URL/health" >/dev/null 2>&1; then
    say "运行中（pid $(cat "$PID_FILE")）：$URL   数据目录：$DATA_DIR"
  else
    say "未运行"; return 1
  fi
}

cmd_claude() {
  [ -s "$TOKEN_FILE" ] || die "还没 setup"
  cat <<EOF
# 1) 把 Ombre Brain 加到 Claude Code（user 级：所有项目都能用）
claude mcp add --scope user --transport http ombre-brain $URL/mcp \\
  --header "Authorization: Bearer $(cat "$TOKEN_FILE")"

# 2) 确认已连接
claude mcp list

# 3)（可选）每次开新会话自动浮现记忆：把下面这段合并进 ~/.claude/settings.json
{
  "env": {
    "OMBRE_HOOK_URL": "$URL",
    "OMBRE_HOOK_TOKEN": "$(cat "$HOOK_TOKEN_FILE")"
  },
  "hooks": {
    "SessionStart": [
      {"matcher": "startup", "hooks": [{"type": "command", "command": "python3 '$REPO_DIR/.claude/hooks/session_breath.py'", "timeout": 12}]}
    ]
  }
}
EOF
  say ""
  say "⚠ Token 等同万能钥匙：别截图、别提交到任何仓库。换 Token：删掉 $TOKEN_FILE → setup → stop/start → 重新 claude mcp add。"
}

cmd_smoke() {
  cmd_status >/dev/null || die "服务没在运行：./deploy/mac_local.sh start"
  "$PY" "$REPO_DIR/deploy/local_smoke_test.py" \
    --url "$URL/mcp" --token-file "$TOKEN_FILE" --data-dir "$DATA_DIR" "$@"
}

case "${1:-}" in
  setup) cmd_setup ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  claude) cmd_claude ;;
  smoke) shift; cmd_smoke "$@" ;;
  logs) tail -f "$LOG_FILE" ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
