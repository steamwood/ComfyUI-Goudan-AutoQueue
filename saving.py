"""输出设置：格式（jpg/png/webp）、质量、是否保留元数据。真正写文件的函数也在这里。

元数据规则：
- 保留元数据 = 关：不写 ComfyUI 的 prompt/workflow，也不带原图的 EXIF（含 GPS、相机信息）、XMP 等。
  只有原图自带「不是 sRGB」的 ICC 色彩配置（例如 Display P3、Adobe RGB）时才保留 ICC：
  像素数值是按那个色彩空间存的，去掉 ICC 颜色会变（发灰/偏色）。ICC 只描述颜色，不含个人信息。
  sRGB 或没有 ICC 的图不写 ICC（没有 ICC 时看图软件本来就按 sRGB 显示，写了也是多余）。
- 保留元数据 = 开：PNG 写 prompt/workflow 文本块（和 ComfyUI 自带保存一样，可以拖回 ComfyUI）；
  JPG/WebP 按 ComfyUI 的做法写进 EXIF（0x0110 放 prompt，0x010F 起往下放 workflow 等）。
  同时尽量保留原图 EXIF（拍摄时间、GPS 等，方向改成 1，因为像素已经转正）和 ICC。
  注意：EXIF 里 0x010F/0x0110 本来是相机厂商/型号，会被 ComfyUI 的数据占用。
"""
import io
import json
import os
import re

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

OUTPUT_TYPE = "GOUDAN_OUTPUT"
FORMATS = ("jpg", "png", "webp")
EXTENSIONS = {"jpg": ".jpg", "png": ".png", "webp": ".webp"}
DONE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
PNG_COMPRESS = 4
JPEG_EXIF_LIMIT = 65000
EXIF_PROMPT_TAG = 0x0110
EXIF_FIRST_EXTRA_TAG = 0x010F
EXIF_ORIENTATION = 0x0112
EXIF_IFD = 0x8769
INTERNAL_KEY_PREFIX = "goudan_autoqueue"
TAG = "[SeedVR2 目录读写]"
SRGB_NAME = re.compile(r"(?<![a-z])s ?rgb(?![a-z])")


def legacy_settings():
    """没连「输出设置」时和 1.0.x 一样：PNG、不写元数据。"""
    return {"格式": "png", "质量": 95, "保留元数据": False}


class GoudanOutputSettings:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "格式": (list(FORMATS), {"default": "jpg", "tooltip": "jpg 体积小；png 无损；webp 体积更小、支持透明。jpg 不支持透明，透明部分会铺成白色。"}),
            "质量": ("INT", {"default": 95, "min": 1, "max": 100, "step": 1, "tooltip": "jpg / webp 的压缩质量（越大越清晰、文件越大）。png 是无损的，不用这个值（固定压缩等级 4）。"}),
            "保留元数据": ("BOOLEAN", {"default": False, "tooltip": "关：不写工作流/提示词，也去掉原图的 EXIF（含 GPS 定位、相机信息）。开：写入 ComfyUI 工作流（可拖回 ComfyUI），并尽量保留原图 EXIF。"}),
        }}

    RETURN_TYPES = (OUTPUT_TYPE,)
    RETURN_NAMES = ("输出设置",)
    OUTPUT_TOOLTIPS = ("连「保存到原目录/SeedVR2」的 输出设置",)
    FUNCTION = "build"
    CATEGORY = "SeedVR2 目录读写"
    DESCRIPTION = "保存格式、质量和元数据。改了格式也能断点续跑：已有任意格式（png/jpg/jpeg/webp）的结果都算做完。"

    def build(self, 格式, 质量, 保留元数据):
        fmt = 格式 if 格式 in FORMATS else "jpg"
        return ({"格式": fmt, "质量": int(质量), "保留元数据": bool(保留元数据)},)


def _is_srgb(icc):
    try:
        from PIL import ImageCms
        profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        desc = ImageCms.getProfileDescription(profile) or ""
    except Exception:
        return False
    return SRGB_NAME.search(desc.lower()) is not None


