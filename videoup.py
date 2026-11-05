"""批量视频放大：按路径（文件夹/单个视频）逐段读视频 → 放大 → 存回 <原目录>/SeedVR2/原文件名_后缀.mp4。

一次运行（一个 prompt）只处理「一个视频的一段」（每段帧数=0 时一段=整个视频）。
每段先存成 <原目录>/SeedVR2/.parts/<文件名_后缀>/00001.mp4（写完才改名，断电不会留下半个文件），
最后一段做完后把所有段无损拼接（不重新编码视频），再把原视频的音轨复制进去，删掉 .parts。
所以：中途停掉/关机，下次再点运行会从没做完的那一段接着做（可续跑）；已经有最终文件的视频整个跳过。
"""
import json
import math
import os
import shutil
from fractions import Fraction

import numpy as np
import torch

from .browse import clean_path

TAG = "[批量视频放大]"
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
OUT_SUB = "SeedVR2"
MODES = ["1 SeedVR2 →1080p", "2 SeedVR2 →1080p →4K", "3 VOSR2 →1080p", "4 VOSR2 →1080p →4K"]
SUFFIX = {MODES[0]: "_SeedVR2_1080p", MODES[1]: "_SeedVR2_4K", MODES[2]: "_VOSR2_1080p", MODES[3]: "_VOSR2_4K"}


def mode_flags(mode, orig4k):
    m = MODES.index(mode) if mode in MODES else 0
    seed = m in (0, 1)
    four = m in (1, 3)
    return seed, not seed, four, bool(four and orig4k), bool(four and not orig4k), SUFFIX[MODES[m]]


def collect(path):
    p = clean_path(path)
    if not p:
        raise ValueError("请填写视频文件夹或单个视频的路径")
    if os.path.isfile(p):
        if not p.lower().endswith(VIDEO_EXTS):
            raise ValueError(f"不是支持的视频格式：{p}")
        return [p], True
    if os.path.isdir(p):
        fs = sorted(os.path.join(p, f) for f in os.listdir(p)
                    if f.lower().endswith(VIDEO_EXTS) and os.path.isfile(os.path.join(p, f)))
        if not fs:
            raise ValueError(f"文件夹里没有视频：{p}")
        return fs, False
    raise ValueError(f"路径不存在：{p}")


def paths_for(src, suffix):
    d = os.path.join(os.path.dirname(src), OUT_SUB)
    name = os.path.splitext(os.path.basename(src))[0] + suffix
    return d, name, os.path.join(d, name + ".mp4"), os.path.join(d, ".parts", name)


def final_done(src, suffix):
    d, name, _, _ = paths_for(src, suffix)
    if not os.path.isdir(d):
        return False
    key = name.lower()
    return any(os.path.splitext(f.lower())[0] == key and f.lower().endswith(VIDEO_EXTS) for f in os.listdir(d))


def probe(src, parts_dir=None):
    """帧数 / 帧率（Fraction）/ 宽高。帧数优先用缓存，其次容器信息，不准就数一遍。"""
    meta = os.path.join(parts_dir, "meta.json") if parts_dir else None
    st = os.stat(src)
    if meta and os.path.isfile(meta):
        try:
            m = json.load(open(meta, encoding="utf-8"))
            if m.get("size") == st.st_size and m.get("mtime") == st.st_mtime:
                return m["frames"], Fraction(m["fps"]), m["w"], m["h"]
        except Exception:
            pass
    import av
    with av.open(src) as c:
        vs = c.streams.video[0]
        fps = vs.average_rate or vs.guessed_rate or Fraction(24)
        w, h = vs.codec_context.width, vs.codec_context.height
        n = 0
        for _ in c.decode(vs):
            n += 1
    if meta:
        os.makedirs(parts_dir, exist_ok=True)
        json.dump({"size": st.st_size, "mtime": st.st_mtime, "frames": n, "fps": str(Fraction(fps)), "w": w, "h": h},
                  open(meta, "w", encoding="utf-8"))
    return n, Fraction(fps), w, h


