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
- 多场景：单独一行 ## 场景名 开一个新场景。每个场景是单独的一条接力（项目名 基名_场景名_r01_时间戳，场景第 1 段 Force Initial），
  每轮按 场景A 全部段 → 场景B 全部段 的顺序；同一轮所有场景同一个种子；某段核对失败只撤这个场景本轮后面的段。没有 ## 和以前完全一样。
- H3 视频批量排队（1.3.0）：工作流里没有 ClipStream 的读取 / 保存片段节点时，每段就是一个独立任务，按顺序排队，不核对镜头数；
  「本段秒数」输出 = 分段行上写的秒数（没写 = 「默认秒数」输入），连到工作流里任意时长输入，只检查大于 0。
只读 Clip Bin 的索引文件（.bin_index.json）来数镜头，不写、不改 ClipStream 的任何东西。"""
import datetime
import json
import math
import os
import random
import re
import unicodedata
import uuid

from . import autoqueue as aq

PICKER_TYPES = ("MiniMaxClipBinPicker", "MiniMaxClipBinDualPicker")
SAVER_TYPES = ("MiniMaxClipBinSaver", "MiniMaxClipBinDualSaver")
BIN_DIRS = ("h3-clipstream", "minimax_h3_bins")  # 新版 / 老版 ClipStream 的存放文件夹
# 老版选项是「后续自动接力」，新版是「后续自动接续」，都按开头的英文匹配
MODE_DEFAULTS = {"Force Initial": "Force Initial (强制新建首段，无上下文)", "Auto": "Auto (首段全新 / 后续自动接续)"}
SEED_MODES = ("每轮随机（同一轮各段同种子）", "每轮递增（同一轮各段同种子）", "不改种子")  # 名字沿用；接续段的种子见 seg_seed
TAG = "分段接力"


DASH_SEP = re.compile(r"-{3,}")
# 分段行后面可以带本段秒数（整行只能是 分段符 + 数字 + 可选单位，单位不分大小写、前面可以有空格）：
#   # 4 / #4 / # 4s / # 4 s / # 4秒 / # 4.5 / # .5 / # 6sec / # 6secs / # 6second(s) / # 6.5S / # 4 秒钟 / --- 5.5 / ---5.5
#   全角数字、全角小数点（# ４．５秒）也认。
# 带别的文字的 # 行（例如「# 注释」「# 1. 开场」「# 3个人」）照旧只是分段线，不读秒数。
_NUM = r"\s*(\d+(?:\.\d*)?|\.\d+)\s*(?:s|secs?|seconds?|秒钟?)?\s*"
HASH_SEC = re.compile(r"#" + _NUM, re.IGNORECASE)
DASH_SEC = re.compile(r"-{3,}" + _NUM, re.IGNORECASE)
GENERIC_DEFAULT_SECONDS = 5.0  # 「默认秒数」没连、也没改时的值
SECONDS_TYPES = ("MiniMaxH3Easy",)
H3_FPS = 24.0
H3_MIN_SECONDS, H3_MAX_SECONDS = 0.2, 30.0  # MiniMaxH3Easy 的 seconds 输入范围（nodes.py MIN_SECONDS / MAX_SECONDS）
CONTEXT_TYPES = ("MiniMaxH3MotionContext",)
DEFAULT_KEY = "_默认秒数"  # 点运行时记下 MiniMaxH3Easy 原来的秒数（副本从改过的 prompt 拷贝，要靠它回到默认）


def _norm(line):
    """全角数字 / 全角小数点 / 全角空格 → 半角（只用于读秒数，分段规则不变）。"""
    return unicodedata.normalize("NFKC", line)


def is_sep(line):
    """分段行：和提示词循环一样以 # 开头；或者单独一行 ---（三个以上减号，MiniMaxH3-Easy 提示词优化输出的分段），后面可带秒数。"""
    return line.startswith("#") or bool(DASH_SEP.fullmatch(line.strip()) or DASH_SEC.fullmatch(_norm(line).strip()))


