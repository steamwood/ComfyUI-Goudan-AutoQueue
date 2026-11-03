"""分段接力排队（MiniMax H3 + H3-ClipStream 的 Clip Bin Picker / Saver）。

一次运行把「轮数 × 段数」个任务按 第1轮第1段、第1轮第2段 … 第2轮第1段 … 的顺序排进队列：
- 每一轮用一个新项目名：基名_r01_20261103_101500（Picker 和 Saver 的 project_name 都连本节点的「项目名」输出）；
- 第 1 段把 Picker 设成 Force Initial（首段，无上下文），后面的段设成 Auto + clip_selection='latest'（接上一段）；
- 「本段提示词」输出：分段规则和文生图「提示词循环」完全一样——单独一行以 # 开头的行把提示词分成段（一段可以写多行，
  空段跳过，段数自动 = 非空段数）；「全局触发词」加在每段最前面；{a|b} / __列表名__ 随机变量在装了 Impact Pack 时展开。
  连到 MiniMaxH3Easy 的 prompt 和 Saver 的 prompt；
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
    """和「提示词循环」一样的分段：用 autoqueue.split_items / nonempty_index（# 行分段、空段跳过）。
    返回每个非空段的正文（# 行本身不进提示词）。全空返回 []。"""
    text = str(text or "")
    if not any(l.strip() and not l.startswith("#") for l in text.split("\n")):
        return []
    items = aq.split_items(text)
    return ["\n".join(l for l in items[i].split("\n") if not l.startswith("#")).strip() for i in aq.nonempty_index(text)]


def legacy_lines(text):
    """1.2.9 的旧写法：每行一段（空行、# 行不算）。"""
    return [l.strip() for l in str(text or "").split("\n") if l.strip() and not l.strip().startswith("#")]


def expand_random(text, seed):
    """{a|b} / __列表名__：和文生图一样交给 Impact Pack 的通配符展开（没装 Impact 或没有随机写法就原样返回）。"""
    if "{" not in text and "__" not in text:
        return text
    try:
        from impact import wildcards
        return wildcards.process(text, seed)
    except Exception as e:
        print(f"[{TAG}] 没能展开随机变量（需要 ComfyUI-Impact-Pack）：{e}，按原文使用")
        return text


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
    """本节点的各段（和提示词循环同一套 # 分段规则）。"""
    return split_prompts(inp.get("分段提示词"))


def upgrade_legacy(inp):
    """1.2.9 的旧写法（API 提交或没经过前端升级的工作流）：带「段数」「种子」、每行一段。
    改成 # 分段 + 每轮随机种子，再照新规则跑。返回是否改过。"""
    if "段数" not in inp and "种子" not in inp:
        return False
    text = inp.get("分段提示词")
    if not any(l.startswith("#") for l in str(text or "").split("\n")):
        lines = legacy_lines(text)
        want = inp.get("段数") if isinstance(inp.get("段数"), int) else 0
        if want and want > len(lines):
            print(f"[{TAG}] 旧工作流的段数是 {want}，但只有 {len(lines)} 行提示词，按 {len(lines)} 段跑")
        if want and want > 0:
            lines = lines[:want]
        inp["分段提示词"] = "\n".join("#\n" + l for l in lines)
    seed = inp.pop("种子", None)
    if "每轮随机种子" not in inp:
        inp["每轮随机种子"] = seed != SEED_MODES[2]
    inp.pop("段数", None)
    print(f"[{TAG}] 旧版分段接力的写法已自动改成 # 分段")
    return True