def read_frames(src, start, count):
    import av
    out = []
    with av.open(src) as c:
        vs = c.streams.video[0]
        for i, fr in enumerate(c.decode(vs)):
            if i < start:
                continue
            if count and i >= start + count:
                break
            out.append(torch.from_numpy(fr.to_ndarray(format="rgb24")))
    if not out:
        raise RuntimeError(f"{TAG} 读不到帧：{src} 从第 {start} 帧")
    return torch.stack(out).float() / 255.0


def read_audio(src):
    """整段原音轨（给预览/兼容用；最终文件直接从原视频复制音轨）。没有音轨返回 None。"""
    try:
        import av
        with av.open(src) as c:
            if not c.streams.audio:
                return None
            a = c.streams.audio[0]
            sr = a.rate
            chunks = [f.to_ndarray() for f in c.decode(a)]
        if not chunks:
            return None
        arr = np.concatenate([x if x.ndim == 2 else x[None] for x in chunks], axis=1).astype(np.float32)
        if np.abs(arr).max() > 1.5:  # 整数采样
            arr = arr / 32768.0
        return {"waveform": torch.from_numpy(arr)[None], "sample_rate": sr}
    except Exception as e:
        print(f"{TAG} 读音频失败（不影响画面）：{e}")
        return None


def todo_units(files, single, suffix, seg):
    """[(源文件, 段号, 总段数)]，按文件、段顺序。已有最终文件 / 已有段文件的跳过。"""
    units = []
    for f in files:
        if not single and final_done(f, suffix):
            continue
        _, _, _, pdir = paths_for(f, suffix)
        n, _, _, _ = probe(f, pdir)
        total = max(1, math.ceil(n / seg)) if seg else 1
        for k in range(total):
            if not os.path.isfile(os.path.join(pdir, f"{k + 1:05d}.mp4")):
                units.append((f, k, total))
    return units


def _blocker(n):
    try:
        from comfy_execution.graph_utils import ExecutionBlocker
    except ImportError:
        from comfy_execution.graph import ExecutionBlocker
    return tuple(ExecutionBlocker(None) for _ in range(n))


class GoudanVideoDirLoad:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "路径": ("STRING", {"default": "", "tooltip": "视频文件夹（批量，逐个视频自动排队）或单个视频路径。引号会自动去掉。"}),
            "模式": (MODES, {"default": MODES[0], "tooltip": "四选一。决定走 SeedVR2 还是 VOSR2、要不要第二段到 4K，以及输出后缀。"}),
            "4K用原模型": ("BOOLEAN", {"default": False, "tooltip": "只对模式 2/4 生效。关=轻量 4x 放大模型+缩到 3840（快、省显存）；开=再跑一次 SeedVR2/VOSR2 到 4K（很慢、显存吃紧）。"}),
            "每段帧数": ("INT", {"default": 48, "min": 0, "max": 100000, "tooltip": "每次运行只放大这么多帧（显存小就调小）。0=整个视频一次做完。段与段之间无缝拼接，可断点续跑。"}),
            "自动排队": ("BOOLEAN", {"default": True, "tooltip": "开：点一次运行，把剩下所有视频/所有段自动排进队列。"}),
        }, "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("IMAGE", "AUDIO", "FLOAT", "STRING", "STRING", "BOOLEAN", "BOOLEAN", "BOOLEAN", "BOOLEAN", "BOOLEAN", "STRING")
    RETURN_NAMES = ("帧", "音频", "帧率", "文件名", "输出目录", "走SeedVR2", "走VOSR2", "做4K", "4K原模型", "4K轻量", "段信息")
    FUNCTION = "load"
    CATEGORY = "SeedVR2 目录读写"

    @classmethod
    def IS_CHANGED(cls, **kw):
        import uuid
        return uuid.uuid4().hex

    def load(self, 路径, 模式, 每段帧数, 自动排队=True, prompt=None, extra_pnginfo=None, unique_id=None, **kw):
        orig4k = kw.get("4K用原模型", False)
        seed, vosr, four, o4, lite, suffix = mode_flags(模式, orig4k)
        files, single = collect(路径)
        units = todo_units(files, single, suffix, 每段帧数)
        if not units:
            print(f"{TAG} 全部 {len(files)} 个视频都已放大完成（{suffix}），本次跳过")
            return _blocker(11)
        if 自动排队 and prompt and len(units) > 1:
            from .autoqueue import fingerprint, is_duplicate, blocked_outputs, make_copy, queue_copies
            fp = fingerprint(prompt, extra_pnginfo, unique_id, ("自动排队",))
            if is_duplicate(fp, extra_pnginfo):
                print(f"{TAG} 这个任务已经在队列里（重复点了运行），这次跳过")
                return blocked_outputs(11)
            copies = (make_copy(prompt, extra_pnginfo, k, unique_id, {"自动排队": False}) for k in range(1, len(units)))
            queue_copies(copies, extra_pnginfo, "批量视频放大", fp, prompt, total=len(units) - 1)
            print(f"{TAG} 还剩 {len(units)} 段，已把后面 {len(units) - 1} 次排进队列")
        src, k, total = units[0]
        d, name, final, pdir = paths_for(src, suffix)
        n, fps, w, h = probe(src, pdir)
        start = k * 每段帧数 if 每段帧数 else 0
        frames = read_frames(src, start, 每段帧数)
        print(f"{TAG} {os.path.basename(src)} 第 {k + 1}/{total} 段（帧 {start}~{start + len(frames) - 1}/{n}，{w}x{h} @ {float(fps):.3f}fps）→ {final}")
        info = json.dumps({"src": src, "part": k, "total": total, "final": final, "parts": pdir, "fps": str(fps)}, ensure_ascii=False)
        audio = read_audio(src) if total == 1 else None
        return (frames, audio, float(fps), name, d, seed, vosr, four, o4, lite, info)