def sep_seconds(line):
    """分段行上写的秒数；没写（或带的不是纯数字）返回 None。"""
    if line.startswith("#"):
        m = HASH_SEC.fullmatch(_norm(line).rstrip())
    else:
        m = DASH_SEC.fullmatch(_norm(line).strip())
    return float(m.group(1)) if m else None


def _dash_to_hash(text):
    return "\n".join("#" if not l.startswith("#") and is_sep(l) else l for l in text.split("\n"))


def is_scene(line):
    """场景标题行：单独一行以 ## 开头（### 不算，照旧只是分段线），后面是场景名（可空）。「## 3」是名叫 3 的场景，不是秒数。"""
    return line.startswith("##") and not line.startswith("###")


def split_scenes(text):
    """按 ## 场景名 切成场景：[(场景名, 场景正文)]。没有 ## 行返回 [(None, 全文)]（和以前完全一样）。
    第一个 ## 之前如果有段，算第一个场景（没名字）；没有段的场景跳过；没写名字的场景叫 场景1、场景2…（按实际场景顺序）。"""
    text = str(text or "")
    lines = text.split("\n")
    if not any(is_scene(l) for l in lines):
        return [(None, text)]
    raw, name, cur = [], "", []
    for l in lines:
        if is_scene(l):
            raw.append((name, "\n".join(cur)))
            name, cur = l[2:].strip(), []
        else:
            cur.append(l)
    raw.append((name, "\n".join(cur)))
    out = []
    for nm, body in raw:
        if _split_prompts_one(body):
            out.append((nm or f"场景{len(out) + 1}", body))
    return out


def scene_plan(text):
    """每个非空段（全部场景连起来的顺序）属于哪个场景：[(场景序号0起, 场景名或None, 场景内第几段0起)]。"""
    plan = []
    for si, (nm, body) in enumerate(split_scenes(text)):
        plan.extend((si, nm, k) for k in range(len(_split_prompts_one(body))))
    return plan


def split_prompts(text):
    """所有场景的段连起来（没有 ## 时和以前一样）。"""
    return [x for _nm, body in split_scenes(text) for x in _split_prompts_one(body)]


def split_seconds(text):
    """每个非空段的秒数（所有场景连起来，顺序同 split_prompts）。"""
    return [x for _nm, body in split_scenes(text) for x in _split_seconds_one(body)]


def _split_prompts_one(text):
    """和「提示词循环」一样的分段：用 autoqueue.split_items / nonempty_index（# 行分段、空段跳过），
    另外单独一行 --- 也算分段。返回每个非空段的正文（分段行本身不进提示词）。全空返回 []。"""
    text = _dash_to_hash(str(text or ""))
    if not any(l.strip() and not l.startswith("#") for l in text.split("\n")):
        return []
    items = aq.split_items(text)
    return ["\n".join(l for l in items[i].split("\n") if not l.startswith("#")).strip() for i in aq.nonempty_index(text)]


def _split_seconds_one(text):
    """和 _split_prompts_one 一一对应：每个非空段的分段行上写的秒数（None = 用 MiniMaxH3Easy 节点上的默认值）。"""
    out, cur, has = [], None, False
    for line in str(text or "").split("\n"):
        if is_sep(line):
            v = sep_seconds(line)
            if has:
                out.append(cur)
                cur = v
            elif v is not None:  # 连着几行分段线（例如「# 4」下面再一行「# 镜头1：…」标题）：秒数留给下面的段
                cur = v
            has = False
        elif line.strip():
            has = True
    if has:
        out.append(cur)
    return out


def segment_seconds(text, seg, default):
    """「本段秒数」输出（通用，接任意时长输入）：第 seg 段（0 起）分段行上写了秒数就用写的，没写就用「默认秒数」。"""
    secs = split_seconds(text)
    v = secs[seg] if 0 <= seg < len(secs) else None
    return float(default if v is None else v)