def on_prompt(json_data):
    """点运行（以及副本入队）时：定下批次时间戳，按段改 Picker 模式。"""
    try:
        prompt = json_data.get("prompt")
        if not isinstance(prompt, dict):
            return json_data
        for nid, node in prompt.items():
            if isinstance(node, dict) and node.get("class_type") == "GoudanSegmentRelay":
                inp = node.setdefault("inputs", {})
                upgrade_legacy(inp)
                trig = inp.get(aq.TRIGGER_KEY)
                if isinstance(trig, str) and trig.strip() and isinstance(inp.get("分段提示词"), str):
                    inp["分段提示词"] = aq.prepend_trigger(inp["分段提示词"], trig)  # 和提示词循环一样：点运行时加一次，副本不重复加
                    inp[aq.TRIGGER_KEY] = ""
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
                "轮数": ("INT", {"default": 1, "min": 1, "max": 1000, "tooltip": "整条长视频做几遍（每遍一个新项目）。总任务数 = 段数 × 轮数，自动排队。"}),
                "自动排队": ("BOOLEAN", {"default": True}),
                "序号": ("INT", {"default": 0, "min": 0, "max": 2**31 - 1, "tooltip": "自动管理，保持 0 即可"}),
                "项目基名": ("STRING", {"default": "H3长视频", "tooltip": "每轮的项目名 = 基名_r01_批次时间。连 Picker 和 Saver 的 project_name"}),
                "分段提示词": ("STRING", {"multiline": True, "default": "#\n第 1 段提示词\n#\n第 2 段提示词",
                                     "tooltip": "和文生图的提示词列表一样：用单独一行 # 分段（一段可以写多行，空段跳过），段数自动 = 非空段数；"
                                                "{a|b|c} 或 __列表名__ 随机（需要 Impact Pack）"}),
            },
            "optional": {
                "每轮随机种子": ("BOOLEAN", {"default": True, "tooltip": "开着时每一轮换新的随机种子（rgthree Seed 和采样器种子都会换），同一轮各段用同一个种子；关着时都不改"}),
                "全局触发词": ("STRING", {"default": "", "tooltip": "可空。填了会自动加在每一段提示词最前面（例如 LoRA 触发词），在随机变量展开之前加，空着就什么都不加"}),
                "批次": ("STRING", {"default": "", "tooltip": "自动管理（点运行时填成时间戳），保持空着即可"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "STRING", "INT", "INT", "INT")
    RETURN_NAMES = ("项目名", "本段提示词", "序号", "段", "轮")
    OUTPUT_TOOLTIPS = ("连 Clip Bin Picker 和 Saver 的 project_name", "连 MiniMaxH3Easy 的 prompt 和 Saver 的 prompt（已加触发词、已展开随机变量）",
                       "第几个任务（0 起）", "第几段（1 起）", "第几轮（1 起）")
    FUNCTION = "run"
    CATEGORY = "狗蛋工具"

    @classmethod
    def IS_CHANGED(cls, **kw):
        return uuid.uuid4().hex

    def run(self, 轮数, 自动排队, 序号, 项目基名, 分段提示词, 每轮随机种子=True, 全局触发词="", 批次="",
            prompt=None, extra_pnginfo=None, unique_id=None, **_ignored):
        inp = {"分段提示词": 分段提示词}
        if 全局触发词 and 全局触发词.strip():  # 直接调用（没经过点运行处理）时也加上
            inp["分段提示词"] = aq.prepend_trigger(分段提示词, 全局触发词)
        segs = segment_list(inp)
        if not segs:
            raise RuntimeError(f"[{TAG}] 「分段提示词」是空的：用单独一行 # 分段，每段写一段提示词。")
        seed_mode = SEED_MODES[0] if 每轮随机种子 else SEED_MODES[2]
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
            if not aq._first_auto_node(prompt, unique_id):
                print(f"[{TAG}] 图里有多个开着「自动排队」的节点，只由 id 最小的那个负责排队，这个不排。")
            else:
                rules = aq._seed_rules(extra_pnginfo)
                round_seeds = {}

                def gen():
                    for k in range(1, total):
                        p = aq.make_copy(prompt, extra_pnginfo, k, unique_id, {"序号": k, "自动排队": False, "批次": batch}, rules={})
                        yield round_seed_copy(p, dict(rules), k // n, seed_mode, round_seeds)
                aq.queue_copies(gen(), extra_pnginfo, TAG, fp, prompt, total=total - 1)
                print(f"[{TAG}] 共 {n} 段 × {轮数} 轮 = {total} 个任务，已把剩下 {total - 1} 个按 轮→段 顺序排进队列")
        have = clip_count(proj)
        if have != s:
            left = _cancel_round(batch, r, unique_id)
            raise RuntimeError(
                f"[{TAG}] 第 {r + 1} 轮第 {s + 1} 段开跑前检查失败：项目「{proj}」里应该已有 {s} 段，实际有 {have} 段"
                f"（{'上一段没有存进 Clip Bin，可能上一段出错或 Saver 的 project_name 没连本节点' if have < s else '项目里有别的镜头，项目名可能重复'}）。"
                f"为了不接错段，这一段不跑，本轮后面的 {left} 段也已从队列撤掉；其它轮不受影响。")
        text = expand_random(segs[s], random.Random(f"{batch}|{序号}").randint(0, 2**32 - 1))
        print(f"[{TAG}] 第 {r + 1}/{轮数} 轮 第 {s + 1}/{n} 段，项目 {proj}：{text[:60]}")
        return (proj, text, 序号, s + 1, r + 1)
