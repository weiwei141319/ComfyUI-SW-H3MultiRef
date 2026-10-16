# -*- coding: utf-8 -*-
"""ComfyUI-SW-H3MultiRef —— 海螺 H3「多图参考生视频」一体节点包。

对应工作流：03海螺H3/04-7多图参考生视频SW武戏版01.json

包含两个节点：
  1. SWH3MultiRefSegments  —— 多图参考素材输入（小节点 + 点开大面板的导演台形态）
     每段 = 1 段参考视频 + ≤9 张参考图 + ≤3 条参考音频 + 1 段提示词 + 时间控制
  2. SWH3RefToVideo        —— 一体生成（除模型/加速/素材外全部内置）

一体节点内置的等价节点（与 04-7 工作流逐一对齐，含先后顺序）：
  MiniMaxH3ReferenceToVideo（内部直调官方节点，行为 100% 一致）
  SW_H3FreeTextEncoder → SW_H3ConditionStrength（顺序同 04-7：先卸 CLIP 再设强度）
  RandomNoise / KSamplerSelect / BasicScheduler / BasicGuider / SamplerCustomAdvanced
  SW_DocKr_VRAMCleanup（04-7 参数：offload_model=False / offload_cache=True / 7000MB）
  VAEDecode / VAEDecodeAudio / CreateVideo（fps=24, bit_depth=8, sRGB, codec=none）
  ResolutionSelector + PrimitiveFloat + ComfyMathExpression（画布与时长，吸附 H3 帧网格）

素材取帧参数与 SW-loadvideo（VHS_LoadVideo）同款语义：
  force_rate / frame_load_cap / skip_first_frames / select_every_nth /
  custom_width / custom_height / 格式（决定 32 对齐）
  + 参考视频短边（默认 352，= 用户 04-7 工作流同款）：官方节点对小于标称画布的
    参考帧保持原尺寸并整段送 VAE 编码，768 短边的编码量是 352 的 4.58 倍，
    这是插件曾比 04-7 工作流慢 3 倍+ 的根因；缩到 352 短边后总耗时约降至 1/3。
"""

import json
import math
import os
from pathlib import Path

import numpy as np
import torch

import comfy.model_management
import comfy.nested_tensor
import comfy.samplers
import comfy.utils
import folder_paths

from comfy_extras.nodes_minimax_h3 import MiniMaxH3ReferenceToVideo

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
H3_FPS = 24.0                      # H3 视频固定 24 fps
AUDIO_LATENT_FPS = 40
FRAME_BASE = 5                     # 帧网格 5 + 17k
FRAME_STEP = 17
CANVAS_MULTIPLE = 32               # 画布按 32 对齐
BASE_SHORT_EDGE = 768
MAX_PIXELS = 768 * 1344
REF_IMAGE_SHORT_EDGE = 2048

MAX_IMAGES = 9                     # 每段参考图上限（官方 Autogrow 上限）
MAX_AUDIOS = 3                     # 每段独立参考音频上限
SEG_SLOTS = 12                     # 段位上限（与前端 MAX_SLOTS 一致）
SEGMENTS_TYPE = "SW_H3_MULTIREF"
DATA_VERSION = 1

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".gif", ".ts"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma", ".aiff", ".aif"}

# 取帧格式：target_rate / 对齐倍数 / 像素上限（与 SW-loadvideo 的 VHSLoadFormats 同源）。
# max_pixels 是官方节点 adapt_canvas 的上限（H3 = MAX_PIXELS = 768*1344）：
# 反正官方也会压到这么大，提前在这里压掉，能省下大量内存和解码时间。
LOAD_FORMATS = {
    "H3": {"target_rate": 24, "downscale_ratio": 32, "max_pixels": 768 * 1344},
    "AnimateDiff": {"target_rate": 8, "downscale_ratio": 8, "max_pixels": 512 * 512},
    "Wan": {"target_rate": 16, "downscale_ratio": 8, "max_pixels": 832 * 480},
    "LTXV": {"target_rate": 24, "downscale_ratio": 32, "max_pixels": 768 * 512},
    "Mochi": {"target_rate": 24, "downscale_ratio": 16, "max_pixels": 848 * 480},
    "Hunyuan": {"target_rate": 24, "downscale_ratio": 16, "max_pixels": 848 * 480},
    "Cosmos": {"target_rate": 24, "downscale_ratio": 16, "max_pixels": 1280 * 704},
    "None": {"target_rate": 0, "downscale_ratio": 8, "max_pixels": 0},
}

# 「跟随参考视频」时最多取多少帧（30 秒 @24fps）。H3 官方对参考视频的建议是 2~15 秒，
# 再长也不会被用到（官方 execute 里会 frames[:length] 截掉），纯浪费内存。
MAX_REF_FRAMES = 720
FORMAT_NAMES = list(LOAD_FORMATS.keys())
DEFAULT_FORMAT = "H3"

DEFAULT_DATA = json.dumps(
    {"version": DATA_VERSION, "segments": []},
    ensure_ascii=False, separators=(",", ":"),
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _as_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) or math.isinf(result) else result


def _as_bool(value, default=False):
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "none", "null"):
            return default
        return text in ("1", "true", "yes", "on", "开", "是")
    if value is None:
        return default
    return bool(value)