def check_generic_seconds(text, default):
    """点运行后、排副本之前：每段的秒数必须是大于 0 的数（只做这一项检查，对齐交给工作流自己的公式）。返回 [每段秒数]，不合法报中文错误。"""
    out, bad = [], []
    for k, v in enumerate(split_seconds(text)):
        own = v is not None
        try:
            sec = float(v if own else default)
        except (TypeError, ValueError):
            sec = float("nan")
        if not (sec > 0 and math.isfinite(sec)):
            bad.append(f"第 {k + 1} 段" + (f"写的 {v:g} 秒" if own else f"（没写秒数，用默认秒数 {default}）"))
        out.append(sec)
    if bad:
        raise RuntimeError(f"[{TAG}] 时长必须大于 0 秒：" + "；".join(bad) + "。请改分段行上的秒数或「默认秒数」，这次一个任务都没排。")
    return out


def uses_clipbin(prompt):
    """工作流里有没有 ClipStream 的读取 / 保存片段节点。没有 = 每段独立出片（批量排队），不按段数核对项目里的镜头。
    prompt 拿不到时按有算（和以前一样）。"""
    if not isinstance(prompt, dict):
        return True
    return any(isinstance(n, dict) and n.get("class_type") in PICKER_TYPES + SAVER_TYPES for n in prompt.values())


def frames_for(seconds, fps=H3_FPS):
    """和 MiniMaxH3Easy 的 _frame_length 完全一样：秒数 × fps 取最近的 17×n+5 帧。"""
    target = max(5.0, float(seconds) * float(fps))
    return max(0, round((target - 5) / 17)) * 17 + 5


def plan_seconds(seconds, default, seg_index, context=22, fps=H3_FPS, label_index=None):
    """本段实际用的秒数和帧数。seg_index = 场景内第几段（0 = 场景首段，不接上下文）。返回 (seconds, frames, 是否来自分段行)。不合法就报中文错误。"""
    own = seconds is not None
    sec = float(seconds if own else default)
    label = f"第 {(seg_index if label_index is None else label_index) + 1} 段" + (f"写的 {sec:g} 秒" if own else f"（默认 {sec:g} 秒）")
    if own and not (H3_MIN_SECONDS <= sec <= H3_MAX_SECONDS):
        raise RuntimeError(f"[{TAG}] {label}超出 MiniMax H3 允许的范围 {H3_MIN_SECONDS:g}～{H3_MAX_SECONDS:g} 秒，请改分段行上的秒数。")
    sec = min(H3_MAX_SECONDS, max(H3_MIN_SECONDS, sec))
    frames = frames_for(sec, fps)
    if seg_index > 0 and frames - context < 17:
        lmin = context + 17
        while lmin % 17 != 5:
            lmin += 1
        need = (lmin - 8.5) / fps  # 四舍五入后能落到 lmin 帧的最短秒数
        raise RuntimeError(
            f"[{TAG}] {label}太短：H3 一段只能是 17×n+5 帧，{sec:g} 秒 × {fps:g}fps 对齐后是 {frames} 帧，"
            f"接续段要先放上一段最后 {context} 帧当动作上下文（成片时剪掉），剩下不到 17 帧。接续段至少写 {math.ceil(need * 10) / 10:g} 秒。")
    return sec, frames, own


def _context_frames(prompt):
    for node in prompt.values():
        if isinstance(node, dict) and node.get("class_type") in CONTEXT_TYPES:
            try:
                return int((node.get("inputs") or {}).get("context_length", 22))
            except (TypeError, ValueError):
                pass
    return 22


def _local(text, seg, prompt=None):
    """场景内第几段（0 起）。给了 prompt 且里面没有 ClipStream 节点（每段独立出片）时一律算 0：不接上一段，不需要上下文帧。"""
    if prompt is not None and not uses_clipbin(prompt):
        return 0
    plan = scene_plan(text)
    return plan[seg][2] if seg < len(plan) else seg