def _encode(frames, path, fps, crf):
    import av
    tmp = path + ".tmp.mp4"
    with av.open(tmp, "w") as c:
        s = c.add_stream("libx264", rate=fps)
        s.width, s.height = frames.shape[2] // 2 * 2, frames.shape[1] // 2 * 2
        s.pix_fmt = "yuv420p"
        s.options = {"crf": str(crf), "preset": "medium"}
        for fr in frames:
            a = (fr[: s.height, : s.width, :3].clamp(0, 1) * 255).round().byte().cpu().numpy()
            for p in s.encode(av.VideoFrame.from_ndarray(a, format="rgb24")):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    os.replace(tmp, path)


def _concat(parts, src, final):
    """视频流无损拼接（不重新编码）+ 原视频音轨直接复制（复制不了就转 AAC）。"""
    import av
    tmp = final + ".tmp.mp4"
    with av.open(tmp, "w") as out:
        with av.open(parts[0]) as first:
            vt = out.add_stream_from_template(first.streams.video[0])
        aud_in = av.open(src)
        at = None
        a_copy = True
        if aud_in.streams.audio:
            ai = aud_in.streams.audio[0]
            try:
                at = out.add_stream_from_template(ai)
            except Exception:
                a_copy = False
                at = out.add_stream("aac", rate=ai.rate)
        offset = 0
        for p in parts:
            with av.open(p) as c:
                vs = c.streams.video[0]
                last = 0
                for pk in c.demux(vs):
                    if pk.dts is None:
                        continue
                    pk.pts = (pk.pts or 0) + offset
                    pk.dts = pk.dts + offset
                    last = max(last, pk.pts + (pk.duration or 0))
                    pk.stream = vt
                    out.mux(pk)
                offset = last
        if at is not None:
            ai = aud_in.streams.audio[0]
            if a_copy:
                for pk in aud_in.demux(ai):
                    if pk.dts is None:
                        continue
                    pk.stream = at
                    out.mux(pk)
            else:
                for fr in aud_in.decode(ai):
                    fr.pts = None
                    for pk in at.encode(fr):
                        out.mux(pk)
                for pk in at.encode():
                    out.mux(pk)
        aud_in.close()
    os.replace(tmp, final)