def read_source_meta(path):
    """读原图的 ICC 和 EXIF（EXIF 转成字节，方向改成 1）。读不到就返回 None。"""
    icc = None
    exif = None
    if not path or not os.path.isfile(path):
        return icc, exif
    try:
        with Image.open(path) as src:
            icc = src.info.get("icc_profile") or None
            ex = src.getexif()
            if len(ex):
                if EXIF_ORIENTATION in ex:
                    ex[EXIF_ORIENTATION] = 1
                exif = ex.tobytes()
    except Exception as e:
        print(f"{TAG} 读取原图元数据失败（不影响保存）：{e}")
    return icc, exif


def _clean_extra(extra_pnginfo):
    if not isinstance(extra_pnginfo, dict):
        return {}
    return {k: v for k, v in extra_pnginfo.items() if not str(k).startswith(INTERNAL_KEY_PREFIX)}


def _metadata_disabled():
    try:
        from comfy.cli_args import args
        return bool(getattr(args, "disable_metadata", False))
    except Exception:
        return False


def _exif_with_comfy(base_exif, prompt, extra, drop_workflow=False):
    ex = Image.Exif()
    if base_exif:
        ex.load(base_exif)
    if prompt is not None:
        ex[EXIF_PROMPT_TAG] = "prompt:" + json.dumps(prompt)
    tag = EXIF_FIRST_EXTRA_TAG
    for key, value in extra.items():
        if drop_workflow and key == "workflow":
            continue
        ex[tag] = f"{key}:{json.dumps(value)}"
        tag -= 1
    return ex.tobytes()


def to_pil(tensor, fmt):
    """IMAGE（H,W,C 浮点 0~1）转 PIL。jpg 带透明时铺到白底。"""
    arr = tensor.detach().cpu().float().numpy()
    if arr.shape[-1] == 4 and fmt == "jpg":
        alpha = arr[..., 3:4]
        arr = arr[..., :3] * alpha + (1.0 - alpha)
    if arr.shape[-1] == 1:
        arr = np.repeat(arr, 3, axis=-1)
    data = np.clip(arr * 255.0 + 0.5, 0, 255).astype(np.uint8)
    if data.shape[-1] == 4:
        return Image.fromarray(data, "RGBA")
    return Image.fromarray(data[..., :3], "RGB")


def save_image(pil, path, settings, source_path=None, prompt=None, extra_pnginfo=None):
    fmt = settings["格式"]
    quality = int(settings["质量"])
    keep = bool(settings["保留元数据"])
    icc, src_exif = read_source_meta(source_path)
    if not keep and icc and _is_srgb(icc):
        icc = None
    kwargs = {}
    if icc:
        kwargs["icc_profile"] = icc
    comfy_meta = keep and not _metadata_disabled()
    extra = _clean_extra(extra_pnginfo) if comfy_meta else {}
    if fmt == "png":
        if keep:
            info = PngInfo()
            if comfy_meta and prompt is not None:
                info.add_text("prompt", json.dumps(prompt))
            for key, value in extra.items():
                info.add_text(key, json.dumps(value))
            kwargs["pnginfo"] = info
            if src_exif:
                kwargs["exif"] = src_exif
        pil.save(path, format="PNG", compress_level=PNG_COMPRESS, **kwargs)
        return
    if keep:
        if comfy_meta:
            exif = _exif_with_comfy(src_exif, prompt, extra)
            if fmt == "jpg" and len(exif) > JPEG_EXIF_LIMIT:
                print(f"{TAG} 工作流太大，超过 JPG 的 EXIF 上限（64KB），这张 JPG 只写提示词（prompt），不写工作流")
                exif = _exif_with_comfy(src_exif, prompt, extra, drop_workflow=True)
            if fmt == "jpg" and len(exif) > JPEG_EXIF_LIMIT:
                print(f"{TAG} 提示词也超过 JPG 的 EXIF 上限，这张 JPG 只保留原图 EXIF")
                exif = src_exif
        else:
            exif = src_exif
        if exif:
            kwargs["exif"] = exif
    if fmt == "jpg":
        subsampling = 0 if quality >= 90 else 2
        pil.save(path, format="JPEG", quality=quality, subsampling=subsampling, optimize=True, **kwargs)
        return
    pil.save(path, format="WEBP", quality=quality, method=4, **kwargs)
