# 部署手册（本 fork）

> 目标：Claude 跨会话读写记忆，并能通过 MCP 读回照片原图。
> 本 fork 比上游多了 `media_read` 与照片上传（见 [PHOTO_MEMORY.md](PHOTO_MEMORY.md)），
> **所以必须从本仓库代码构建/部署**，不能用上游的 `p0luz/ombre-brain:latest` 镜像
> （那个镜像没有照片读回功能，记忆本身仍兼容）。

## 0. 先认准持久化

记忆的真源是数据目录里的 `.md` 文件，照片在同一目录的 `_media/` 下：

```
<数据目录>/
  dynamic/ permanent/ archive/   记忆（Markdown，可直接用 Obsidian 打开）
  _sources/                      原文证据
  _media/<bucket_id>/<sha256>.jpg 照片原图
  _ledger/                       事件日志
  embeddings.db                  向量索引（派生，可重算）
  config.yaml                    配置（含 API Key，不要外传）
```

判断是否安全只有一条：**能在宿主机 / 平台磁盘上看到这些 `.md` 和照片文件**。容器重建、`git pull`、
代码更新都不会碰这个目录；只有「没挂持久盘」或手动删目录才会丢。

## 1. 现在用的方案：Mac 本地 + Claude Code（不用 Docker、不用 API Key）

```bash
brew install uv                       # 一次性；提供 Python 3.12（macOS 自带的是 3.9）
git clone -b claude/ombre-brain-setup-j7revo https://github.com/wanjianing55-gif/Ombre-Brain.git
cd Ombre-Brain
./deploy/mac_local.sh setup           # 建 .venv、装依赖、建 ~/OmbreBrain、生成本机 Token
./deploy/mac_local.sh start           # 只监听 127.0.0.1:18001
./deploy/mac_local.sh smoke           # 自测：文字保存/搜索 + 照片存入/media_read 读回
./deploy/mac_local.sh smoke ~/Desktop/某张照片.jpg   # 用你自己的照片再测一次
./deploy/mac_local.sh claude          # 打印 claude mcp add 命令，复制执行
```

- 数据全部在 `~/OmbreBrain`：记忆 `.md`、照片 `_media/`、配置。重启电脑、`git pull` 都不会动它。
- 没有 API Key 时：记忆逐字保存，但不自动打标签（主题显示「未分类」）；搜索只走关键词/BM25，没有语义检索。
- 在 Claude Code 里存照片：让 Claude 先把照片复制到 `/tmp`，再 `hold(content=..., media=["/tmp/xx.jpg"])`
  （服务端只从临时目录收文件，防止一次调用读走任意文件）。也可以浏览器打开 `http://127.0.0.1:18001/media` 上传。
- iPhone 的 HEIC 先转换：`sips -s format jpeg in.heic --out out.jpg`。
- 电脑重启后需再执行一次 `./deploy/mac_local.sh start`。

## 1b. 以后想要一直在线 / 手机也能用：两条路线（二选一）

| | A. 自己的电脑 / NAS + Cloudflare Tunnel | B. Render Starter |
|---|---|---|
| 月费 | 0（需要一个托管在 Cloudflare 的域名，约 $10/年） | 约 $7（实例）+ 5 GB 磁盘约 $1.25 |
| 在线 | 电脑开着才在线 | 一直在线 |
| HTTPS | Cloudflare Tunnel 提供 | Render 自带 |
| 持久化 | 本机目录 | Render 持久盘（平台每日快照） |
| 维护 | `git pull` + `docker compose up -d --build` | 推送到仓库后自动重新部署 |

### A. 自己的电脑 + Docker

```bash
git clone https://github.com/wanjianing55-gif/Ombre-Brain.git
cd Ombre-Brain
cat > deploy/.env <<'EOF'
OMBRE_DASHBOARD_PASSWORD=换成一个长密码
OMBRE_HOST_VAULT_DIR=/绝对路径/ombre-data      # 记忆与照片都在这里
OMBRE_MEDIA_READ_TOOL=true
# 可选：现在不填也行，之后在 Dashboard「③ 引擎」里填
OMBRE_COMPRESS_API_KEY=
OMBRE_EMBED_API_KEY=
EOF
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
curl http://localhost:18001/health      # → {"status":"ok"}
```

浏览器打开 `http://localhost:18001` 设置/登录 Dashboard。

HTTPS：Dashboard → 设置 → Cloudflare Tunnel，粘贴 Tunnel token → 启动；然后在 Cloudflare 为 Tunnel 添加
Public Hostname（例如 `ombre.你的域名`，Service `HTTP` → `localhost:8000`）。

### B. Render

1. Render → New → Blueprint → 选本仓库（`render.yaml` 已配好持久盘 `/opt/render/project/src/buckets` 和 `OMBRE_MEDIA_READ_TOOL=true`）。
2. 在 Render 面板填 `OMBRE_COMPRESS_API_KEY` 等（可先留空）。`OMBRE_DASHBOARD_PASSWORD` 会自动生成，在 Environment 里查看。
3. 部署完成后：Dashboard `https://<服务名>.onrender.com`，MCP `https://<服务名>.onrender.com/mcp`。
4. 打开 `/onboarding` → 「公网安全模式」→ 把上面的 https 地址填进「公网连接地址」→ 保存 → 在 Render 里重启一次。

## 2. 连接 Claude（远程 HTTPS 时）

**Claude.ai（网页 / 手机 / 桌面 App）**：Settings → Connectors → Add custom connector → 填 `https://<你的域名>/mcp`
→ 弹出的是**你自己服务器**的授权页，输入 Dashboard 密码 → 完成。之后每个对话里在工具菜单启用 Ombre Brain。

**Claude Code（终端）**：

```bash
# 远程（OAuth，会打开浏览器授权）
claude mcp add --transport http ombre-brain https://<你的域名>/mcp
# 同一台电脑上用 Docker 部署时也可以直连本机
claude mcp add --transport http ombre-brain http://127.0.0.1:18001/mcp
```

只给不支持 OAuth 的客户端用时，才在 Dashboard「MCP 鉴权」里切到「OAuth + 静态 Token 共存」并生成 Token；
请求头 `Authorization: Bearer <Token>`。Token 等同万能钥匙，不要放进 URL、截图或仓库。

**让 Claude 按约定使用**：把 [CLAUDE_PROMPT.md](CLAUDE_PROMPT.md) 的内容放进 Claude.ai Project 指令 / 个人偏好，
核心一句：每个对话先调用 `breath()`。

## 3. 备份（照片务必做）

Dashboard ZIP 导出与 GitHub 同步**不含照片**。照片的异地副本靠整库备份：

```bash
# A 路线（宿主机）
python tools/backup_vault.py /绝对路径/ombre-data ~/ombre-backup-$(date +%F).tar.gz
# 容器内
docker exec ombre-brain python /app/tools/backup_vault.py /app/buckets /tmp/ombre-backup.tar.gz
docker cp ombre-brain:/tmp/ombre-backup.tar.gz .
```

归档含 `config.yaml`（可能有 API Key）——加 `--exclude-secrets` 可跳过。另外建议在 Dashboard → GitHub 同步
配一个**私有**仓库，给 `.md` 记忆多一份云端副本。

## 4. 更新

```bash
git pull
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
```

不要用 Dashboard「热更新」（它默认从上游拉代码，会覆盖本 fork 的照片功能）。如确实想用，设
`OMBRE_ALLOW_CUSTOM_UPDATE_REPO=1`，并在 `config.yaml` 写 `update: {repo: "wanjianing55-gif/Ombre-Brain"}`。
