# -*- coding: utf-8 -*-
"""给「H3 多图参考素材输入」面板用的三个小接口。

  GET  /sw_h3ref/input_files    列出 input 目录下的 视频 / 图片 / 音频
  POST /sw_h3ref/probe_video    返回视频的 时长 / 帧率 / 尺寸 / 帧数
  POST /sw_h3ref/probe_audio    返回音频的 时长 / 采样率 / 声道

只在前端面板里用，不影响节点执行。
"""

import os

from aiohttp import web

import folder_paths

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".gif", ".ts"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma", ".aiff", ".aif"}
MAX_WALK_FILES = 6000


def _root():
    return os.path.abspath(folder_paths.get_input_directory())


def _relative_to_input(abs_path):
    return os.path.relpath(os.path.abspath(abs_path), _root()).replace("\\", "/")


def _resolve(rel):
    """把面板给的相对路径安全地解析成 input 目录下的绝对路径（防目录穿越）。"""
    root = _root()
    candidate = os.path.abspath(os.path.join(root, str(rel).replace("\\", "/").lstrip("/")))
    try:
        if os.path.commonpath([root, candidate]) != root:
            return None
    except ValueError:
        return None
    return candidate if os.path.isfile(candidate) else None


def _walk_input():
    root = _root()
    if not os.path.isdir(root):
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            found.append(os.path.join(dirpath, name))
            if len(found) >= MAX_WALK_FILES:
                return found
    return found


def _probe_video(path):
    import av

    duration, fps, width, height, frames = 0.0, 0.0, 0, 0, 0
    with av.open(path) as container:
        if container.duration is not None:
            duration = float(container.duration / av.time_base)
        for stream in container.streams:
            if stream.type == "video":
                width = int(stream.width or 0)
                height = int(stream.height or 0)
                fps = float(stream.average_rate) if stream.average_rate else 0.0
                if not duration and stream.frames and fps:
                    duration = float(stream.frames) / fps
                break
    if duration and fps:
        frames = int(round(duration * fps))
    return {"duration": duration, "fps": fps, "width": width, "height": height, "frames": frames}


def _probe_audio(path):
    import av

    duration, sample_rate, channels = 0.0, 0, 0
    with av.open(path) as container:
        if container.duration is not None:
            duration = float(container.duration / av.time_base)
        for stream in container.streams:
            if stream.type == "audio":
                sample_rate = int(stream.rate or 0)
                channels = int(stream.channels or 0)
                if not duration:
                    frames = int(getattr(stream, "frames", 0) or 0)
                    if frames and sample_rate:
                        duration = frames / float(sample_rate)
                break
    return {"duration": duration, "sample_rate": sample_rate, "channels": channels}


def register_routes():
    """由 __init__.py 调用；重复调用不会重复注册。"""
    try:
        from server import PromptServer
    except ImportError:
        return False

    routes = PromptServer.instance.routes

    @routes.get("/sw_h3ref/input_files")
    async def list_input_files(request):
        videos, images, audios = [], [], []
        for path in _walk_input():
            ext = os.path.splitext(path)[1].lower()
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            item = {"name": os.path.basename(path), "path": _relative_to_input(path), "size": size}
            if ext in VIDEO_EXTENSIONS:
                videos.append(item)
            elif ext in IMAGE_EXTENSIONS:
                images.append(item)
            elif ext in AUDIO_EXTENSIONS:
                audios.append(item)
        key = lambda it: it["name"].lower()  # noqa: E731
        videos.sort(key=key)
        images.sort(key=key)
        audios.sort(key=key)
        return web.json_response({"videos": videos, "images": images, "audios": audios})

    @routes.post("/sw_h3ref/probe_video")
    async def probe_video(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        rel = str(payload.get("path") or "").strip()
        if not rel:
            return web.json_response({"error": "缺少 path"}, status=400)
        path = _resolve(rel)
        if path is None:
            return web.json_response({"error": "找不到文件"}, status=404)
        try:
            return web.json_response(_probe_video(path))
        except Exception as exc:
            return web.json_response({"error": "无法读取视频信息：%s" % exc})

    @routes.post("/sw_h3ref/probe_audio")
    async def probe_audio(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        rel = str(payload.get("path") or "").strip()
        if not rel:
            return web.json_response({"error": "缺少 path"}, status=400)
        path = _resolve(rel)
        if path is None:
            return web.json_response({"error": "找不到文件"}, status=404)
        try:
            return web.json_response(_probe_audio(path))
        except Exception as exc:
            return web.json_response({"error": "无法读取音频信息：%s" % exc})

    return True
