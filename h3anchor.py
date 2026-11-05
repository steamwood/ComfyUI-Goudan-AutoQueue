"""一镜到底：把同一场景后面各段的整体颜色/亮度/对比拉回该场景第 1 段（latent 层 AdaIN 式逐通道 均值/标准差 匹配）。

- 场景第 1 段（context_frames = 0，Motion Context 的 trim_frames）：记下视频 latent 每个通道的均值和标准差，
  按「项目名」精确存成 output/goudan_anchor/<项目名>.pt（项目名 = 基名[_场景名]_rNN_批次，每轮每场景唯一，不会串场景）；
- 接续段：只看本段新生成的部分（上下文帧之后）量当前统计，校正后按帧渐入：前 context_frames 帧不动（接缝保持），
  之后 ramp_frames 帧里从 0 升到 strength；标准差的缩放限制在 0.85～1.18；
- 细节锚定（1.2.13+）：另把每个通道的「细节能量」（latent 空间 3x3 模糊残差 = 细纹理带、再一层 = 中频带，各自 rms）
  也记进锚点；接续段只会把细节往「更软」拉（增益 ≤ 1，最低 detail_floor），永远不加锐；同样只动上下文之后的新帧；
- 只改视频流，音频流原样返回；找不到锚点文件就原样通过并打印提示。"""
import os
import re

import torch

FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
TAG = "场景锚定"
STD_MIN, STD_MAX = 0.85, 1.18


def tokens_for_frames(frames):
    """像素帧 → 视频 latent 帧（H3：第 1 个 latent 帧 1 帧，之后每个 4 帧；不整除时向上取整）。"""
    t, f = 0, 0
    while f < int(frames):
        f += FRAME_PER_TOKEN[t % 5]
        t += 1
    return t


def safe_key(name):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "").strip())
    return s.strip(" .") or "_"


def anchor_dir():
    try:
        import folder_paths
        root = folder_paths.get_output_directory()
    except Exception:
        root = os.path.join(os.getcwd(), "output")
    return os.path.join(root, "goudan_anchor")


def split_av(samples):
    if hasattr(samples, "unbind") and type(samples).__name__ == "NestedTensor":
        parts = list(samples.unbind())
    elif isinstance(samples, (list, tuple)):
        parts = list(samples)
    else:
        return samples, None
    return parts[0], (parts[1] if len(parts) > 1 else None)


def join_av(orig, video, sound):
    if sound is None:
        return video
    if isinstance(orig, (list, tuple)):
        return type(orig)([video, sound])
    import comfy.nested_tensor
    return comfy.nested_tensor.NestedTensor((video, sound))


def channel_stats(v5):
    """v5: [B,C,T,H,W] → 每通道 (mean, std)，形状 [C]。"""
    x = v5.float().transpose(0, 1).reshape(v5.shape[1], -1)
    return x.mean(1), x.std(1).clamp_min(1e-6)


def blur3(v5):
    """3x3 盒式模糊（每帧空间方向，边缘复制）。"""
    import torch.nn.functional as F
    h, w = v5.shape[-2], v5.shape[-1]
    p = F.pad(v5, (1, 1, 1, 1, 0, 0), mode="replicate")
    out = 0
    for i in range(3):
        for j in range(3):
            out = out + p[..., i:i + h, j:j + w]
    return out / 9.0


def bands(v5):
    """拆成 低频 + 中频 + 细节 三带（相加还原）。"""
    v5 = v5.float()
    b1 = blur3(v5)
    b2 = blur3(blur3(b1))
    return b2, b1 - b2, v5 - b1


def band_rms(x5):
    """每通道 rms，形状 [C]。"""
    x = x5.float().transpose(0, 1).reshape(x5.shape[1], -1)
    return (x * x).mean(1).sqrt().clamp_min(1e-6)


def detail_stats(v5):
    _, mid, fine = bands(v5)
    return {"fine": band_rms(fine), "mid": band_rms(mid)}


def frame_weights(T, hold_t, ramp_t, strength, device=None):
    w = torch.zeros(T, device=device)
    for t in range(hold_t, T):
        w[t] = strength * min(1.0, (t - hold_t + 1) / max(1, ramp_t))
    return w


def detail_apply(v5, dstats, hold_t, ramp_t, strength, floor):
    """只往软拉：gain = clamp(锚点/当前, floor, 1)，按帧渐入；返回 (新 v5 float, 说明)。"""
    T = v5.shape[2]
    low, mid, fine = bands(v5)
    cur = detail_stats(v5[:, :, hold_t:])
    sh = (1, -1, 1, 1, 1)
    w = frame_weights(T, hold_t, ramp_t, strength, v5.device).view(1, 1, T, 1, 1)
    parts, info = [], []
    for name, band in (("fine", fine), ("mid", mid)):
        a = dstats[name].to(v5.device).float()
        ratio = (a / cur[name]).clamp(float(floor), 1.0)
        g = 1.0 + w * (ratio.view(sh) - 1.0)
        parts.append(band * g)
        info.append(f"{'细节' if name == 'fine' else '中频'} {float((cur[name] / a).mean()):.3f}x→增益 {float(ratio.mean()):.3f}")
    return low + parts[0] + parts[1], "，".join(info)


