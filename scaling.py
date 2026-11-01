"""缩放设置：按「倍数」或「长边像素」决定 SeedVR2 处理尺寸和最终输出尺寸。

流程（每张图单独计算）：
1. 「SeedVR2 前缩放」把原图缩放到 SeedVR2 要处理的尺寸（宽高对齐到 8 的倍数）。
   - 放大：SeedVR2 尺寸 = 原图 × 有效倍数。
   - 缩小（长边模式，目标比原图小）：SeedVR2 按 1 倍只做修复，之后再缩小。
2. SeedVR2 处理。
3. 「最终尺寸」用高质量 lanczos 精确缩放到目标尺寸（长边正好等于设定像素，短边按原比例四舍五入）。
   缩小且勾了「缩小时跳过SeedVR2」时，直接从原图缩放，SeedVR2 那一串节点整串不执行（懒加载输入不请求）。
"""
import numpy as np
import torch
from PIL import Image

SCALE_TYPE = "GOUDAN_SCALE"
MODE_MULT = "倍数"
MODE_LONG = "长边"
KEY_MODE = "模式"
KEY_MULT = "倍数"
KEY_LONG = "长边像素"
KEY_SKIP = "缩小时跳过SeedVR2（只缩放）"
ALIGN = 8
TAG = "[缩放设置]"


def _round(x):
    return int(x + 0.5)


def _align(x):
    """向上对齐到 8 的倍数：SeedVR2 出来的图不小于最终尺寸，最后一步只会轻微缩小，不会再放大。"""
    return max(ALIGN, -(-_round(x) // ALIGN) * ALIGN)


def default_settings():
    return {KEY_MODE: MODE_MULT, KEY_MULT: 1.5, KEY_LONG: 3840, KEY_SKIP: False}


def plan(width, height, settings):
    """返回这张图的处理计划：SeedVR2 尺寸、最终尺寸、是否缩小、是否跳过 SeedVR2。"""
    s = dict(default_settings())
    s.update(settings or {})
    if s[KEY_MODE] == MODE_LONG:
        target = max(1, int(s[KEY_LONG]))
        longest = max(width, height)
        if width >= height:
            final_w = target
            final_h = max(1, _round(height * target / width))
        else:
            final_h = target
            final_w = max(1, _round(width * target / height))
        downscale = target < longest
        seed_scale = 1.0 if downscale else target / longest
    else:
        mult = max(1.0, float(s[KEY_MULT]))
        final_w = max(1, _round(width * mult))
        final_h = max(1, _round(height * mult))
        downscale = False
        seed_scale = mult
    skip = bool(downscale and s[KEY_SKIP])
    return {
        "seed_w": _align(width * seed_scale),
        "seed_h": _align(height * seed_scale),
        "final_w": final_w,
        "final_h": final_h,
        "seed_scale": seed_scale,
        "downscale": downscale,
        "skip_seedvr2": skip,
    }


def _resize_channel(arr, width, height):
    pil = Image.fromarray(np.ascontiguousarray(arr, dtype=np.float32))
    return np.asarray(pil.resize((width, height), Image.Resampling.LANCZOS), dtype=np.float32)


def as_frames(images):
    """5 维（批, 帧, 高, 宽, 通道）的视频张量摊平成普通 IMAGE（帧, 高, 宽, 通道）。"""
    if images.ndim == 5:
        return images.reshape(-1, images.shape[-3], images.shape[-2], images.shape[-1])
    return images


def lanczos(images, width, height):
    """高质量 lanczos 缩放（32 位浮点逐通道，不先转 8 位；带透明通道时按预乘 alpha 缩放，边缘不发黑）。"""
    images = as_frames(images)
    if images.shape[1] == height and images.shape[2] == width:
        return images
    out = []
    for im in images:
        arr = im.detach().cpu().float().numpy()
        channels = arr.shape[2]
        if channels == 4:
            alpha = arr[..., 3:4]
            work = np.concatenate([arr[..., :3] * alpha, alpha], axis=2)
        else:
            work = arr
        res = np.stack([_resize_channel(work[..., k], width, height) for k in range(channels)], axis=2)
        if channels == 4:
            new_alpha = np.clip(res[..., 3:4], 0.0, 1.0)
            safe = np.where(new_alpha > 1e-6, new_alpha, 1.0)
            rgb = np.where(new_alpha > 1e-6, res[..., :3] / safe, 0.0)
            res = np.concatenate([rgb, new_alpha], axis=2)
        out.append(torch.from_numpy(np.clip(res, 0.0, 1.0)))
    return torch.stack(out).to(images.device, images.dtype)


class GoudanScaleSettings:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            KEY_MODE: ([MODE_MULT, MODE_LONG], {"default": MODE_MULT, "tooltip": "倍数：最终尺寸 = 原图 × 倍数。长边：每张图的长边都变成「长边像素」，保持原比例；比原图大就放大，比原图小就缩小。"}),
            KEY_MULT: ("FLOAT", {"default": 1.5, "min": 1.0, "max": 8.0, "step": 0.05, "round": 0.01, "tooltip": "倍数模式用。1 = 同尺寸只做修复/优化，不放大。输出越大越吃显存：倍数大、原图又大时可能显存不够（OOM），出错就调小。"}),
            KEY_LONG: ("INT", {"default": 3840, "min": 64, "max": 16384, "step": 1, "tooltip": "长边模式用：输出图长边正好等于这个像素数，短边按原比例。比原图小时先按 1 倍用 SeedVR2 修复，再用 lanczos 高质量缩小。注意：数值很大时（比如 4K 以上）可能显存不够（OOM），出错就调小（16GB 显卡没实测过上限）。"}),
            KEY_SKIP: ("BOOLEAN", {"default": False, "tooltip": "长边模式、目标比原图小时：开 = 不跑 SeedVR2，只用 lanczos 缩小（很快）；关 = 先 SeedVR2 按 1 倍修复再缩小。放大的图不受影响。"}),
        }}

    RETURN_TYPES = (SCALE_TYPE,)
    RETURN_NAMES = ("缩放设置",)
    OUTPUT_TOOLTIPS = ("连「SeedVR2 前缩放」和「最终尺寸」",)
    FUNCTION = "build"
    CATEGORY = "SeedVR2 目录读写"
    DESCRIPTION = "选倍数或长边像素。每张图按自己的尺寸计算，长边模式下横图竖图都是长边等于设定值。"

    def build(self, **kw):
        s = default_settings()
        for key in (KEY_MODE, KEY_MULT, KEY_LONG, KEY_SKIP):
            if key in kw:
                s[key] = kw[key]
        return (s,)


