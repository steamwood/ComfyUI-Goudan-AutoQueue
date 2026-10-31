"""SeedVR2 目录读写：从任意路径（文件夹或单张图片）逐张读图，放大后存回 <原目录>/SeedVR2/。"""
import os, hashlib
import numpy as np
import torch
from PIL import Image, ImageOps

EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


def _clean(p):
    p = (p or "").strip().strip('"').strip("'").strip()
    return os.path.normpath(os.path.expanduser(p)) if p else ""


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
    if not os.path.isdir(out_dir):
        return False
    key = (stem + suffix).lower()
    return any(f.lower().startswith(key) and f.lower().endswith(".png") for f in os.listdir(out_dir))


class SeedVR2DirLoad:
    """逐张模式：每运行一次只读一张还没放大过的图。批量次数设成 ≥ 图片张数即可，做完的会自动跳过。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "路径": ("STRING", {"default": "", "multiline": False,
                     "tooltip": "粘贴图片文件夹路径（批量，逐张处理）或单张图片路径。Windows“复制文件地址”带的引号会自动去掉。"}),
            "后缀": ("STRING", {"default": "_SeedVR2", "tooltip": "输出文件名 = 原文件名 + 后缀"}),
            "自动排队": ("BOOLEAN", {"default": True, "tooltip": "开着时，点一次运行会自动把剩下没放大的张数排进队列，仍然逐张处理"}),
        }, "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("图像", "文件名", "输出目录")
    FUNCTION = "load"
    CATEGORY = "SeedVR2 目录读写"

    @classmethod
    def IS_CHANGED(cls, **kw):
        return float("nan")  # 每次运行都重新挑下一张

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
                return (b, b, b)
            except ImportError:
                raise RuntimeError(f"全部 {len(files)} 张都已放大完成")
        if 自动排队 and not single and len(todo) > 1 and prompt:
            from .autoqueue import make_copy, queue_copies
            copies = [make_copy(prompt, extra_pnginfo, k, unique_id, {"自动排队": False}) for k in range(1, len(todo))]
            queue_copies(copies, extra_pnginfo, "SeedVR2 目录读写")
            print(f"[SeedVR2 目录读写] 还剩 {len(todo)} 张，正在把后面 {len(todo) - 1} 次排进队列（逐张处理）")
        f = todo[0]
        img = ImageOps.exif_transpose(Image.open(f))
        if img.mode == "I":
            img = img.point(lambda i: i * (1 / 255))
        img = img.convert("RGB")
        arr = np.array(img).astype(np.float32) / 255.0
        stem = os.path.splitext(os.path.basename(f))[0]
        done_n = len(files) - len(todo) + 1
        print(f"[SeedVR2 目录读写] 第 {done_n}/{len(files)} 张：{f}")
        return (torch.from_numpy(arr)[None,], stem + 后缀, os.path.join(os.path.dirname(f), "SeedVR2"))


class SeedVR2DirSave:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "图像": ("IMAGE",),
            "文件名": ("STRING", {"forceInput": True}),
            "输出目录": ("STRING", {"forceInput": True}),
        }}

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "save"
    CATEGORY = "SeedVR2 目录读写"

    def save(self, 图像, 文件名, 输出目录):
        os.makedirs(输出目录, exist_ok=True)
        ui = []
        for i, im in enumerate(图像):
            arr = np.clip(im.cpu().numpy() * 255.0, 0, 255).astype(np.uint8)
            pil = Image.fromarray(arr)
            stem = 文件名 + (f"_{i+1}" if len(图像) > 1 else "")
            path = os.path.join(输出目录, stem + ".png")
            n = 1
            while os.path.exists(path):
                path = os.path.join(输出目录, f"{stem}_{n:03d}.png"); n += 1
            pil.save(path, compress_level=4)
            print(f"[SeedVR2 目录读写] 已保存 {path}")
            try:
                import folder_paths
                tmp = folder_paths.get_temp_directory(); os.makedirs(tmp, exist_ok=True)
                pv = pil.copy(); pv.thumbnail((1024, 1024))
                tname = f"seedvr2_dirio_{os.getpid()}_{abs(hash(path))}.png"
                pv.save(os.path.join(tmp, tname), compress_level=1)
                ui.append({"filename": tname, "subfolder": "", "type": "temp"})
            except Exception:
                pass
        return {"ui": {"images": ui}}