def apply_seconds(prompt, nid, seg):
    """把本段秒数写进 MiniMaxH3Easy 的 seconds；没写秒数的段恢复成节点原来的值。返回 [(节点id, 秒, 帧, 是否自定)]。"""
    inp = prompt[str(nid)]["inputs"]
    secs = split_seconds(inp.get("分段提示词"))
    want = secs[seg] if seg < len(secs) else None
    loc = _local(inp.get("分段提示词"), seg, prompt)
    defaults = inp.setdefault(DEFAULT_KEY, {})
    ctx = _context_frames(prompt)
    done = []
    for tid, node in prompt.items():
        if not isinstance(node, dict) or node.get("class_type") not in SECONDS_TYPES:
            continue
        ni = node.setdefault("inputs", {})
        if tid not in defaults:
            if isinstance(ni.get("seconds"), list):
                continue  # 秒数连了别的节点，不动
            defaults[tid] = ni.get("seconds", 5.0)
        try:
            fps = float(ni.get("fps", H3_FPS))
        except (TypeError, ValueError):
            fps = H3_FPS
        try:
            sec, frames, own = plan_seconds(want, defaults[tid], loc, ctx if loc > 0 else 0, fps, seg)
        except RuntimeError as e:  # 不在这里改；节点运行时会报同样的错误
            print(e)
            ni["seconds"] = defaults[tid]
            continue
        ni["seconds"] = sec if own else defaults[tid]
        done.append((tid, sec, frames, own, ctx, fps))
    return done


def check_all_seconds(prompt, nid):
    """点运行后第一个任务开跑时：所有段的秒数都检查一遍，有问题直接报错，一个副本都不排。"""
    p = json.loads(json.dumps(prompt))
    msgs = []
    for k in range(max(1, len(segment_list(p[str(nid)]["inputs"])))):
        loc = _local(p[str(nid)]["inputs"].get("分段提示词"), k, p)
        msgs.append(f"第 {k + 1} 段 " + (describe_seconds(_strict_apply(p, nid, k), loc) or "（没有 MiniMaxH3Easy）"))
    return msgs


def _strict_apply(prompt, nid, seg):
    inp = prompt[str(nid)]["inputs"]
    secs = split_seconds(inp.get("分段提示词"))
    want = secs[seg] if seg < len(secs) else None
    loc = _local(inp.get("分段提示词"), seg, prompt)
    defaults = inp.get(DEFAULT_KEY) or {}
    ctx = _context_frames(prompt) if loc > 0 else 0
    done = []
    for tid, node in prompt.items():
        if isinstance(node, dict) and node.get("class_type") in SECONDS_TYPES:
            ni = node.get("inputs") or {}
            d = defaults.get(tid, ni.get("seconds", 5.0))
            if isinstance(d, list):
                continue
            try:
                fps = float(ni.get("fps", H3_FPS))
            except (TypeError, ValueError):
                fps = H3_FPS
            sec, frames, own = plan_seconds(want, d, loc, ctx, fps, seg)
            done.append((tid, sec, frames, own, _context_frames(prompt), fps))
    return done


def describe_seconds(done, seg):
    out = []
    for tid, sec, frames, own, ctx, fps in done:
        body = frames - ctx if seg > 0 else frames
        snap = "" if abs(frames - sec * fps) < 1e-6 else f"（H3 只能 17×n+5 帧，已对齐）"
        out.append(f"{'本段' if own else '默认'} {sec:g} 秒 → {frames} 帧 ≈ {frames / fps:.2f} 秒{snap}"
                   + (f"，去掉 {ctx} 帧上下文后成片 {body} 帧 ≈ {body / fps:.2f} 秒" if seg > 0 else ""))
    return "；".join(out)


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


# MiniMax H3 官方提示词写法（MiniMax-AI/MiniMax-H3 skills/h3-prompt-writing）：
#   基础模式：[可选首行对齐说明 + 空行] integrated_multimodal_description: … / overall_soundscape: … / non_diegetic_music: …
#   全参考模式：subject_definitions: / summary: / retention_analysis: / detailed_description: / … 字段名单独一行
# 官方要求对齐说明是第一行、字段名原样保留，所以触发词不能加在段首，要加在「画面描述」字段的正文开头。
H3_BODY_FIELDS = ("integrated_multimodal_description:", "detailed_description:")


