"""
========================================
web/media.py — 给已有记忆挂照片（人工上传入口）
========================================

为什么需要：hold(media=[{data_base64:...}]) 要求模型把整张照片的 Base64 打进
工具参数。Claude.ai 里模型看得见用户发的照片，却拿不到它的字节——几 MB 的
原图也不可能靠模型逐字吐出来。所以原图由人上传，模型负责写那段记忆。

POST /api/media/upload（Dashboard 登录态）
  multipart 字段：file（必填）、bucket_id（必填）、title、note（可选）
  → 走 bucket_mgr.update(media_append=...)，与 trace(media_append=...) 同一条
    持久化路径：SHA-256 内容寻址、原子写入、单项上限 media_max_bytes。

GET /media（页面本身公开，提交时靠 Dashboard 登录态）
  → 极简上传页：填 bucket_id、选照片、可写标题/备注。

GET /api/media/{bucket_id}/{filename}（Dashboard 登录态）
  → 返回已挂在该记忆上的原图，供人在浏览器里核对。只认 frontmatter 里登记过的文件。

不做什么：不新建记忆（记忆该由模型写）、不删照片、不转码。

对外暴露：register(mcp)
========================================
"""

from __future__ import annotations

import base64
import logging
import os
import re
import stat
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response

from tools.media.core import sniff_image_mime

from . import _shared as sh

logger = logging.getLogger("ombre_brain.web.media")

_SAFE_NAME = re.compile(r"^[0-9a-f]{64}\.[a-zA-Z0-9]{1,10}$")

