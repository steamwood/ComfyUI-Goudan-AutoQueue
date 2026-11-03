"""分段接力排队（MiniMax H3 + H3-ClipStream 的 Clip Bin Picker / Saver）。

一次运行把「轮数 × 段数」个任务按 第1轮第1段、第1轮第2段 … 第2轮第1段 … 的顺序排进队列：
- 每一轮用一个新项目名：基名_r01_20261103_101500（Picker 和 Saver 的 project_name 都连本节点的「项目名」输出）；
- 第 1 段把 Picker 设成 Force Initial（首段，无上下文），后面的段设成 Auto + clip_selection='latest'（接上一段）；
- 「本段提示词」输出：分段提示词里每行一段，连到 MiniMaxH3Easy 的 prompt 和 Saver 的 prompt；
- 每份任务的「序号」不同，Picker 的缓存签名也就不同，不会把上一段的读取结果当缓存直接用；
- 每段开跑前核对：项目里已有的镜头数必须 = 段号 - 1。不对就报中文错误，并把这一轮后面的段从队列里撤掉，
  不会悄悄地接错段或跳段。
只读 Clip Bin 的索引文件（.bin_index.json）来数镜头，不写、不改 ClipStream 的任何东西。"""
import datetime
import json
import os
import random
import uuid

from . import autoqueue as aq

PICKER_TYPES = ("MiniMaxClipBinPicker", "MiniMaxClipBinDualPicker")
SAVER_TYPES = ("MiniMaxClipBinSaver", "MiniMaxClipBinDualSaver")
BIN_DIRS = ("h3-clipstream", "minimax_h3_bins")  # 新版 / 老版 ClipStream 的存放文件夹
# 老版选项是「后续自动接力」，新版是「后续自动接续」，都按开头的英文匹配
MODE_DEFAULTS = {"Force Initial": "Force Initial (强制新建首段，无上下文)", "Auto": "Auto (首段全新 / 后续自动接续)"}
SEED_MODES = ("每轮随机（同一轮各段同种子）", "每轮递增（同一轮各段同种子）", "不改种子")
TAG = "分段接力"


def split_prompts(text):
    """每行一段；空行、# 开头的行不算。"""
    return [l.strip() for l in str(text or "").split("\n") if l.strip() and not l.strip().startswith("#")]


def sanitize(name):
    """和 ClipStream 一样的文件夹名规则（只留字母数字、_、-、空格）。"""
    safe = "".join(c for c in (name or "Default_Project") if c.isalnum() or c in ("_", "-", " ")).strip()
    return safe or "Default_Project"


def project_name(base, r, stamp):
    return f"{(base or 'H3').strip()}_r{r:02d}_{stamp}"


def _output_dir():
    try:
        import folder_paths
        return folder_paths.get_output_directory()
    except Exception:
        return os.path.abspath("output")


def clip_count(project, out_dir=None):
    """项目里已有几段（读 .bin_index.json 的 clips；没有索引就数含 meta.json 的子文件夹）。两个存放文件夹都看。"""
    out_dir = out_dir or _output_dir()
    total = 0
    for d in BIN_DIRS:
        p = os.path.join(out_dir, d, sanitize(project))
        if not os.path.isdir(p):
            continue
        idx = os.path.join(p, ".bin_index.json")
        n = None
        if os.path.isfile(idx):
            try:
                with open(idx, encoding="utf-8") as f:
                    n = len((json.load(f) or {}).get("clips", []) or [])
            except (OSError, ValueError, AttributeError):
                n = None
        if n is None:
            n = sum(1 for c in os.listdir(p) if os.path.isfile(os.path.join(p, c, "meta.json")))
        total = max(total, n)
    return total


def _mode_options(class_type):
    try:
        import nodes
        cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
        spec = cls.INPUT_TYPES()["required"]["mode"][0]
        return [o for o in spec if isinstance(o, str)]
    except Exception:
        return []


def pick_mode(prefix, options, current=None):
    """在这个版本 Picker 的选项里找以 prefix 开头的那个（老版“接力”、新版“接续”都能对上）。"""
    for o in list(options or []) + ([current] if isinstance(current, str) else []):
        if o.startswith(prefix):
            return o
    return MODE_DEFAULTS[prefix]


