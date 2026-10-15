"""
========================================
tools/media/__init__.py — media_read 工具入口（可选，默认关闭）
========================================

hold(media=...) / trace(media_append=...) 早就能把照片存进 _media/，但读取侧
只给 path 字符串——模型看见的是文件名，不是那张照片。media_read 补上这一半：
列出 / 检索照片索引，并按需把校验过的原图作为 MCP 图片内容返回。

默认不注册（config.media_read_tool / OMBRE_MEDIA_READ_TOOL），所以基础工具
仍是 16 个；开启后 /mcp 多出这一个。

对外暴露：media_read / collect_media_index
========================================
"""

from .core import collect_media_index, media_read  # noqa: F401