def is_h3_official(seg):
    """这一段是不是 H3 官方结构化写法（有画面描述字段名）。"""
    return any(l.lstrip().startswith(H3_BODY_FIELDS) for l in seg.split("\n"))


def _h3_trigger_seg(lines, trigger):
    """官方写法的一段：触发词加到 integrated_multimodal_description / detailed_description 正文最前面。"""
    sep = aq._trigger_sep(trigger)
    for i, line in enumerate(lines):
        body = line.lstrip()
        f = next((f for f in H3_BODY_FIELDS if body.startswith(f)), None)
        if not f:
            continue
        rest = body[len(f):]
        if rest.strip():  # 字段名和正文在同一行
            txt = rest.lstrip()
            if not aq._starts_with_word(txt, trigger):
                lines[i] = line[:len(line) - len(body)] + f + rest[:len(rest) - len(txt)] + trigger + sep + txt
            return lines
        for j in range(i + 1, len(lines)):  # 字段名单独一行：加在下一行正文前
            b = lines[j].lstrip()
            if b.strip():
                if not is_sep(lines[j]) and not aq._starts_with_word(b, trigger):
                    lines[j] = lines[j][:len(lines[j]) - len(b)] + trigger + sep + b
                return lines
        return lines
    return lines


def add_trigger(text, trigger):
    """和提示词循环一样给每段加触发词；H3 官方写法的段加在画面描述字段里，不破坏首行对齐说明和字段名。"""
    trigger = (trigger or "").strip().rstrip(",，、;；").strip()
    if not trigger or not isinstance(text, str):
        return text
    out, seg = [], []

    def flush():
        if not seg:
            return
        if is_h3_official("\n".join(seg)):
            out.extend(_h3_trigger_seg(seg[:], trigger))
        else:
            out.extend(aq.prepend_trigger("#\n" + "\n".join(seg), trigger).split("\n")[1:])
        seg.clear()
    for line in text.split("\n"):
        if is_sep(line):
            flush()
            out.append(line)
        else:
            seg.append(line)
    flush()
    return "\n".join(out)


def sanitize(name):
    """和 ClipStream 一样的文件夹名规则（只留字母数字、_、-、空格）。"""
    safe = "".join(c for c in (name or "Default_Project") if c.isalnum() or c in ("_", "-", " ")).strip()
    return safe or "Default_Project"


def project_name(base, r, stamp, scene=None):
    """没有 ## 场景：基名_r01_时间戳（和以前一样）；有场景：基名_场景名_r01_时间戳（场景名去掉文件名不能用的字符，空格换成 _）。"""
    if scene is None:
        return f"{(base or 'H3').strip()}_r{r:02d}_{stamp}"
    sc = "".join(c for c in str(scene) if c.isalnum() or c in ("_", "-", " ")).strip().replace(" ", "_") or "场景"
    return f"{(base or 'H3').strip()}_{sc}_r{r:02d}_{stamp}"


def video_prefix(batch, proj, scene_segments, now=None, root="video", tag="H3"):
    """最终成片 SaveVideo 的 filename_prefix：
    - 日期文件夹 = 点运行时的日期（批次时间戳前 8 位），同一批次所有任务都一样，跨过午夜也不变；
    - 一个场景（没有 ## 时就是整条）有多段：放进 video/日期/项目名/（项目名 = 基名[_场景名]_rNN_批次，和 Clip Bin 项目一一对应）；
    - 只有一段：直接放 video/日期/；
    - 文件名照旧 H3-本段开跑时间，ComfyUI 再加 _00001_ 序号。"""
    b = str(batch or "")
    day = b[:8] if re.fullmatch(r"\d{8}", b[:8] or "") else (now or datetime.datetime.now()).strftime("%Y%m%d")
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d%H%M%S")
    sub = f"{proj}/" if scene_segments > 1 else ""
    return f"{root}/{day}/{sub}{tag}-{stamp}"


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
    loc = _local(inp.get("分段提示词"), seg, prompt)
    pickers = {t for t, _ in linked_from(prompt, nid, 0) if prompt[t].get("class_type") in PICKER_TYPES}
    for t in pickers:
        pi = prompt[t]["inputs"]
        opts = _mode_options(prompt[t]["class_type"])
        pi["mode"] = pick_mode("Force Initial" if loc == 0 else "Auto", opts, pi.get("mode"))
        if not isinstance(pi.get("clip_selection"), list):
            pi["clip_selection"] = "latest"
    done = apply_seconds(prompt, nid, seg)
    if any(d[3] for d in done) or loc > 0:
        print(f"[{TAG}] 第 {seg + 1} 段时长：{describe_seconds(done, loc)}")
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
                    inp["分段提示词"] = add_trigger(inp["分段提示词"], trig)  # 和提示词循环一样：点运行时加一次，副本不重复加
                    inp[aq.TRIGGER_KEY] = ""
                if not inp.get("批次"):
                    inp["批次"] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                configure(prompt, nid)
    except Exception as e:
        print(f"[{TAG}] 运行前设置 Picker 出错：{e}")
    return json_data


