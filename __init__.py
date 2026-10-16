# -*- coding: utf-8 -*-
"""ComfyUI-SW-H3MultiRef —— 海螺 H3 多图参考生视频（一体节点包）。

  SW 海螺H3·多图参考素材输入     SWH3MultiRefSegments   小节点 + 点开大面板的导演台
  SW 海螺H3·多图参考生视频（一体） SWH3RefToVideo          除模型/加速/素材外全部内置

另外注册两个面板专用接口（GET /sw_h3ref/input_files、POST /sw_h3ref/probe_video|probe_audio）。
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

try:
    from .api_routes import register_routes

    register_routes()
except Exception as exc:  # 接口只在面板里用，注册失败不影响出图
    print("[SW-H3MultiRef] 面板接口注册失败（不影响出图）：%s" % exc, flush=True)

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