class GoudanScalePrepare:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "图像": ("IMAGE",),
            "缩放设置": (SCALE_TYPE,),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("送进SeedVR2",)
    FUNCTION = "prepare"
    CATEGORY = "SeedVR2 目录读写"
    DESCRIPTION = "按缩放设置把原图缩放到 SeedVR2 要处理的尺寸（宽高向上对齐到 8 的倍数，lanczos）。缩小时按 1 倍送进去只做修复。透明通道会保留。"

    def prepare(self, 图像, 缩放设置):
        h = int(图像.shape[1])
        w = int(图像.shape[2])
        p = plan(w, h, 缩放设置)
        print(f"{TAG} 原图 {w}x{h} → SeedVR2 处理 {p['seed_w']}x{p['seed_h']} → 最终 {p['final_w']}x{p['final_h']}")
        return (lanczos(图像, p["seed_w"], p["seed_h"]),)


class GoudanScaleFinish:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "原图": ("IMAGE",),
            "缩放设置": (SCALE_TYPE,),
            "SeedVR2结果": ("IMAGE", {"lazy": True}),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("最终图像",)
    FUNCTION = "finish"
    CATEGORY = "SeedVR2 目录读写"
    DESCRIPTION = "把结果用 lanczos 精确缩放到最终尺寸。缩小且勾了「缩小时跳过SeedVR2」时直接用原图缩放，SeedVR2 不执行。"

    def check_lazy_status(self, 原图=None, 缩放设置=None, SeedVR2结果=None):
        if 原图 is None:
            return []
        p = plan(int(原图.shape[2]), int(原图.shape[1]), 缩放设置)
        if p["skip_seedvr2"]:
            return []
        return ["SeedVR2结果"]

    def finish(self, 原图, 缩放设置, SeedVR2结果=None):
        h = int(原图.shape[1])
        w = int(原图.shape[2])
        p = plan(w, h, 缩放设置)
        if p["skip_seedvr2"] or SeedVR2结果 is None:
            print(f"{TAG} 缩小且设置了跳过 SeedVR2：直接从原图 {w}x{h} 缩放到 {p['final_w']}x{p['final_h']}")
            src = 原图
        else:
            src = as_frames(SeedVR2结果)
            if 原图.shape[-1] == 4 and src.shape[-1] == 3:
                alpha = lanczos(原图[..., 3:4].repeat(1, 1, 1, 3), int(src.shape[2]), int(src.shape[1]))[..., :1]
                src = torch.cat([src, alpha.to(src.device, src.dtype)], dim=-1)
        out = lanczos(src, p["final_w"], p["final_h"])
        print(f"{TAG} 最终尺寸 {int(out.shape[2])}x{int(out.shape[1])}")
        return (out,)