def _align_frame_count(n):
    """向上吸附到 H3 帧网格 5 + 17k（官方 align_frame_count）。"""
    n = max(FRAME_BASE, _as_int(n, FRAME_BASE))
    while n % FRAME_STEP != FRAME_BASE:
        n += 1
    return n


def _align_frame_count_floor(n):
    """向下吸附到 5 + 17k（参考视频的硬性要求）。"""
    n = _as_int(n, FRAME_BASE)
    if n < FRAME_BASE:
        return FRAME_BASE
    while n % FRAME_STEP != FRAME_BASE and n > FRAME_BASE:
        n -= 1
    return n


def _seconds_to_length(seconds):
    """秒 -> 帧数（与工作流里 ComfyMathExpression 的表达式一致）。"""
    return _align_frame_count(max(FRAME_BASE, int(round(_as_float(seconds, 0.0) * H3_FPS))))


def _safe_input_path(relative_name, extensions):
    """把素材名解析到 ComfyUI input 目录内，防越权。"""
    root = Path(folder_paths.get_input_directory()).resolve()
    normalized = str(relative_name or "").replace("\\", "/").lstrip("/")
    if not normalized:
        raise ValueError("素材名为空")
    candidate = (root / Path(normalized)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("素材路径必须位于 ComfyUI input 目录内：%r" % relative_name) from exc
    if not candidate.is_file():
        raise FileNotFoundError(
            "找不到素材文件：%s（可能在 input 目录里被删掉了，请重新在面板里选一次）" % relative_name)
    if extensions and candidate.suffix.lower() not in extensions:
        raise ValueError("不支持的素材格式：%s" % candidate.suffix)
    return candidate


# --------------------------------------------------------------------------- #
# 素材加载（图片 / 音频 / 视频）
# --------------------------------------------------------------------------- #
def _load_image_file(path):
    """图片 -> [1,H,W,C] float 0-1。"""
    from PIL import Image, ImageOps

    with Image.open(path) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert("RGB")
        array = np.asarray(image, dtype=np.float32) / 255.0
    return torch.from_numpy(np.ascontiguousarray(array)).unsqueeze(0)


def _decode_audio_file(path, target_rate=44100):
    """音频（或视频里的音轨）-> {"waveform": [1,2,L], "sample_rate": rate}。"""
    import av

    chunks = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return None
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=target_rate)
        for frame in container.decode(stream):
            converted = resampler.resample(frame)
            if not isinstance(converted, list):
                converted = [converted]
            for output in converted:
                if output is None:
                    continue
                array = output.to_ndarray()
                if array.ndim == 1:
                    array = array[None, :]
                chunks.append(array.astype(np.float32, copy=False))
        flushed = resampler.resample(None)
        if not isinstance(flushed, list):
            flushed = [flushed] if flushed is not None else []
        for output in flushed:
            if output is None:
                continue
            array = output.to_ndarray()
            if array.ndim == 1:
                array = array[None, :]
            chunks.append(array.astype(np.float32, copy=False))
    if not chunks:
        return None
    waveform = torch.from_numpy(np.ascontiguousarray(np.concatenate(chunks, axis=1))).unsqueeze(0)
    return {"waveform": waveform, "sample_rate": int(target_rate)}


def _target_size(width, height, custom_width, custom_height, downscale_ratio, max_pixels=0, short_edge=0):
    """与 SW-loadvideo / VHS 的 target_size 一致，额外支持像素上限与短边上限。

    max_pixels > 0 且没填自定义尺寸时，把画面等比压到该像素数以内再对齐；
    short_edge > 0 时改按「短边像素」缩（只缩不放、保持宽高比），
    二者可叠加（先缩短边，再套像素上限）。

    ★ 为什么需要 short_edge：官方 MiniMaxH3ReferenceToVideo 对参考视频是
    「输入比标称画布（768 短边 / 768x1344）小就保持原尺寸，然后整段送 VAE 编码」
    （nodes_minimax_h3.py:321-325）。喂 768x1344 与喂 352x640，
    每帧像素量差 4.58 倍，整段参考视频的 VAE 编码量就差 4.58 倍 ——
    这正是插件比用户 04-7 工作流（SW-loadvideo + Resize 352x640）慢 3 倍+ 的根因。
    提前把帧缩到 352 短边，编码量降 4.58 倍，总耗时约降至 1/3。
    """
    custom = bool(custom_width) or bool(custom_height)
    if not custom_width and not custom_height:
        pass
    elif not custom_height:
        height *= custom_width / width
        width = custom_width
    elif not custom_width:
        width *= custom_height / height
        height = custom_height
    else:
        width, height = custom_width, custom_height

    ratio = max(1, _as_int(downscale_ratio, 8))
    limit = _as_int(max_pixels, 0)
    se = _as_int(short_edge, 0)
    if not custom and se > 0:
        scale = min(1.0, se / float(min(width, height)))
        width *= scale
        height *= scale
    if limit > 0 and not custom and width * height > limit:
        scale = math.sqrt(limit / float(width * height))
        width *= scale
        height *= scale

    width = int(width / ratio + 0.5) * ratio
    height = int(height / ratio + 0.5) * ratio
    # 对齐时是「四舍五入」，可能又把像素数顶回上限之上；这里再压一次，且只用向下取整，
    # 保证最终一定不超过 max_pixels。
    if limit > 0 and not custom and width * height > limit:
        scale = math.sqrt(limit / float(width * height))
        width = max(ratio, int(width * scale / ratio) * ratio)
        height = max(ratio, int(height * scale / ratio) * ratio)
    return max(ratio, width), max(ratio, height)