class GoudanVideoDirSave:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "帧": ("IMAGE",),
            "段信息": ("STRING", {"forceInput": True}),
            "画质crf": ("INT", {"default": 16, "min": 0, "max": 51, "tooltip": "H.264 CRF，越小越清晰文件越大。16≈视觉无损。"}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("最终文件",)
    OUTPUT_NODE = True
    FUNCTION = "save"
    CATEGORY = "SeedVR2 目录读写"

    def save(self, 帧, 段信息, 画质crf=16):
        info = json.loads(段信息)
        fps = Fraction(info["fps"])
        pdir, final, total = info["parts"], info["final"], info["total"]
        os.makedirs(pdir, exist_ok=True)
        part = os.path.join(pdir, f"{info['part'] + 1:05d}.mp4")
        _encode(帧, part, fps, 画质crf)
        print(f"{TAG} 已存第 {info['part'] + 1}/{total} 段 {part}（{帧.shape[2]}x{帧.shape[1]}，{帧.shape[0]} 帧）")
        parts = [os.path.join(pdir, f"{k + 1:05d}.mp4") for k in range(total)]
        if all(os.path.isfile(p) for p in parts):
            os.makedirs(os.path.dirname(final), exist_ok=True)
            if os.path.exists(final):
                b, e = os.path.splitext(final)
                n = 1
                while os.path.exists(f"{b}_{n:03d}{e}"):
                    n += 1
                final = f"{b}_{n:03d}{e}"
            _concat(parts, info["src"], final)
            shutil.rmtree(pdir, ignore_errors=True)
            try:
                os.rmdir(os.path.dirname(pdir))  # .parts 空了就删掉
            except OSError:
                pass
            print(f"{TAG} 完成 {final}")
            return {"ui": {"text": [final]}, "result": (final,)}
        return {"ui": {"text": [part]}, "result": (part,)}


class GoudanFrameModelUpscale:
    """逐帧（小批）用放大模型放大，再立刻 lanczos 缩到目标长边，内存里不会堆 4 倍大的整段帧。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "放大模型": ("UPSCALE_MODEL",),
            "帧": ("IMAGE",),
            "目标长边": ("INT", {"default": 3840, "min": 64, "max": 16384, "step": 2}),
            "每批帧数": ("INT", {"default": 2, "min": 1, "max": 64}),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("帧",)
    FUNCTION = "run"
    CATEGORY = "SeedVR2 目录读写"

    def run(self, 放大模型, 帧, 目标长边, 每批帧数):
        import comfy.utils
        from comfy_extras.nodes_upscale_model import ImageUpscaleWithModel
        H, W = 帧.shape[1], 帧.shape[2]
        if W >= H:
            tw, th = 目标长边, max(2, round(H * 目标长边 / W / 2) * 2)
        else:
            th, tw = 目标长边, max(2, round(W * 目标长边 / H / 2) * 2)
        out = []
        for i in range(0, 帧.shape[0], 每批帧数):
            r = ImageUpscaleWithModel.execute(放大模型, 帧[i:i + 每批帧数])
            r = r.result[0] if hasattr(r, "result") else r[0]
            r = comfy.utils.common_upscale(r.movedim(-1, 1), tw, th, "lanczos", "disabled").movedim(1, -1)
            out.append(r.clamp(0, 1).cpu())
        return (torch.cat(out),)


class GoudanVideoLongSide:
    """整段帧缩放到指定长边（宽高取偶数/8 的倍数）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"帧": ("IMAGE",), "长边": ("INT", {"default": 960, "min": 64, "max": 16384}),
                             "对齐": ("INT", {"default": 8, "min": 2, "max": 64})}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("帧",)
    FUNCTION = "run"
    CATEGORY = "SeedVR2 目录读写"

    def run(self, 帧, 长边, 对齐):
        import comfy.utils
        H, W = 帧.shape[1], 帧.shape[2]
        s = 长边 / max(H, W)
        tw = max(对齐, round(W * s / 对齐) * 对齐)
        th = max(对齐, round(H * s / 对齐) * 对齐)
        if (tw, th) == (W, H):
            return (帧,)
        out = [comfy.utils.common_upscale(帧[i:i + 8].movedim(-1, 1), tw, th, "lanczos", "disabled").movedim(1, -1).clamp(0, 1)
               for i in range(0, 帧.shape[0], 8)]
        return (torch.cat(out),)