def linked_from(prompt, nid, out_index=None):
    """输入连着 nid（某个输出）的 [(节点id, 输入名)]。"""
    res = []
    for tid, node in prompt.items():
        if not isinstance(node, dict):
            continue
        for k, v in (node.get("inputs") or {}).items():
            if isinstance(v, list) and len(v) == 2 and str(v[0]) == str(nid) and (out_index is None or v[1] == out_index):
                res.append((tid, k))
    return res


def configure(prompt, nid):
    """按本节点的「序号」改 Picker：第 1 段 Force Initial，后面 Auto + latest。点运行和每份副本入队时都会调。"""
    node = prompt.get(str(nid)) or {}
    inp = node.get("inputs", {})
    segs = segment_list(inp)
    n = max(1, len(segs))
    k = inp.get("序号") if isinstance(inp.get("序号"), int) else 0
    seg = k % n
    pickers = {t for t, _ in linked_from(prompt, nid, 0) if prompt[t].get("class_type") in PICKER_TYPES}
    for t in pickers:
        pi = prompt[t]["inputs"]
        opts = _mode_options(prompt[t]["class_type"])
        pi["mode"] = pick_mode("Force Initial" if seg == 0 else "Auto", opts, pi.get("mode"))
        if not isinstance(pi.get("clip_selection"), list):
            pi["clip_selection"] = "latest"
    return pickers


def segment_list(inp):
    segs = split_prompts(inp.get("分段提示词"))
    want = inp.get("段数") if isinstance(inp.get("段数"), int) else 0
    if want and want > 0:
        return segs[:want] if len(segs) >= want else segs + [None] * (want - len(segs))
    return segs


def on_prompt(json_data):
    """点运行（以及副本入队）时：定下批次时间戳，按段改 Picker 模式。"""
    try:
        prompt = json_data.get("prompt")
        if not isinstance(prompt, dict):
            return json_data
        for nid, node in prompt.items():
            if isinstance(node, dict) and node.get("class_type") == "GoudanSegmentRelay":
                inp = node.setdefault("inputs", {})
                if not inp.get("批次"):
                    inp["批次"] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                configure(prompt, nid)
    except Exception as e:
        print(f"[{TAG}] 运行前设置 Picker 出错：{e}")
    return json_data


aq.PRE_HOOKS.append(on_prompt)  # 在自动排队算指纹之前改好（和副本入队走同一个处理）


def round_seed_copy(p, rules, r, mode, round_seeds):
    """同一轮各段用同一个种子：每轮随机 / 每轮递增（第 1 轮是原值）/ 不改。"""
    if mode == SEED_MODES[2]:
        return p
    for nid, node in p.items():
        if isinstance(node, dict) and node.get("class_type") == "Seed (rgthree)" and nid not in rules:
            rules[nid] = (("seed",), "randomize")
    for nid, (keys, _m) in rules.items():
        node = p.get(nid)
        if not node:
            continue
        for key in keys:
            v = node.get("inputs", {}).get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                if mode == SEED_MODES[0]:
                    node["inputs"][key] = v if r == 0 else round_seeds.setdefault((r, nid, key), random.randint(0, 2**50))
                else:
                    node["inputs"][key] = v + r
    return p


def _cancel_round(batch, r, my_id):
    """把这一轮还没跑的段从队列里撤掉（同批次、同轮）。"""
    try:
        from server import PromptServer
        q = PromptServer.instance.prompt_queue

        def same(item):
            node = (item[2] or {}).get(str(my_id)) or {}
            inp = node.get("inputs", {})
            if node.get("class_type") != "GoudanSegmentRelay" or inp.get("批次") != batch:
                return False
            n = max(1, len(segment_list(inp)))
            return isinstance(inp.get("序号"), int) and inp["序号"] // n == r
        n = 0
        while q.delete_queue_item(same):
            n += 1
        return n
    except Exception as e:
        print(f"[{TAG}] 撤掉本轮剩余段时出错：{e}")
        return 0