aq.PRE_HOOKS.append(on_prompt)  # 在自动排队算指纹之前改好（和副本入队走同一个处理）


def seg_seed(base, loc, nid, key):
    """接续段（场景内第 2 段起）的种子：由本轮种子 + 段号确定性地派生，每段不同、可复现；场景首段原样用本轮种子。"""
    if loc <= 0:
        return base
    return random.Random(f"{base}|{nid}|{key}|seg{loc}").randint(0, 2**50)


def round_seed_copy(p, rules, r, mode, round_seeds, loc=0):
    """每轮：随机 / 递增（第 1 轮是原值）/ 不改；再按场景内段号 loc 给接续段派生各自不同的种子
    （同一轮各场景首段仍然同种子；「不改种子」时首段保持原值，接续段也从原值派生，所以每次跑结果一样）。"""
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
                    v = v if r == 0 else round_seeds.setdefault((r, nid, key), random.randint(0, 2**50))
                elif mode == SEED_MODES[1]:
                    v = v + r
                node["inputs"][key] = seg_seed(v, loc, nid, key)
    return p


def _cancel_round(batch, r, my_id, scene=None):
    """把这一轮还没跑的段从队列里撤掉（同批次、同轮；给了 scene 就只撤这个场景的）。"""
    try:
        from server import PromptServer
        q = PromptServer.instance.prompt_queue

        def same(item):
            node = (item[2] or {}).get(str(my_id)) or {}
            inp = node.get("inputs", {})
            if node.get("class_type") != "GoudanSegmentRelay" or inp.get("批次") != batch:
                return False
            n = max(1, len(segment_list(inp)))
            if not (isinstance(inp.get("序号"), int) and inp["序号"] // n == r):
                return False
            if scene is None:
                return True
            plan = scene_plan(inp.get("分段提示词"))
            k = inp["序号"] % n
            return k < len(plan) and plan[k][0] == scene
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
                                     "tooltip": "和文生图的提示词列表一样：用单独一行 # 分段（也认单独一行 ---；一段可以写多行，空段跳过），段数自动 = 非空段数；"
                                                "分段行后面写秒数 = 这一段的时长（# 4、#4、# 4s、# 4秒、# 4.5、# 6sec、--- 5.5 都行），从「本段秒数」输出，"
                                                "也会自动写进 MiniMaxH3Easy 的 seconds；不写就用「默认秒数」（MiniMaxH3Easy 用节点上的值）；"
                                                "H3 视频批量排队：每段一个任务，例如「# 4 换行 提示词 换行 # 5 换行 提示词」；"
                                                "单独一行 ## 场景名 = 新场景（多场景）：每个场景单独一条接力（自己的项目 基名_场景名_r01_时间，第 1 段 Force Initial，不接上一个场景），"
                                                "每轮按场景顺序跑；## 后面不写名字就叫 场景1、场景2…；「## 3」是名叫 3 的场景，不是秒数；不写 ## 和以前完全一样；"
                                                "{a|b|c} 或 __列表名__ 随机（需要 Impact Pack）"}),
            },
            "optional": {
                "每轮随机种子": ("BOOLEAN", {"default": True, "tooltip": "开着时每一轮换新的随机种子（rgthree Seed 和采样器种子都会换）；关着时用工作流里的种子。不管开关，接续段（第 2 段起）都会在本轮种子上按段号派生出各自不同的种子（可复现），避免每段同一噪声叠加导致越接越糊"}),
                "全局触发词": ("STRING", {"default": "", "tooltip": "可空。填了会自动加在每一段提示词最前面（例如 LoRA 触发词），在随机变量展开之前加，空着就什么都不加"}),
                "批次": ("STRING", {"default": "", "tooltip": "自动管理（点运行时填成时间戳），保持空着即可"}),
                "默认秒数": ("FLOAT", {"default": GENERIC_DEFAULT_SECONDS, "min": 0.0, "max": 100000.0, "step": 0.1,
                                   "tooltip": "分段行上没写秒数的段用这个时长。可以把工作流原来的时长节点（例如「视频时长（秒）」）连到这里，"
                                              "再把「本段秒数」输出连到原来时长连去的地方"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "STRING", "INT", "INT", "INT", "STRING", "FLOAT")
    RETURN_NAMES = ("项目名", "本段提示词", "序号", "段", "轮", "视频前缀", "本段秒数")
    OUTPUT_TOOLTIPS = ("连 Clip Bin Picker 和 Saver 的 project_name", "连 MiniMaxH3Easy 的 prompt 和 Saver 的 prompt（已加触发词、已展开随机变量）",
                       "第几个任务（0 起）", "第几段（1 起）", "第几轮（1 起）",
                       "连最终成片 SaveVideo 的 filename_prefix：video/点运行时的日期/项目名/H3-时间（同一批次跨天也在同一个日期文件夹；只有一段的放 video/日期/）",
                       "这一段的时长（秒）：分段行上写了就用写的，没写就是「默认秒数」。连到工作流里任意时长输入"
                       "（例如原来「视频时长（秒）」连去的数学表达式 / 子图入口 / 视频节点的秒数），每个排队任务各自用自己那段的秒数")
    FUNCTION = "run"
    CATEGORY = "狗蛋工具"

    @classmethod
    def IS_CHANGED(cls, **kw):
        return uuid.uuid4().hex

    def run(self, 轮数, 自动排队, 序号, 项目基名, 分段提示词, 每轮随机种子=True, 全局触发词="", 批次="", 默认秒数=GENERIC_DEFAULT_SECONDS,
            prompt=None, extra_pnginfo=None, unique_id=None, **_ignored):
        inp = {"分段提示词": 分段提示词}
        if 全局触发词 and 全局触发词.strip():  # 直接调用（没经过点运行处理）时也加上
            inp["分段提示词"] = add_trigger(分段提示词, 全局触发词)
        segs = segment_list(inp)
        if not segs:
            raise RuntimeError(f"[{TAG}] 「分段提示词」是空的：用单独一行 # 分段，每段写一段提示词。")
        seed_mode = SEED_MODES[0] if 每轮随机种子 else SEED_MODES[2]
        n = len(segs)
        total = n * 轮数
        batch = 批次 or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        r, s = 序号 // n, 序号 % n
        plan = scene_plan(inp["分段提示词"])
        si, sname, loc = plan[s] if s < len(plan) else (0, None, s)
        scenes = split_scenes(inp["分段提示词"])
        sn = len(scenes)
        sc_len = sum(1 for x in plan if x[0] == si)
        chain = uses_clipbin(prompt)  # 没有 ClipStream 节点 = 每段独立出片（H3 视频批量排队）
        if 默认秒数 is None:
            默认秒数 = GENERIC_DEFAULT_SECONDS
        if 序号 == 0:
            gsecs = check_generic_seconds(inp["分段提示词"], 默认秒数)  # 秒数不合法在这里报错，一个副本都不排
            if any(x is not None for x in split_seconds(分段提示词)):
                print(f"[{TAG}] 各段时长（本段秒数）：" + "、".join(f"{x:g}" for x in gsecs) + " 秒")
        if 序号 == 0 and prompt and str(unique_id) in prompt:
            sp = check_all_seconds(prompt, unique_id)  # 秒数不合法在这里报错，后面的段不排
            if any(x is not None for x in split_seconds(分段提示词)) and any(
                    isinstance(v, dict) and v.get("class_type") in SECONDS_TYPES for v in prompt.values()):
                print(f"[{TAG}] 各段时长：" + " | ".join(sp))
            if sname is not None:
                print(f"[{TAG}] 共 {sn} 个场景：" + "，".join(f"「{nm}」{sum(1 for x in plan if x[0] == i)} 段" for i, (nm, _b) in enumerate(scenes))
                      + "。每个场景是一条单独的接力（自己的项目，第 1 段 Force Initial，不接上一个场景），所有场景用同一套参考图；"
                        "每轮按场景顺序跑，同一轮所有场景同一个种子。")
        proj = project_name(项目基名, r + 1, batch, sname)
        if 自动排队 and 序号 == 0 and total > 1 and prompt:
            fp = aq.fingerprint(prompt, extra_pnginfo, unique_id, ("序号", "自动排队", "批次"))
            if aq.is_duplicate(fp, extra_pnginfo):
                print(f"[{TAG}] 队列里已经有一套内容完全相同的任务（重复点了运行），这次整次跳过。")
                return aq.blocked_outputs(7)
            if not aq._first_auto_node(prompt, unique_id):
                print(f"[{TAG}] 图里有多个开着「自动排队」的节点，只由 id 最小的那个负责排队，这个不排。")
            else:
                rules = aq._seed_rules(extra_pnginfo)
                round_seeds = {}

                def gen():
                    for k in range(1, total):
                        p = aq.make_copy(prompt, extra_pnginfo, k, unique_id, {"序号": k, "自动排队": False, "批次": batch}, rules={})
                        yield round_seed_copy(p, dict(rules), k // n, seed_mode, round_seeds, _local(inp["分段提示词"], k % n, prompt))
                aq.queue_copies(gen(), extra_pnginfo, TAG, fp, prompt, total=total - 1)
                print(f"[{TAG}] 共 {n} 段{f'（{sn} 个场景）' if sname is not None else ''} × {轮数} 轮 = {total} 个任务，"
                      f"已把剩下 {total - 1} 个按 {'轮→场景→段' if sname is not None else '轮→段'} 顺序排进队列")
        have = clip_count(proj) if chain else loc
        where = f"场景「{sname}」第 {loc + 1}/{sc_len} 段" if sname is not None else f"第 {s + 1} 段"
        if have != loc:
            left = _cancel_round(batch, r, unique_id, si if sname is not None else None)
            raise RuntimeError(
                f"[{TAG}] 第 {r + 1} 轮{where}开跑前检查失败：项目「{proj}」里应该已有 {loc} 段，实际有 {have} 段"
                f"（{'上一段没有存进 Clip Bin，可能上一段出错或 Saver 的 project_name 没连本节点' if have < loc else '项目里有别的镜头，项目名可能重复'}）。"
                + (f"为了不接错段，这一段不跑，这个场景本轮后面的 {left} 段也已从队列撤掉；其它场景、其它轮不受影响。" if sname is not None else
                   f"为了不接错段，这一段不跑，本轮后面的 {left} 段也已从队列撤掉；其它轮不受影响。"))
        text = expand_random(segs[s], random.Random(f"{batch}|{序号}").randint(0, 2**32 - 1))
        print(f"[{TAG}] 第 {r + 1}/{轮数} 轮 " + (f"场景 {si + 1}/{sn}「{sname}」第 {loc + 1}/{sc_len} 段" if sname is not None else f"第 {s + 1}/{n} 段")
              + f"，项目 {proj}：{text[:60]}")
        sec = segment_seconds(inp["分段提示词"], s, 默认秒数)
        print(f"[{TAG}] 本段秒数 {sec:g}")
        return (proj, text, 序号, s + 1, r + 1, video_prefix(batch, proj, sc_len), sec)