def anchor_apply(v, stats, hold_t, ramp_t, strength, match_std=True, detail_strength=0.0, detail_floor=0.6):
    v5 = v.unsqueeze(0) if v.ndim == 4 else v
    T = v5.shape[2]
    if hold_t >= T or (strength <= 0 and detail_strength <= 0):
        return v, ("strength 为 0，不校正" if hold_t < T else "上下文后面没有新帧，不校正")
    dmsg = ""
    if detail_strength > 0 and stats.get("detail"):
        v5d, dinfo = detail_apply(v5, stats["detail"], hold_t, ramp_t, float(detail_strength), detail_floor)
        v5 = torch.cat([v5[:, :, :hold_t].float(), v5d[:, :, hold_t:]], 2).to(v5.dtype)
        dmsg = f"；细节锚定 {dinfo}（强度 {detail_strength:g}，最低 {detail_floor:g}）"
    elif detail_strength > 0:
        dmsg = "；锚点里没有细节数据（第 1 段是旧版插件记的），细节锚定跳过"
    if strength <= 0:
        return (v5[0] if v.ndim == 4 else v5), "颜色/对比不校正" + dmsg
    mu_a, sd_a = stats["mean"].to(v5.device), stats["std"].to(v5.device)
    mu_c, sd_c = channel_stats(v5[:, :, hold_t:])
    ratio = (sd_a / sd_c).clamp(STD_MIN, STD_MAX) if match_std else torch.ones_like(sd_c)
    sh = (1, -1, 1, 1, 1)
    corr = (v5.float() - mu_c.view(sh)) * ratio.view(sh) + mu_a.view(sh)
    w = frame_weights(T, hold_t, ramp_t, strength, v5.device)
    out = v5.float() + w.view(1, 1, T, 1, 1) * (corr - v5.float())
    out = out.to(v5.dtype)
    msg = (f"均值偏差 {float((mu_c - mu_a).abs().mean()):.4f}，标准差比 {float((sd_c / sd_a).mean()):.3f}"
           f"，校正强度 {strength:g}（前 {hold_t} 个 latent 帧不动，{ramp_t} 帧渐入）{dmsg}")
    return (out[0] if v.ndim == 4 else out), msg


class GoudanH3SceneAnchor:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "latent": ("LATENT",),
                "项目名": ("STRING", {"default": "", "forceInput": True, "tooltip": "连分段接力的「项目名」：每轮每个场景一个，按它精确存取锚点"}),
                "context_frames": ("INT", {"default": 22, "min": 0, "max": 4096, "tooltip": "连 Motion Context 的 trim_frames：0 = 场景第 1 段（记锚点），>0 = 接续段（校正）"}),
                "strength": ("FLOAT", {"default": 0.6, "min": 0.0, "max": 1.0, "step": 0.05, "tooltip": "拉回第 1 段统计的比例；0.5～0.8 试"}),
                "ramp_frames": ("INT", {"default": 22, "min": 0, "max": 512, "tooltip": "上下文之后多少帧内从 0 渐入到 strength"}),
                "匹配对比": ("BOOLEAN", {"default": True, "tooltip": "开 = 均值 + 标准差（亮度/颜色 + 对比/饱和）；关 = 只匹配均值"}),
            },
            "optional": {
                "细节锚定": ("BOOLEAN", {"default": True, "tooltip": "开 = 把细纹理/中频能量也拉回第 1 段（只会变软，不会加锐），压「越来越锐、越来越硬」"}),
                "细节强度": ("FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.05, "tooltip": "细节拉回比例；还锐就 0.9～1.0"}),
                "细节最低增益": ("FLOAT", {"default": 0.6, "min": 0.2, "max": 1.0, "step": 0.05, "tooltip": "每段最多把细节削到原来的多少；越小越能压，太小会糊"}),
            },
        }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "报告")
    FUNCTION = "run"
    CATEGORY = "狗蛋工具"

    def run(self, latent, 项目名, context_frames, strength, ramp_frames, 匹配对比=True, 细节锚定=True, 细节强度=0.8, 细节最低增益=0.6):
        samples = latent["samples"]
        video, sound = split_av(samples)
        path = os.path.join(anchor_dir(), safe_key(项目名) + ".pt")
        if int(context_frames) <= 0:
            v5 = video.unsqueeze(0) if video.ndim == 4 else video
            mu, sd = channel_stats(v5)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            det = {k: x.cpu() for k, x in detail_stats(v5).items()}
            torch.save({"mean": mu.cpu(), "std": sd.cpu(), "detail": det, "project": str(项目名)}, path)
            msg = f"[{TAG}] 场景第 1 段：已记下锚点 {os.path.basename(path)}"
            print(msg)
            return (latent, msg)
        if not os.path.isfile(path):
            msg = f"[{TAG}] 没找到「{项目名}」的锚点（场景第 1 段没开 E 或没跑完），本段不校正"
            print(msg)
            return (latent, msg)
        stats = torch.load(path, map_location="cpu")
        out, info = anchor_apply(video, stats, tokens_for_frames(context_frames), tokens_for_frames(ramp_frames) if ramp_frames > 0 else 1,
                                 float(strength), bool(匹配对比),
                                 float(细节强度) if 细节锚定 else 0.0, float(细节最低增益))
        msg = f"[{TAG}] {项目名}：{info}"
        print(msg)
        res = dict(latent)
        res["samples"] = join_av(samples, out, sound)
        return (res, msg)