def _memory_frame_limit(width, height, channels=3):
    """按当前可用内存估算最多能安全缓存多少帧（对齐 VHS 的 max_loadable_frames 思路）。"""
    if not width or not height:
        return 0
    try:
        import psutil

        available = psutil.virtual_memory().available + psutil.swap_memory().free
    except Exception:
        return 0
    per_frame = width * height * channels * 4          # float32
    budget = int(available * 0.6)
    return max(1, budget // max(1, per_frame))


def _decode_video_file(path, force_rate=24, frame_load_cap=0, skip_first_frames=0,
                       select_every_nth=1, custom_width=0, custom_height=0,
                       downscale_ratio=32, max_pixels=0, short_edge=0):
    """视频 -> [T,H,W,C] float 0-1（取帧语义与 SW-loadvideo 一致，PyAV 解码）。

      force_rate         0 = 保持原帧率；否则按时间重采样到该帧率
      frame_load_cap     最多取多少帧（0 = 不限，但仍受 MAX_REF_FRAMES / 内存守卫约束）
      skip_first_frames  跳过片头 N 个输出帧
      select_every_nth   隔帧取样（1 = 每帧都取）
      custom_width/height  自定义尺寸（0 = 用原尺寸，填了则不套任何上限）
      downscale_ratio    最终尺寸按此倍数对齐（H3 = 32）
      max_pixels         像素上限（H3 = 768*1344），0 = 不限
      short_edge         短边像素上限（只缩不放）；> 0 时先生效，再叠 max_pixels。
                         默认 352 = 用户 04-7 工作流（SW-loadvideo + Resize 352x640）同款：
                         官方节点对小于标称画布的参考帧保持原尺寸并整段送 VAE 编码，
                         768 短边的编码量是 352 短边的 4.58 倍，这就是插件曾比工作流慢 3 倍+ 的根因。
    """
    import av

    target_rate = _as_float(force_rate, 0.0)
    skip = max(0, _as_int(skip_first_frames, 0))
    nth = max(1, _as_int(select_every_nth, 1))
    cap = max(0, _as_int(frame_load_cap, 0))

    kept = []
    src_width = src_height = 0
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError("视频文件没有画面轨：%s" % path.name)
        stream = container.streams.video[0]
        try:
            stream.thread_type = "AUTO"
        except Exception:
            pass
        src_fps = float(stream.average_rate) if stream.average_rate else 0.0
        src_width = int(stream.width or 0)
        src_height = int(stream.height or 0)
        rate = target_rate if target_rate > 0 else (src_fps or H3_FPS)
        time_base = stream.time_base

        # 先定目标尺寸，才能按内存估算能缓存多少帧
        new_width, new_height = _target_size(
            src_width, src_height, _as_int(custom_width, 0), _as_int(custom_height, 0),
            downscale_ratio, max_pixels, _as_int(short_edge, 0))
        hard_cap = MAX_REF_FRAMES if cap <= 0 else min(cap, MAX_REF_FRAMES)
        mem_cap = _memory_frame_limit(new_width, new_height)
        if mem_cap > 0:
            hard_cap = min(hard_cap, mem_cap)
        if hard_cap != cap:
            print("[SW-H3MultiRef] 取帧上限 %s -> %d（%s）" % (
                cap or "不限", hard_cap,
                "参考视频上限 %d" % MAX_REF_FRAMES if cap <= 0 or cap > MAX_REF_FRAMES else "面板设置"),
                flush=True)

        base_time = None
        slot = 0          # 已输出/已跳过的槽位号
        evaluated = -1    # 参与 select_every_nth 计数的帧号
        for frame in container.decode(stream):
            if frame.pts is not None and time_base is not None:
                stamp = float(frame.pts * time_base)
                if base_time is None:
                    base_time = stamp
                elapsed = stamp - base_time
            else:
                elapsed = slot / rate if rate else 0.0
            if elapsed + 1e-9 < slot / rate:
                continue                       # 还没到下一个输出槽的时间点
            slot += 1
            if slot <= skip:
                continue                       # 片头跳过
            evaluated += 1
            if evaluated % nth != 0:
                continue                       # 隔帧取样
            # 逐帧立刻缩到目标尺寸再入列：原尺寸堆叠 1080p×243 帧要 ~12GB 内存，
            # 缩完再堆只要几十 MB。
            array = frame.to_ndarray(format="rgb24")
            if not src_width or not src_height:
                # 流没报尺寸：用第一帧反推，并补算目标尺寸
                src_height, src_width = array.shape[:2]
                new_width, new_height = _target_size(
                    src_width, src_height, _as_int(custom_width, 0), _as_int(custom_height, 0),
                    downscale_ratio, max_pixels, _as_int(short_edge, 0))
            if new_width != src_width or new_height != src_height:
                from PIL import Image

                array = np.asarray(
                    Image.fromarray(array).resize((new_width, new_height), Image.LANCZOS),
                    dtype=np.uint8)
            kept.append(array)
            if len(kept) >= hard_cap:
                break

    if not kept:
        raise ValueError(
            "参考视频没有取到任何帧（skip_first_frames=%d 可能已超出视频长度）：%s"
            % (skip, path.name))

    if not src_width or not src_height:
        src_height, src_width = kept[0].shape[:2]
        new_width, new_height = _target_size(
            src_width, src_height, _as_int(custom_width, 0), _as_int(custom_height, 0),
            downscale_ratio, max_pixels, _as_int(short_edge, 0))

    # uint8 0-255 -> float 0-1（ComfyUI IMAGE 惯例）。
    # ⚠ 官方 MiniMaxH3ReferenceToVideo 的 _resize / vae.encode 都不做数值归一，
    #   这里少除一个 255，参考视频就会以全白画面的形式喂进 VAE 和 Qwen。
    frames = torch.from_numpy(np.ascontiguousarray(np.stack(kept, axis=0))).float() / 255.0

    info = {
        "source_fps": src_fps, "source_width": src_width, "source_height": src_height,
        "loaded_fps": rate, "loaded_frame_count": int(frames.shape[0]),
        "loaded_width": new_width, "loaded_height": new_height,
    }
    return frames, info


# --------------------------------------------------------------------------- #
# 参考编码辅助（官方 _resize / _encode_ref_audio 的同款实现，供长度估算用）
# --------------------------------------------------------------------------- #
def _av_latent_audio_seconds(length):
    """按帧数算这段成片的音轨秒数（24 fps）。"""
    return _align_frame_count(max(FRAME_BASE, length)) / H3_FPS


def _merge_audio(audios):
    """把各段音轨按顺序拼起来（采样率不同则重采样到第一条的采样率）。"""
    items = [a for a in audios if a is not None and isinstance(a, dict) and a.get("waveform") is not None]
    if not items:
        return None
    target_rate = int(items[0].get("sample_rate") or 44100)
    pieces = []
    for audio in items:
        waveform = audio["waveform"]
        if waveform.ndim != 3:
            continue
        rate = int(audio.get("sample_rate") or target_rate)
        if rate != target_rate:
            import comfy.audio

            waveform = comfy.audio.resample(waveform, rate, target_rate)
        if waveform.shape[0] != 1:
            waveform = waveform[:1]
        pieces.append(waveform)
    if not pieces:
        return None
    return {"waveform": torch.cat(pieces, dim=-1), "sample_rate": target_rate}


def _free_text_encoder(clip):
    """把 CLIP 编码器从显存里卸掉（等价 SW_H3FreeTextEncoder）。

    注意不能用 free_memory(target)：它把可驱逐权重也算作空闲，目标会立刻满足而不卸载。
    """
    unloaded = 0
    try:
        want = getattr(clip, "patcher", None)
        for loaded in list(getattr(comfy.model_management, "current_loaded_models", [])):
            if want is not None and getattr(loaded, "model", None) is not want:
                continue
            try:
                loaded.model_unload()
                if loaded in comfy.model_management.current_loaded_models:
                    comfy.model_management.current_loaded_models.remove(loaded)
                unloaded += 1
            except Exception:
                pass
    except Exception as exc:
        print("[SW-H3MultiRef] 定向卸载 CLIP 失败：%s" % exc, flush=True)

    if unloaded == 0:
        try:
            comfy.model_management.unload_all_models()
            unloaded = -1
        except Exception as exc:
            print("[SW-H3MultiRef] unload_all_models 失败：%s" % exc, flush=True)

    try:
        comfy.model_management.soft_empty_cache()
    except Exception:
        pass
    return unloaded


def _vram_cleanup(batch_index=1, batch_clear=1, bypass_all=False,
                  offload_model=False, offload_cache=True, critical_threshold_mb=7000):
    """1:1 复刻 04-7 工作流里的 SW DocKr / VRAM-Cleanup（节点 181）所用配置。

    04-7 的参数：batch_index=192, batch_clear=192（= 每次必触发）、
    bypass_all=False、offload_model=False、offload_cache=True、critical=7000MB。
    多段循环里每段算一个 batch，所以 batch_clear=1（每段都清）。

    offload_model=False 很关键：不卸载模型，只清分配器缓存 + gc，
    否则下一段要把 25GB DiT 重新读一遍。
    """
    import gc

    executed = False
    try:
        try:
            import psutil

            available_mb = psutil.virtual_memory().available / (1024 * 1024)
        except Exception:
            available_mb = 1 << 30
        is_critical = available_mb < critical_threshold_mb
        should_execute = (batch_index % max(1, batch_clear) == 0)

        if bypass_all and not is_critical:
            print("[SW-H3MultiRef] 显存清理跳过（bypass_all，可用 %.0fMB）" % available_mb, flush=True)
            return False
        if not should_execute and not is_critical:
            print("[SW-H3MultiRef] 显存清理跳过（index=%d %% %d，可用 %.0fMB）"
                  % (batch_index, batch_clear, available_mb), flush=True)
            return False
        if is_critical:
            print("[SW-H3MultiRef] ⚠ 系统内存危险！可用仅 %.0fMB < %dMB，强制清理"
                  % (available_mb, critical_threshold_mb), flush=True)

        if offload_model:
            comfy.model_management.unload_all_models()
        if offload_cache:
            # = DocKr 的 force_memory_cleanup()
            gc.collect()
            gc.collect()
            gc.collect()
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
            except Exception:
                pass
            try:
                comfy.model_management.soft_empty_cache()
            except Exception:
                pass
            try:
                from server import PromptServer

                PromptServer.instance.prompt_queue.set_flag("free_memory", True)
            except Exception:
                pass
            executed = True
        print("[SW-H3MultiRef] 显存清理完成 [卸载模型: %s, 清空缓存: %s, 可用 %.0fMB]"
              % (offload_model, offload_cache, available_mb), flush=True)
    except Exception as exc:
        print("[SW-H3MultiRef] 显存清理失败：%s" % exc, flush=True)
    return executed


def _basic_sample(model, conditioning, latent, seed, steps, sampler_name, scheduler, denoise):
    """等价 RandomNoise + KSamplerSelect + BasicScheduler + BasicGuider + SamplerCustomAdvanced。"""
    from comfy_extras.nodes_custom_sampler import (
        BasicGuider,
        BasicScheduler,
        KSamplerSelect,
        RandomNoise,
        SamplerCustomAdvanced,
    )

    sampler = KSamplerSelect.execute(sampler_name).result[0]
    sigmas = BasicScheduler.execute(model, scheduler, int(steps), float(denoise)).result[0]
    guider = BasicGuider.execute(model, conditioning).result[0]
    noise = RandomNoise.execute(int(seed)).result[0]
    output = SamplerCustomAdvanced.execute(noise, guider, sampler, sigmas, latent).result
    return output[0]


def _decode_latent(latent, vae, audio_vae):
    """VAE 解码 -> (IMAGE 帧, AUDIO)。"""
    from comfy_extras.nodes_audio import VAEDecodeAudio
    from nodes import VAEDecode

    # ⚠ VAEDecode 是全 ComfyUI 里唯一不走 io.Schema 的老式节点：
    #   FUNCTION = "decode"，只有实例方法 .decode(self, vae, samples)，没有 classmethod execute。
    #   （BasicScheduler / KSamplerSelect / BasicGuider / RandomNoise /
    #     SamplerCustomAdvanced / VAEDecodeAudio / CreateVideo / MiniMaxH3ReferenceToVideo
    #     都经 io.Schema 包装过，一律有 .execute。）
    # 写死 VAEDecode().decode() 会在未来版本（若哪天它也升级成 V3）崩，所以两路都留。
    if hasattr(VAEDecode, "execute"):
        images = VAEDecode.execute(vae, latent).result[0]
    else:
        images = VAEDecode().decode(vae=vae, samples=latent)[0]
    audio = None
    if audio_vae is not None:
        try:
            audio = VAEDecodeAudio.execute(audio_vae, latent).result[0]
        except Exception as exc:
            print("[SW-H3MultiRef] 音轨解码失败（这一段将没有声音）：%s" % exc, flush=True)
    return images, audio


def _build_video(images, fps, audio):
    """等价 CreateVideo（04-7 参数：fps=24, bit_depth=8, sRGB, codec=none）。"""
    from comfy_extras.nodes_video import CreateVideo

    return CreateVideo.execute(images=images, fps=float(fps), audio=audio,
                               bit_depth=8, color_space="sRGB", codec="none").result[0]


# --------------------------------------------------------------------------- #
# 段位数据解析
# --------------------------------------------------------------------------- #
def _parse_segments(raw):
    """把面板 JSON 解析成 [{index, prompt, seconds, ...}]（已校验、已去空）。"""
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(str(raw or "{}"))
        except Exception as exc:
            raise ValueError("段位数据不是合法 JSON：%s" % exc) from exc

    if isinstance(data, dict) and data.get("type") == SEGMENTS_TYPE and "items" in data:
        return list(data["items"])            # 上游连线直传

    segments = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(segments, list):
        raise ValueError("段位数据里没有 segments 列表")

    cleaned = []
    for order, item in enumerate(segments[:SEG_SLOTS], start=1):
        if not isinstance(item, dict):
            continue
        images = []
        for entry in (item.get("images") or [])[:MAX_IMAGES]:
            name = entry.get("file") if isinstance(entry, dict) else entry
            if str(name or "").strip():
                images.append(str(name).strip())
        audios = []
        for entry in (item.get("audios") or [])[:MAX_AUDIOS]:
            name = entry.get("file") if isinstance(entry, dict) else entry
            if str(name or "").strip():
                audios.append(str(name).strip())

        video = item.get("video")
        video = str(video).strip() if isinstance(video, str) else ""
        options = item.get("videoOpts") if isinstance(item.get("videoOpts"), dict) else {}

        cleaned.append({
            "order": order,
            "video": video,
            "video_opts": {
                "force_rate": _as_float(options.get("force_rate"), 0.0),
                "frame_load_cap": _as_int(options.get("frame_load_cap"), 0),
                "skip_first_frames": _as_int(options.get("skip_first_frames"), 0),
                "select_every_nth": max(1, _as_int(options.get("select_every_nth"), 1)),
                "custom_width": _as_int(options.get("custom_width"), 0),
                "custom_height": _as_int(options.get("custom_height"), 0),
                "format": str(options.get("format") or DEFAULT_FORMAT),
                "use_audio": _as_bool(options.get("use_audio"), True),
            },
            "images": images,
            "audios": audios,
            "prompt": str(item.get("prompt") or ""),
            "seconds": _as_float(item.get("seconds"), 0.0),
        })
    return [c for c in cleaned if c["video"] or c["images"] or c["audios"] or c["prompt"].strip()]


# --------------------------------------------------------------------------- #
# 节点 1：多图参考素材输入
# --------------------------------------------------------------------------- #
class SWH3MultiRefSegments:
    """导演台形态的素材输入：小节点 + 点开大面板，每段 1 视频 + 9 图 + 3 音频 + 提示词 + 时长。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "段位数据": ("STRING", {
                    "multiline": True, "default": DEFAULT_DATA, "dynamicPrompts": False,
                    "tooltip": "由「打开素材面板」自动维护，不建议手工编辑",
                }),
            },
        }

    RETURN_TYPES = (SEGMENTS_TYPE,)
    RETURN_NAMES = ("段位数据",)
    FUNCTION = "build"
    CATEGORY = "SW/海螺H3"
    DESCRIPTION = ("多图参考素材输入（导演台面板）：点节点上的「打开素材面板」，"
                   "逐段选 1 段参考视频（含取帧参数）+ 最多 9 张参考图 + 最多 3 条参考音频，"
                   "填 1 段提示词与使用时长。输出整份段位数据给「多图参考生视频（一体）」节点。")

    def build(self, 段位数据=""):
        items = _parse_segments(段位数据)
        if not items:
            raise ValueError(
                "还没有任何段位：请点节点上的「打开素材面板」，新增一段并选好参考素材。")
        return ({"type": SEGMENTS_TYPE, "version": DATA_VERSION,
                 "count": len(items), "items": items},)

    @classmethod
    def IS_CHANGED(cls, 段位数据=""):
        import hashlib

        return hashlib.sha256(str(段位数据).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 节点 2：多图参考生视频（一体）
# --------------------------------------------------------------------------- #
class SWH3RefToVideo:
    """多图参考生视频一体节点：除模型 / 加速 / 素材外全部内置。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "H3 权重（可先串 LoRA / SageAttention / BlockSparse / LoRA堆叠）"}),
                "clip": ("CLIP", {"tooltip": "Qwen3-VL 文本编码器（参考素材要走它编码）"}),
                "vae": ("VAE", {"tooltip": "H3 video VAE"}),
                "audio_vae": ("VAE", {"tooltip": "H3 audio VAE；不接则参考音频无法编码、成片静音"}),
                "段位数据": (SEGMENTS_TYPE, {"tooltip": "接「多图参考素材输入」的输出"}),

                "宽": ("INT", {"default": 736, "min": 32, "max": 8192, "step": 32,
                               "tooltip": "成片宽度（32 的倍数）。04-7 原工作流 = 736"}),
                "高": ("INT", {"default": 1280, "min": 32, "max": 8192, "step": 32,
                               "tooltip": "成片高度（32 的倍数）。04-7 原工作流 = 1280"}),
                "默认时长": ("FLOAT", {"default": 7.3, "min": 0.2, "max": 30.0, "step": 0.1,
                                       "tooltip": ("某段既没填使用时长、也没有参考视频时的兜底秒数。"
                                                   "对应 04-7 工作流里的 132 Float(Duration)（7.3s = 175 帧）。"
                                                   "正常每段都会在面板里填使用时长，这里只是兜底")}),

                "参考图尺寸": (["match", "max"], {"default": "match",
                                              "tooltip": "match = 按成片面积等比缩（推荐）；max = 2048 短边，一致性更好但慢很多"}),
                "参考强度": ("FLOAT", {"default": 0.999, "min": 0.0, "max": 1.0, "step": 0.001,
                                       "tooltip": "等价 SW_H3ConditionStrength 的 visual_strength"}),
                "音频强度": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.001,
                                       "tooltip": "等价 SW_H3ConditionStrength 的 audio_strength"}),

                # 不走 control_after_generate：节点自己带「递增种子」，
                # 少一个隐藏 widget 也少一次 widgets_values 错位的机会。
                "种子": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF,
                                 "tooltip": "第 1 段的采样种子；配合「递增种子」自动往后排"}),
                "递增种子": ("BOOLEAN", {"default": True, "tooltip": "第 N 段用 种子 + (N-1)"}),
                "步数": ("INT", {"default": 6, "min": 1, "max": 10000,
                                 "tooltip": "04-7 武戏版 = 6"}),
                "采样器": (comfy.samplers.KSampler.SAMPLERS, {"default": "euler"}),
                "调度器": (comfy.samplers.KSampler.SCHEDULERS, {"default": "beta",
                                                                "tooltip": "★ beta = H3 官方 BasicScheduler 的调度器，建议保持"}),
                "降噪": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),

                "帧率": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0, "step": 1.0,
                                   "tooltip": "成片帧率，H3 固定 24"}),
                "卸载CLIP": ("BOOLEAN", {"default": True,
                                        "tooltip": "每段编码完就把 Qwen3-VL 卸出显存（等价 SW_H3FreeTextEncoder）"}),
                "只生成第几段": ("INT", {"default": 0, "min": 0, "max": SEG_SLOTS, "step": 1,
                                         "tooltip": "0 = 生成全部段；填 N = 只生成第 N 段（快速试跑）"}),

                # ★ 速度关键：喂给官方 ref2va 的参考视频帧短边。追加在末尾，
                #   不动前 15 个控件的位置（老工作流加载不错位）。
                "参考视频短边": ("INT", {"default": 352, "min": 0, "max": 4096, "step": 16,
                                        "tooltip": ("参考视频帧的短边像素（只缩不放）。"
                                                    "352 = 你 04-7 工作流同款（SW-loadvideo + Resize 352x640）："
                                                    "官方节点对小于标称画布（768 短边）的参考帧保持原尺寸，"
                                                    "并整段送 VAE 编码——768 短边的编码量是 352 的 4.58 倍，"
                                                    "这正是插件曾比 04-7 工作流慢 3 倍+ 的根因。"
                                                    "填 0 = 不限制（按格式的像素上限，慢）。"
                                                    "面板里填了自定义尺寸的段不受此影响")}),
            },
        }

    RETURN_TYPES = ("VIDEO", "IMAGE", "AUDIO", "INT", "INT", "FLOAT", "STRING")
    RETURN_NAMES = ("video", "画面", "音轨", "段数", "成片帧数", "成片秒数", "报告")
    FUNCTION = "generate"
    CATEGORY = "SW/海螺H3"
    DESCRIPTION = ("海螺 H3 多图参考生视频（一体）：内置官方 ref2va 条件编码、条件强度、"
                   "CLIP 卸载、无 CFG 采样（euler/beta）、VAE 解码、音轨拼接、CreateVideo。"
                   "每段 1 参考视频 + ≤9 参考图 + ≤3 参考音频 + 提示词 + 时长，逐段生成后按帧拼接。")

    # ------------------------------------------------------------------ #
    def generate(self, model, clip, vae, audio_vae, 段位数据,
                 宽=736, 高=1280, 默认时长=7.3,
                 参考图尺寸="match", 参考强度=0.999, 音频强度=1.0,
                 种子=0, 递增种子=True, 步数=6, 采样器="euler", 调度器="beta", 降噪=1.0,
                 帧率=24.0, 卸载CLIP=True, 只生成第几段=0, 参考视频短边=352):
        import time

        width = max(32, _as_int(宽, 736) // 32 * 32)
        height = max(32, _as_int(高, 1280) // 32 * 32)
        默认时长 = _as_float(默认时长, 7.3)

        segments = _parse_segments(段位数据)
        if not segments:
            raise ValueError("段位数据是空的：请在「多图参考素材输入」面板里至少配好一段。")

        only = _as_int(只生成第几段, 0)
        if only > 0:
            if only > len(segments):
                raise ValueError("只生成第几段 = %d 越界：面板里只有 %d 段。" % (only, len(segments)))
            todo = [(only, segments[only - 1])]
        else:
            todo = list(enumerate(segments, start=1))

        all_frames, all_audios, report = [], [], []
        total = len(todo)

        for done, (index, seg) in enumerate(todo, start=1):
            print("[SW-H3MultiRef] 第 %d/%d 段（面板第 %d 段）开始…" % (done, total, index), flush=True)
            t_seg = time.perf_counter()

            t0 = time.perf_counter()
            images, video_frames, video_audio = self._load_segment_materials(index, seg, 参考视频短边)
            t_mat = time.perf_counter() - t0

            # ---- 时长 / 帧数 ----
            if seg["seconds"] > 0:
                length = _seconds_to_length(seg["seconds"])
                length_src = "面板时长 %.2fs" % seg["seconds"]
            elif video_frames is not None and int(video_frames.shape[0]) >= FRAME_BASE:
                length = _align_frame_count_floor(int(video_frames.shape[0]))
                length_src = "跟随参考视频 %d 帧" % length
            else:
                length = _seconds_to_length(默认时长)
                length_src = "默认时长 %.2fs" % 默认时长

            # ---- 官方 ref2va 条件编码（内部直调，行为与 04-7 完全一致）----
            ref_images, ref_videos, ref_video_audios, ref_audios = {}, {}, {}, {}
            for i, image in enumerate(images):
                ref_images["ref_image_%d" % i] = image
            if video_frames is not None:
                ref_videos["ref_video_0"] = video_frames
                if video_audio is not None:
                    ref_video_audios["ref_video_audio_0"] = video_audio
            for i, audio in enumerate(seg["_audios"]):
                ref_audios["ref_audio_%d" % i] = audio

            t0 = time.perf_counter()
            try:
                conditioning, latent = MiniMaxH3ReferenceToVideo.execute(
                    clip=clip, prompt=seg["prompt"], width=width, height=height, length=length,
                    ref_image_size=参考图尺寸, vae=vae, audio_vae=audio_vae,
                    ref_images=ref_images or None, ref_videos=ref_videos or None,
                    ref_video_audios=ref_video_audios or None, ref_audios=ref_audios or None,
                ).result[:2]
            except Exception as exc:
                raise RuntimeError(
                    "第 %d 段条件编码失败（视频=%s，图 %d 张，音频 %d 条）：%s"
                    % (index, seg["video"] or "无", len(images), len(seg["_audios"]), exc)) from exc
            t_enc = time.perf_counter() - t0

            # ---- 顺序严格照 04-7：136 → 196(FreeTextEncoder) → 197(ConditionStrength) → 126(Guider)
            t0 = time.perf_counter()
            if 卸载CLIP:
                _free_text_encoder(clip)
            t_free = time.perf_counter() - t0

            import node_helpers

            conditioning = node_helpers.conditioning_set_values(conditioning, {
                "minimax_visual_cond_noise_aug": float(参考强度),
                "minimax_audio_cond_noise_aug": float(音频强度),
            })

            # ---- 采样 ----
            seed = (_as_int(种子, 0) + (index - 1)) if 递增种子 else _as_int(种子, 0)
            t0 = time.perf_counter()
            sampled = _basic_sample(model, conditioning, latent, seed, 步数,
                                    采样器, 调度器, 降噪)
            t_sample = time.perf_counter() - t0

            # ---- 显存清理（04-7 的 181 号节点就卡在采样和解码之间，无条件执行）----
            t0 = time.perf_counter()
            _vram_cleanup(batch_index=done, batch_clear=1, bypass_all=False,
                          offload_model=False, offload_cache=True, critical_threshold_mb=7000)
            t_clean = time.perf_counter() - t0

            # ---- 解码 ----
            t0 = time.perf_counter()
            frames, audio = _decode_latent(sampled, vae, audio_vae)
            t_decode = time.perf_counter() - t0
            frame_count = int(frames.shape[0])
            all_frames.append(frames)
            all_audios.append(audio)
            vinfo = seg.get("_video_info")
            video_desc = "无"
            if vinfo:
                video_desc = "%dx%d·%d帧" % (vinfo["loaded_width"], vinfo["loaded_height"],
                                             vinfo["loaded_frame_count"])
            report.append("第 %d 段：%d 帧（%.2fs，%s）· %d 图 / %d 音频 / 视频 %s · seed %d"
                          % (index, frame_count, frame_count / float(帧率), length_src,
                             len(images), len(seg["_audios"]), video_desc, seed))
            print("[SW-H3MultiRef] 第 %d 段完成：%d 帧 | 计时 素材 %.1fs / 编码 %.1fs / "
                  "卸CLIP %.1fs / 采样 %.1fs / 清理 %.1fs / 解码 %.1fs / 合计 %.1fs"
                  % (index, frame_count, t_mat, t_enc, t_free, t_sample, t_clean,
                     t_decode, time.perf_counter() - t_seg), flush=True)

        t0 = time.perf_counter()
        frames = torch.cat(all_frames, dim=0) if len(all_frames) > 1 else all_frames[0]
        audio = _merge_audio(all_audios)
        video = _build_video(frames, 帧率, audio)
        t_build = time.perf_counter() - t0

        total_frames = int(frames.shape[0])
        total_seconds = total_frames / float(帧率)
        header = "合计 %d 段 · %d 帧 · %.2f 秒 · 画布 %dx%d · %d 步 %s/%s · 封装 %.1fs" % (
            len(todo), total_frames, total_seconds, width, height,
            _as_int(步数, 6), 采样器, 调度器, t_build)
        text = header + "\n" + "\n".join(report) + (
            "\n（只生成了第 %d 段，未跑的段不计入）" % only if only > 0 else "")
        print("[SW-H3MultiRef] " + header, flush=True)

        return (video, frames, audio, len(todo), total_frames, total_seconds, text)

    # ------------------------------------------------------------------ #
    def _load_segment_materials(self, index, seg, short_edge=0):
        """加载一段的素材：返回（参考图列表, 参考视频帧 or None, 参考视频音轨 or None）。

        short_edge：参考视频帧的短边像素（只缩不放）；面板里填了 custom_width/height 时以面板为准。
        """
        images = [
            _load_image_file(_safe_input_path(name, IMAGE_EXTENSIONS))
            for name in seg["images"]
        ]

        video_frames, video_audio = None, None
        seg["_video_info"] = None
        if seg["video"]:
            opts = seg["video_opts"]
            spec = LOAD_FORMATS.get(opts["format"], LOAD_FORMATS[DEFAULT_FORMAT])
            path = _safe_input_path(seg["video"], VIDEO_EXTENSIONS)
            force_rate = opts["force_rate"] or spec.get("target_rate", 24)
            # 只取真正会被用到的帧数：填了使用时长就按该时长，否则按参考视频上限（30s）。
            # 官方 execute 里本来就会 frames[:length] 截掉，多取纯属浪费内存。
            wanted = _seconds_to_length(seg["seconds"]) if seg["seconds"] > 0 else MAX_REF_FRAMES
            user_cap = _as_int(opts["frame_load_cap"], 0)
            cap = min(wanted, user_cap) if user_cap > 0 else wanted
            video_frames, video_info = _decode_video_file(
                path,
                force_rate=force_rate,
                frame_load_cap=cap,
                skip_first_frames=opts["skip_first_frames"],
                select_every_nth=opts["select_every_nth"],
                custom_width=opts["custom_width"],
                custom_height=opts["custom_height"],
                downscale_ratio=spec.get("downscale_ratio", 32),
                max_pixels=spec.get("max_pixels", 0),
                short_edge=short_edge,
            )
            seg["_video_info"] = video_info
            if opts["use_audio"]:
                try:
                    video_audio = _decode_audio_file(path)
                except Exception as exc:
                    print("[SW-H3MultiRef] 第 %d 段视频原声提取失败（忽略）：%s" % (index, exc), flush=True)
                    video_audio = None

        audios = []
        for name in seg["audios"]:
            decoded = _decode_audio_file(_safe_input_path(name, AUDIO_EXTENSIONS))
            if decoded is not None:
                audios.append(decoded)
        seg["_audios"] = audios

        return images, video_frames, video_audio


NODE_CLASS_MAPPINGS = {
    "SWH3MultiRefSegments": SWH3MultiRefSegments,
    "SWH3RefToVideo": SWH3RefToVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SWH3MultiRefSegments": "SW 海螺H3·多图参考素材输入",
    "SWH3RefToVideo": "SW 海螺H3·多图参考生视频（一体）",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
