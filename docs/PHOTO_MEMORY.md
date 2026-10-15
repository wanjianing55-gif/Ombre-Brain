# 照片记忆：现状评估与使用方式

> 本文档属于本 fork（wanjianing55-gif/Ombre-Brain），基于上游 3.6.14。

## 1. 改动之前的架构评估

| 能力 | 上游 3.6.14 状态 | 说明 |
|---|---|---|
| 存原图 | ✅ 已有 | `hold(media=[...])` / `trace(media_append=[...])` 经 `MediaStore` 写入 `<数据目录>/_media/<bucket_id>/<sha256>.<ext>`，原子写、SHA-256 内容寻址、单项上限 25 MiB（`OMBRE_MEDIA_MAX_BYTES`）。 |
| 引用登记 | ✅ 已有 | 桶 frontmatter 的 `media:` 列表：`path` / `sha256` / `size` / `type` / `title` / `note`。 |
| 模型读回原图 | ❌ 没有 | `breath` / `breath_search` / `dream` 只输出一行 `🖼️ media: <path> (title)`，**不读文件内容**；没有任何 MCP 工具返回图片。 |
| 照片索引 / 检索 | ⚠️ 间接 | 只能通过照片所在记忆的正文被 `breath_search` 命中；照片的 `title` / `note` 不进检索。 |
| 从 Claude.ai 写入照片 | ⚠️ 实际不可用 | `media` 只接受「服务器 /tmp 下的路径」或 `data_base64`。Claude.ai 里模型看得见图片，但拿不到图片字节；让模型逐字吐出几 MB 的 Base64 不现实。 |
| 备份 | ❌ 缺口 | Dashboard ZIP 导出与 GitHub 同步只带 `.md`、`_sources/` 和 You 快照，**不带 `_media/`**。 |

## 2. 本 fork 补上的部分

1. **`media_read` MCP 工具（可选，默认关闭）**
   - 开启：`config.yaml` 写 `media_read_tool: true`，或环境变量 `OMBRE_MEDIA_READ_TOOL=true`，重启。
   - `media_read()` / `media_read(query="海边")` / `media_read(bucket_id="...")` → 照片索引，每行一个 `ref`。
     `query` 是空格分隔的 AND 关键词，匹配记忆标题/标签/域/正文 + 照片 title/note。
   - `media_read(ref="3fd6e6be528c")` → 返回 **原图本身**（MCP `image` 内容），读前校验 SHA-256。
     `ref` 可以是 SHA-256 前缀（≥8 位）或 `🖼️ media:` 后面的路径。显式按 ref 读时也能读归档记忆上的照片。
   - 只读：不 touch、不强化、不改任何桶。
   - 安全边界：不跟随符号链接、不读 `_media/` 之外的文件（frontmatter 可被手改）、只返回按文件头识别的
     JPEG/PNG/GIF/WebP、单次上限 `media_read_max_bytes`（默认 5 MiB，超过的原图仍完整保存在磁盘）、信件上的媒体不进索引。
   - 默认关闭的原因：上游把基础工具数固定为 16 个（claude.ai 按需加载工具时，工具越多越容易漏加载），
     关闭时工具清单与上游完全一致。

2. **人工上传入口**
   - 页面：`https://<你的域名>/media`（需先在 Dashboard 登录）。
   - API：`POST /api/media/upload`（Dashboard 登录 cookie；multipart 字段 `file`、`bucket_id`、可选 `title`、`note`）。
   - 只接受 JPEG/PNG/GIF/WebP；iPhone 的 HEIC 需先转 JPEG（设置 → 相机 → 格式 → 兼容性最佳）。
   - `GET /api/media/{bucket_id}/{filename}`：登录后在浏览器里看原图；只返回 frontmatter 登记过的位图，带 `CSP: sandbox`。

3. **整库备份脚本 `tools/backup_vault.py`**：把数据目录（含 `_media/`）打成 tar.gz，SQLite 用 backup API 取一致性快照。

## 3. 推荐工作流

1. 你在 Claude 里发照片并说「记住这个」。
2. Claude 用 `hold` 写下这件事（照片里有什么、为什么重要），把 `bucket_id` 告诉你。
3. 你打开 `/media`，填 `bucket_id`、选原图上传。页面返回 `ref`。
4. 以后 Claude 通过 `breath_search` 想起这件事时，会看到 `🖼️ media:` 一行；需要真的看照片时调用 `media_read(ref=...)`。

## 4. 尚未做 / 可以以后再做

- **照片语义检索**：目前索引是关键词匹配，语义检索依赖照片所在记忆的正文 embedding。若要「按画面内容」检索，
  需要在上传时让多模态模型生成描述并写进 `note`（会产生 API 费用）。
- **ZIP 导出 / GitHub 同步带上 `_media/`**：涉及导入校验与仓库体积，暂用 `tools/backup_vault.py` 做卷级备份代替。
- **缩略图 / 自动转码**：仓库没有 Pillow 依赖；超过 5 MiB 的原图目前只提示，不缩放。
- **Dashboard 热更新会覆盖本 fork 的改动**：默认更新源是上游 `P0luz/Ombre-Brain`。要保留照片功能，
  请用 `git pull` + 重建镜像更新；或设 `OMBRE_ALLOW_CUSTOM_UPDATE_REPO=1` 并在 `config.yaml` 写
  `update: {repo: "wanjianing55-gif/Ombre-Brain"}`。