class GoudanSegmentRelay:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "段数": ("INT", {"default": 0, "min": 0, "max": 999, "tooltip": "每轮几段。0 = 按「分段提示词」的行数"}),
                "轮数": ("INT", {"default": 1, "min": 1, "max": 1000, "tooltip": "整条长视频做几遍（每遍一个新项目）。总任务数 = 轮数 × 段数"}),
                "项目基名": ("STRING", {"default": "H3长视频", "tooltip": "每轮的项目名 = 基名_r01_批次时间。连 Picker 和 Saver 的 project_name"}),
                "分段提示词": ("STRING", {"multiline": True, "default": "", "tooltip": "每行一段（空行和 # 开头的行不算）"}),
                "种子": (list(SEED_MODES), {"default": SEED_MODES[0], "tooltip": "同一轮里各段用同一个种子；每轮换新随机数或在原值上 +轮号"}),
                "自动排队": ("BOOLEAN", {"default": True}),
                "序号": ("INT", {"default": 0, "min": 0, "max": 2**31 - 1, "tooltip": "自动管理，保持 0 即可"}),
            },
            "optional": {
                "批次": ("STRING", {"default": "", "tooltip": "自动管理（点运行时填成时间戳），保持空着即可"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "STRING", "INT", "INT", "INT")
    RETURN_NAMES = ("项目名", "本段提示词", "序号", "段", "轮")
    OUTPUT_TOOLTIPS = ("连 Clip Bin Picker 和 Saver 的 project_name", "连 MiniMaxH3Easy 的 prompt 和 Saver 的 prompt",
                       "第几个任务（0 起）", "第几段（1 起）", "第几轮（1 起）")
    FUNCTION = "run"
    CATEGORY = "狗蛋工具"

    @classmethod
    def IS_CHANGED(cls, **kw):
        return uuid.uuid4().hex

    def run(self, 段数, 轮数, 项目基名, 分段提示词, 种子, 自动排队, 序号, 批次="", prompt=None, extra_pnginfo=None, unique_id=None):
        inp = {"段数": 段数, "分段提示词": 分段提示词}
        segs = segment_list(inp)
        if not segs:
            raise RuntimeError(f"[{TAG}] 「分段提示词」是空的：每行写一段提示词。")
        if None in segs:
            raise RuntimeError(f"[{TAG}] 段数设成 {段数}，但「分段提示词」只有 {len(split_prompts(分段提示词))} 行。每段要一行提示词（或把段数设成 0 按行数算）。")
        n = len(segs)
        total = n * 轮数
        batch = 批次 or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        r, s = 序号 // n, 序号 % n
        proj = project_name(项目基名, r + 1, batch)
        if 自动排队 and 序号 == 0 and total > 1 and prompt:
            fp = aq.fingerprint(prompt, extra_pnginfo, unique_id, ("序号", "自动排队", "批次"))
            if aq.is_duplicate(fp, extra_pnginfo):
                print(f"[{TAG}] 队列里已经有一套内容完全相同的任务（重复点了运行），这次整次跳过。")
                return aq.blocked_outputs(5)
            rules = aq._seed_rules(extra_pnginfo)
            round_seeds = {}

            def gen():
                for k in range(1, total):
                    p = aq.make_copy(prompt, extra_pnginfo, k, unique_id, {"序号": k, "自动排队": False, "批次": batch}, rules={})
                    yield round_seed_copy(p, dict(rules), k // n, 种子, round_seeds)
            aq.queue_copies(gen(), extra_pnginfo, TAG, fp, prompt, total=total - 1)
            print(f"[{TAG}] 共 {轮数} 轮 × {n} 段 = {total} 个任务，已把剩下 {total - 1} 个按 轮→段 顺序排进队列")
        have = clip_count(proj)
        if have != s:
            left = _cancel_round(batch, r, unique_id)
            raise RuntimeError(
                f"[{TAG}] 第 {r + 1} 轮第 {s + 1} 段开跑前检查失败：项目「{proj}」里应该已有 {s} 段，实际有 {have} 段"
                f"（{'上一段没有存进 Clip Bin，可能上一段出错或 Saver 的 project_name 没连本节点' if have < s else '项目里有别的镜头，项目名可能重复'}）。"
                f"为了不接错段，这一段不跑，本轮后面的 {left} 段也已从队列撤掉；其它轮不受影响。")
        print(f"[{TAG}] 第 {r + 1}/{轮数} 轮 第 {s + 1}/{n} 段，项目 {proj}")
        return (proj, segs[s], 序号, s + 1, r + 1)
