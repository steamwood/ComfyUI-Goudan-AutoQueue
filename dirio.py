"""SeedVR2 目录读写：从任意路径（文件夹或单张图片）逐张读图，放大后存回 <原目录>/SeedVR2/。"""
import os
import re
import numpy as np
import torch
from PIL import Image, ImageOps

from .browse import clean_path
from .saving import DONE_EXTS, EXTENSIONS, OUTPUT_TYPE, legacy_settings, save_image, to_pil

EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
TAG = "[SeedVR2 目录读写]"


def _clean(p):
    return clean_path(p)


def _collect(path):
    path = _clean(path)
    if not path:
        raise ValueError("请填写图片文件夹或单张图片的路径")
    if os.path.isfile(path):
        if not path.lower().endswith(EXTS):
            raise ValueError(f"不是支持的图片格式：{path}")
        return [path]
    if os.path.isdir(path):
        files = sorted(
            os.path.join(path, f) for f in os.listdir(path)
            if f.lower().endswith(EXTS) and os.path.isfile(os.path.join(path, f))
        )
        if not files:
            raise ValueError(f"文件夹里没有图片：{path}")
        return files
    raise ValueError(f"路径不存在：{path}")


def _done(out_dir, stem, suffix):
    """输出目录里已经有「原文件名+后缀」的结果（png/jpg/jpeg/webp 任意一种，含 _001 这类重名编号）就算做完。"""
    if not os.path.isdir(out_dir):
        return False
    key = (stem + suffix).lower()
    pattern = re.compile(re.escape(key) + r"(_\d+)*")
    for f in os.listdir(out_dir):
        name, ext = os.path.splitext(f.lower())
        if ext in DONE_EXTS and pattern.fullmatch(name):
            return True
    return False


def _has_alpha(img):
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        return True
    return img.mode == "P" and "transparency" in img.info


class SeedVR2DirLoad:
    """逐张模式：每运行一次只读一张还没放大过的图。批量次数设成 ≥ 图片张数即可，做完的会自动跳过。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "路径": ("STRING", {"default": "", "multiline": False,
                     "tooltip": "粘贴图片文件夹路径（批量，逐张处理）或单张图片路径，Windows「复制文件地址」带的引号会自动去掉。也可以点节点下面的「选择文件夹…」「选择图片…」按钮在系统窗口里选。"}),
            "后缀": ("STRING", {"default": "_SeedVR2", "tooltip": "输出文件名 = 原文件名 + 后缀"}),
            "自动排队": ("BOOLEAN", {"default": True, "tooltip": "开着时，点一次运行会自动把剩下没放大的张数排进队列，仍然逐张处理"}),
        }, "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("图像", "文件名", "输出目录", "源文件")
    OUTPUT_TOOLTIPS = ("原图（带透明通道的图会保留透明）", "原文件名 + 后缀", "<原目录>/SeedVR2", "原图完整路径（保存节点用它读原图的 EXIF/ICC）")
    FUNCTION = "load"
    CATEGORY = "SeedVR2 目录读写"

    @classmethod
    def IS_CHANGED(cls, **kw):
        import uuid
        return uuid.uuid4().hex  # 每次运行都重新挑下一张，不吃缓存

    def load(self, 路径, 后缀, 自动排队=True, prompt=None, extra_pnginfo=None, unique_id=None):
        files = _collect(路径)
        single = len(files) == 1 and os.path.isfile(_clean(路径))
        todo = files if single else [
            f for f in files
            if not _done(os.path.join(os.path.dirname(f), "SeedVR2"), os.path.splitext(os.path.basename(f))[0], 后缀)
        ]
        if not todo:
            print(f"[SeedVR2 目录读写] 全部 {len(files)} 张都已放大完成，本次跳过")
            try:
                try:
                    from comfy_execution.graph_utils import ExecutionBlocker
                except ImportError:
                    from comfy_execution.graph import ExecutionBlocker
                b = ExecutionBlocker(None)
                return (b, b, b, b)
            except ImportError:
                raise RuntimeError(f"全部 {len(files)} 张都已放大完成")
        if 自动排队 and not single and prompt:
            from .autoqueue import fingerprint, is_duplicate, blocked_outputs, make_copy, queue_copies
            fp = fingerprint(prompt, extra_pnginfo, unique_id, ("自动排队",))
            if is_duplicate(fp, extra_pnginfo):
                print("[SeedVR2 目录读写] 这个文件夹已经有排好的任务在队列里（重复点了运行），这次整次跳过。换了文件夹的新任务会正常排队。")
                return blocked_outputs(4)
            if len(todo) > 1:
                copies = [make_copy(prompt, extra_pnginfo, k, unique_id, {"自动排队": False}) for k in range(1, len(todo))]
                queue_copies(copies, extra_pnginfo, "SeedVR2 目录读写", fp, prompt)
                print(f"[SeedVR2 目录读写] 还剩 {len(todo)} 张，已把后面 {len(todo) - 1} 次排进队列（逐张处理）")
        f = todo[0]
        img = ImageOps.exif_transpose(Image.open(f))
        if img.mode == "I":
            img = img.point(lambda i: i * (1 / 255))
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")
        arr = np.array(img).astype(np.float32) / 255.0
        stem = os.path.splitext(os.path.basename(f))[0]
        done_n = len(files) - len(todo) + 1
        print(f"[SeedVR2 目录读写] 第 {done_n}/{len(files)} 张：{f}")
        return (torch.from_numpy(arr)[None,], stem + 后缀, os.path.join(os.path.dirname(f), "SeedVR2"), f)


class SeedVR2DirSave:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "图像": ("IMAGE",),
            "文件名": ("STRING", {"forceInput": True}),
            "输出目录": ("STRING", {"forceInput": True}),
        }, "optional": {
            "输出设置": (OUTPUT_TYPE, {"tooltip": "连「输出设置」。不连时和以前一样存 PNG、不写元数据。"}),
            "源文件": ("STRING", {"forceInput": True, "tooltip": "连读图节点的 源文件（用来保留原图 ICC/EXIF）"}),
        }, "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"}}

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "save"
    CATEGORY = "SeedVR2 目录读写"

    def save(self, 图像, 文件名, 输出目录, 输出设置=None, 源文件=None, prompt=None, extra_pnginfo=None):
        settings = dict(legacy_settings())
        settings.update(输出设置 or {})
        ext = EXTENSIONS.get(settings["格式"], ".png")
        os.makedirs(输出目录, exist_ok=True)
        ui = []
        for i, im in enumerate(图像):
            pil = to_pil(im, settings["格式"])
            stem = 文件名 + (f"_{i+1}" if len(图像) > 1 else "")
            path = os.path.join(输出目录, stem + ext)
            n = 1
            while os.path.exists(path):
                path = os.path.join(输出目录, f"{stem}_{n:03d}{ext}")
                n += 1
            save_image(pil, path, settings, 源文件, prompt, extra_pnginfo)
            print(f"{TAG} 已保存 {path}（{pil.width}x{pil.height}）")
            try:
                import folder_paths
                tmp = folder_paths.get_temp_directory()
                os.makedirs(tmp, exist_ok=True)
                pv = pil.copy()
                pv.thumbnail((1024, 1024))
                tname = f"seedvr2_dirio_{os.getpid()}_{abs(hash(path))}.png"
                pv.save(os.path.join(tmp, tname), compress_level=1)
                ui.append({"filename": tname, "subfolder": "", "type": "temp"})
            except Exception:
                pass
        return {"ui": {"images": ui}}