_UPLOAD_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ombre Brain · 照片上传</title>
<style>
body{font-family:system-ui,sans-serif;max-width:520px;margin:2rem auto;padding:0 16px;line-height:1.6}
label{display:block;margin-top:.8rem}input{width:100%;box-sizing:border-box;padding:.4rem}
button{margin-top:1rem;padding:.5rem 1.2rem}#out{white-space:pre-wrap;margin-top:1rem}
img{max-width:100%;margin-top:1rem;border-radius:6px}
</style></head><body>
<h2>给一条记忆挂照片</h2>
<p>先让 Claude 用 <code>hold</code> 记下这件事，它会给你一个 bucket_id。需要先在 <a href="/">Dashboard</a> 登录。
只接受 JPEG / PNG / GIF / WebP（iPhone 请在「设置→相机→格式」选「兼容性最佳」，或导出为 JPEG）。</p>
<form id="f">
<label>bucket_id<input name="bucket_id" required pattern="[A-Za-z0-9_.-]+"></label>
<label>照片<input name="file" type="file" accept="image/jpeg,image/png,image/gif,image/webp" required></label>
<label>标题（可选）<input name="title" maxlength="200"></label>
<label>备注（可选）<input name="note" maxlength="500"></label>
<button>上传</button>
</form>
<div id="out"></div><img id="pv" hidden alt="">
<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = document.getElementById("out"), pv = document.getElementById("pv");
  out.textContent = "上传中…"; pv.hidden = true;
  try {
    const r = await fetch("/api/media/upload", {method: "POST", body: new FormData(e.target), credentials: "same-origin"});
    const j = await r.json();
    if (!r.ok) { out.textContent = "失败：" + (j.error || r.status); return; }
    const m = (j.media || [])[0] || {};
    const ref = String(m.sha256 || "").slice(0, 12);
    out.textContent = "已保存。ref = " + ref + "。Claude 可以用 media_read(ref='" + ref + "') 看到它。";
    pv.src = "/api/media/" + encodeURIComponent(j.bucket_id) + "/" + encodeURIComponent(String(m.path || "").split("/").pop());
    pv.hidden = false;
  } catch (err) { out.textContent = "失败：" + err; }
});
</script></body></html>
"""


def register(mcp) -> None:
    @mcp.custom_route("/media", methods=["GET"])
    async def media_upload_page(request: Request) -> Response:
        from starlette.responses import HTMLResponse

        return HTMLResponse(_UPLOAD_PAGE, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/api/media/upload", methods=["POST"])
    async def api_media_upload(request: Request) -> Response:
        from starlette.responses import JSONResponse

        from .import_api import _read_multipart_form_limited

        err = sh._require_auth(request)
        if err:
            return err

        max_bytes = int(sh.config.get("media_max_bytes") or 25 * 1024 * 1024)
        try:
            form = await _read_multipart_form_limited(request, max_bytes)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        try:
            bucket_id = str(form.get("bucket_id") or "").strip()
            upload = form.get("file")
            if not bucket_id:
                return JSONResponse({"error": "缺少 bucket_id"}, status_code=400)
            if upload is None or isinstance(upload, str):
                return JSONResponse({"error": "缺少 file 字段"}, status_code=400)
            data = await upload.read(max_bytes + 1)
            if not data:
                return JSONResponse({"error": "文件为空"}, status_code=400)
            if len(data) > max_bytes:
                return JSONResponse(
                    {"error": f"文件超过单项上限 {max_bytes} 字节"}, status_code=413
                )

            mime = sniff_image_mime(data)
            if not mime:
                return JSONResponse(
                    {"error": "只接受 JPEG/PNG/GIF/WebP（模型能直接看的格式）。iPhone 的 HEIC 请先导出为 JPEG。"},
                    status_code=415,
                )

            bucket = await sh.bucket_mgr.get(bucket_id)
            if not bucket:
                return JSONResponse({"error": "not found"}, status_code=404)
            if str((bucket.get("metadata") or {}).get("type") or "") == "letter":
                return JSONResponse({"error": "信件不挂照片"}, status_code=403)

            item = {
                "data_base64": base64.b64encode(data).decode("ascii"),
                "filename": os.path.basename(str(upload.filename or "photo")),
                "type": mime,
            }
            for key in ("title", "note"):
                value = str(form.get(key) or "").strip()
                if value:
                    item[key] = value
        finally:
            close = getattr(form, "close", None)
            if callable(close):
                await close()

        try:
            await sh.bucket_mgr.update(
                bucket_id, media_append=[item], event_actor="human"
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)[:300]}, status_code=400)
        except Exception:
            logger.error("media upload failed", exc_info=True)
            return JSONResponse({"error": "保存失败，详见服务器日志"}, status_code=500)

        refreshed = await sh.bucket_mgr.get(bucket_id) or {}
        media = (refreshed.get("metadata") or {}).get("media") or []
        return JSONResponse({"ok": True, "bucket_id": bucket_id, "media": media[-1:] if media else []})

    @mcp.custom_route("/api/media/{bucket_id}/{filename}", methods=["GET"])
    async def api_media_get(request: Request) -> Response:
        from starlette.responses import JSONResponse

        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        filename = request.path_params["filename"]
        if not _SAFE_NAME.fullmatch(filename):
            return JSONResponse({"error": "not found"}, status_code=404)
        bucket = await sh.bucket_mgr.get_including_archive(bucket_id)
        if not bucket or str((bucket.get("metadata") or {}).get("type") or "") == "letter":
            return JSONResponse({"error": "not found"}, status_code=404)
        registered = {
            Path(str(m.get("path") or "")).name
            for m in (bucket.get("metadata") or {}).get("media") or []
            if isinstance(m, dict)
        }
        if filename not in registered:
            return JSONResponse({"error": "not found"}, status_code=404)

        store = sh.bucket_mgr.media_store
        for m in bucket["metadata"]["media"]:
            if Path(str(m.get("path") or "")).name != filename:
                continue
            raw = Path(str(m["path"]))
            candidate = raw if raw.is_absolute() else store.vault_dir / raw
            try:
                st = os.lstat(candidate)
            except OSError:
                break
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                break
            resolved = candidate.resolve()
            if store.media_dir not in resolved.parents:
                break
            # 只按文件头认四种位图：SVG/HTML 若以同源返回就是存储型 XSS。
            data = resolved.read_bytes()
            mime = sniff_image_mime(data)
            if not mime:
                break
            return Response(
                data,
                media_type=mime,
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                },
            )
        return JSONResponse({"error": "not found"}, status_code=404)
