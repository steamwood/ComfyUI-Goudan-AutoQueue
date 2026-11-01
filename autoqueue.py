"""自动排队：在第一次运行时，把剩下的运行次数自己排进 ComfyUI 队列。"""
import asyncio
import copy
import hashlib
import inspect
import json
import random
import threading
import time
import uuid

from .filenames import resolve_prompt_dates

SEED_KEYS = ("seed", "noise_seed")


def _seed_rules(extra_pnginfo):
    """从界面工作流里读出哪些种子是“随机/递增/递减”，返回 {节点id: (输入名, 模式)}。"""
    rules = {}
    try:
        nodes = (extra_pnginfo or {}).get("workflow", {}).get("nodes", [])
    except AttributeError:
        return rules
    for n in nodes:
        wv = n.get("widgets_values")
        nid = str(n.get("id"))
        if not isinstance(wv, list) or not wv:
            continue
        if n.get("type") == "Seed (rgthree)":
            v = wv[0]
            mode = {-1: "randomize", -2: "increment", -3: "decrement"}.get(v)
            if mode:
                rules[nid] = (("seed",), mode)
            continue
        for j, v in enumerate(wv):
            if v in ("randomize", "increment", "decrement") and j > 0 and isinstance(wv[j - 1], int) and not isinstance(wv[j - 1], bool):
                rules[nid] = (SEED_KEYS, v)
                break
    return rules


def make_copy(prompt, extra_pnginfo, k, my_id, overrides, force_random=False):
    p = copy.deepcopy(prompt)
    # ComfyUI 执行时会往 prompt 里写 "is_changed"（NaN）。原样带进副本会让所有副本的缓存签名相同，
    # 导致后面的副本整图命中缓存、不重新计算（放大一直停在第二张、提示词轮次重复）。必须去掉。
    for node in p.values():
        if isinstance(node, dict):
            node.pop("is_changed", None)
    p[str(my_id)]["inputs"].update(overrides)
    rules = _seed_rules(extra_pnginfo)
    if force_random:
        # rgthree Seed 在排队时会把 -1 换成实际种子写进工作流，读不出“随机”模式，所以直接全部随机
        for nid, node in p.items():
            if isinstance(node, dict) and node.get("class_type") == "Seed (rgthree)":
                rules[nid] = (("seed",), "randomize")
            elif isinstance(node, dict) and nid in rules:
                rules[nid] = (rules[nid][0], "randomize")
    for nid, (keys, mode) in rules.items():
        node = p.get(nid)
        if not node:
            continue
        for key in keys:
            v = node.get("inputs", {}).get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                if mode == "randomize":
                    node["inputs"][key] = random.randint(0, 2**50)
                elif mode == "increment":
                    node["inputs"][key] = v + k
                else:
                    node["inputs"][key] = max(0, v - k)
    return p


FP_KEY = "goudan_autoqueue_fp"  # 写在 extra_pnginfo 里的内容指纹，标记它属于哪一套
COPY_KEY = "goudan_autoqueue_copy"  # 自动排出来的副本才有
_posting = {}  # 正在后台提交副本的指纹 -> 线程数（副本还没进队列时也要算“已排”）
_posting_lock = threading.Lock()


def fingerprint(prompt, extra_pnginfo, my_id, drop_keys):
    """原始任务的内容指纹：去掉每次点运行都会变的部分（is_changed、本节点的序号/自动排队、随机种子），
    内容完全一样才算“连点重复”；改了提示词/模型/LoRA/轮数/路径就是新的一套。"""
    p = copy.deepcopy(prompt)
    volatile = {nid: keys for nid, (keys, mode) in _seed_rules(extra_pnginfo).items()}
    for nid, node in p.items():
        if not isinstance(node, dict):
            continue
        node.pop("is_changed", None)
        inp = node.get("inputs", {})
        if nid == str(my_id):
            for k in drop_keys:
                inp.pop(k, None)
        keys = ("seed",) if node.get("class_type") == "Seed (rgthree)" else volatile.get(nid, ())
        for k in keys:
            if isinstance(inp.get(k), int) and not isinstance(inp.get(k), bool):
                inp[k] = "*"
    return hashlib.sha1(json.dumps(p, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def pending_copies(fp):
    """队列里（或正在提交中）还有同一份内容排出来的副本就返回数量；内容不同的另一套、以及还没跑的原始任务都不算。"""
    with _posting_lock:
        if _posting.get(fp):
            return _posting[fp]
    try:
        from server import PromptServer
        running, queued = (getattr(PromptServer.instance.prompt_queue, "get_current_queue_volatile", None) or PromptServer.instance.prompt_queue.get_current_queue)()
        n = 0
        for item in list(queued):
            extra = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
            pi = extra.get("extra_pnginfo") or {}
            if pi.get(FP_KEY) == fp and pi.get(COPY_KEY):
                n += 1
        return n
    except Exception:
        return 0


AUTO_NODES = {"PromptCycleAutoQueue": ("序号", "自动排队"), "SeedVR2DirLoad": ("自动排队",)}
DUP_KEY = "goudan_autoqueue_dup"


def _active_fps():
    """队列里（正在跑 + 排队中）所有带指纹的任务的指纹。"""
    from server import PromptServer
    running, queued = (getattr(PromptServer.instance.prompt_queue, "get_current_queue_volatile", None) or PromptServer.instance.prompt_queue.get_current_queue)()
    fps = set()
    for item in list(running) + list(queued):
        extra = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
        fp = (extra.get("extra_pnginfo") or {}).get(FP_KEY)
        if fp:
            fps.add(fp)
    return fps


def _on_prompt(json_data):
    """每次点运行（POST /prompt）时：给原始任务打上内容指纹；如果同样内容的一套还在队列里没跑完，就标记成“重复点击”。
    这样后点的那次即使排在整套副本后面才轮到，也能认出来是重复的。"""
    try:
        prompt = json_data.get("prompt")
        if not isinstance(prompt, dict):
            return json_data
        # 先定下日期（必须在算指纹之前，原始任务执行时重算的指纹才能对上）
        resolve_prompt_dates(prompt)
        hit = next(((nid, n["class_type"]) for nid, n in prompt.items() if isinstance(n, dict)
                    and n.get("class_type") in AUTO_NODES and n.get("inputs", {}).get("自动排队") is True), None)
        if not hit:
            return json_data
        extra = json_data.setdefault("extra_data", {})
        pi = extra.get("extra_pnginfo")
        if not isinstance(pi, dict):
            pi = extra["extra_pnginfo"] = {}
        fp = fingerprint(prompt, pi, hit[0], AUTO_NODES[hit[1]])
        with _posting_lock:
            posting = bool(_posting.get(fp))
        if posting or fp in _active_fps():
            pi[DUP_KEY] = True
        pi[FP_KEY] = fp
    except Exception as e:
        print(f"[狗蛋自动排队] 检查重复点击出错（不影响运行）：{e}")
    return json_data


try:
    from server import PromptServer
    PromptServer.instance.add_on_prompt_handler(_on_prompt)
except Exception:
    pass


def is_duplicate(fp, extra_pnginfo):
    """这次原始运行是不是“重复点击”：点运行那一刻同样内容的一套还没跑完，或者现在队列里还有它的副本。"""
    if isinstance(extra_pnginfo, dict) and extra_pnginfo.get(DUP_KEY) and extra_pnginfo.get(FP_KEY) == fp:
        return True
    return bool(pending_copies(fp))


def blocked_outputs(n):
    """重复点击时让这次运行什么都不做（下游节点全部跳过）。"""
    try:
        from comfy_execution.graph_utils import ExecutionBlocker
    except ImportError:
        from comfy_execution.graph import ExecutionBlocker
    return tuple(ExecutionBlocker(None) for _ in range(n))


def _running_number(prompt):
    """当前正在跑的这次任务在队列里的编号（副本用 编号+小数 插在它后面，保证整套连续、不和后面点的任务交错）。"""
    try:
        from server import PromptServer
        running, _ = (getattr(PromptServer.instance.prompt_queue, "get_current_queue_volatile", None) or PromptServer.instance.prompt_queue.get_current_queue)()
        mine = [it for it in running if it[2] is prompt] or (running if len(running) == 1 else [])
        return float(mine[0][0]) if mine else None
    except Exception:
        return None


QUEUED_EVENT = "goudan-autoqueue.queued"
UI_QUEUE_LIMIT = 200  # 新版前端队列面板只取最新的 200 个排队任务来显示（多出来的照常执行，只是看不到）


def _queue_item_len(srv):
    """这个版本 ComfyUI 队列里每一项有几个字段（新版 6 个：多了 sensitive；老版 5 个）。"""
    running, queued = (getattr(srv.prompt_queue, "get_current_queue_volatile", None) or srv.prompt_queue.get_current_queue)()
    for item in list(running) + list(queued):
        if len(item) in (5, 6):
            return len(item)
    return None


def _validate(srv, prompt_id, p):
    """和 /prompt 一样校验一份副本（兼容新旧版本的 validate_prompt 参数和同步/异步写法）。"""
    import execution
    fn = execution.validate_prompt
    args = []
    for name in inspect.signature(fn).parameters:
        if name == "prompt_id":
            args.append(prompt_id)
        elif name == "prompt":
            args.append(p)
        else:
            args.append(None)
    res = fn(*args)
    if inspect.isawaitable(res):
        res = asyncio.run_coroutine_threadsafe(res, srv.loop).result(timeout=120)
    return res


def _client_id(srv):
    """发起这次运行的页面的 client_id（ComfyUI 执行时会把当前任务的 client_id 放在 srv.client_id），
    副本任务带上它，进度/图片/文字就能显示到发起页面上。没有时为 None，ComfyUI 会发给所有打开的页面。"""
    return getattr(srv, "client_id", None) or None
    if clients:
        return list(clients.keys())[-1]
    return cid


def _queue_direct(srv, item_len, prompts, extra, numbers, tag):
    """直接按 /prompt 的流程（on_prompt 处理 → 校验 → 入队）把副本放进队列。返回 (成功数, 失败数)。
    中途出错不再改用别的方式补排，只在日志里报错，并把没排的算进失败数。"""
    import server as server_mod
    ok = 0
    bad = 0
    cid = _client_id(srv)
    try:
        for p, number in zip(prompts, numbers):
            json_data = {"prompt": p, "extra_data": {"extra_pnginfo": extra}, "number": number}
            if cid:
                json_data["client_id"] = cid
                json_data["extra_data"]["client_id"] = cid
            json_data = srv.trigger_on_prompt(json_data)
            p = json_data["prompt"]
            replacer = getattr(srv, "node_replace_manager", None)
            if replacer is not None and hasattr(replacer, "apply_replacements"):
                replacer.apply_replacements(p)
            prompt_id = str(uuid.uuid4())
            valid = _validate(srv, prompt_id, p)
            if not valid[0]:
                bad += 1
                if bad <= 3:
                    print(f"[{tag}] 副本校验没通过，跳过这一个：{valid[1]} {valid[3]}")
                continue
            extra_data = dict(json_data.get("extra_data") or {})
            extra_data.pop("workflow_metadata", None)
            meta_fn = getattr(server_mod, "workflow_metadata_from_prompt", None)
            if meta_fn is not None:
                meta = meta_fn(extra_data)
                if meta is not None:
                    extra_data["workflow_metadata"] = meta
            extra_data["create_time"] = int(time.time() * 1000)
            item = (number, prompt_id, p, extra_data, valid[2])
            if item_len == 6:
                item = item + ({},)
            srv.prompt_queue.put(item)
            ok += 1
    except Exception as e:
        left = len(prompts) - ok - bad
        print(f"[{tag}] 出错：直接入队中途失败，已排进去 {ok} 个，剩下 {left} 个没有排：{e}")
        bad += left
    return ok, bad


def _notify(srv, tag, ok, bad):
    """在界面右上角弹提示：排了多少个；超过 200 个时说明队列面板只显示 200 个、其余不会丢。"""
    try:
        running, queued = (getattr(srv.prompt_queue, "get_current_queue_volatile", None) or srv.prompt_queue.get_current_queue)()
        waiting = len(queued)
        print(f"[{tag}] 已自动排队 {ok} 次（现在队列里共 {waiting} 个等待中）" + (f"，{bad} 个没排进去" if bad else ""))
        if waiting > UI_QUEUE_LIMIT:
            print(f"[{tag}] 提示：ComfyUI 的队列面板最多只显示 {UI_QUEUE_LIMIT} 个排队任务，其余的都在队列里，会照常按顺序执行。")
        srv.send_sync(QUEUED_EVENT, {"tag": tag, "queued": ok, "failed": bad, "waiting": waiting, "ui_limit": UI_QUEUE_LIMIT})
    except Exception as e:
        print(f"[{tag}] 已自动排队 {ok} 次（{e}）")


def queue_copies(prompts, extra_pnginfo, tag, fp=None, prompt=None):
    """把副本排进队列：编号 = 当前任务编号 + 小数，整套连续排在当前任务后面。
    排完才返回（同步），所以不会有“还没排完就开始跑下一个”的问题，数量也没有上限。"""
    from server import PromptServer
    srv = PromptServer.instance
    base = _running_number(prompt) if prompt is not None else None
    total = len(prompts)
    if base is not None:
        numbers = [base + i / (total + 1) for i in range(1, total + 1)]
    else:
        numbers = [None] * total
    extra = dict(extra_pnginfo or {})
    extra.pop(DUP_KEY, None)
    if fp:
        extra[FP_KEY] = fp
        extra[COPY_KEY] = True
        with _posting_lock:
            _posting[fp] = _posting.get(fp, 0) + 1
    t0 = time.time()
    ok = 0
    bad = 0
    try:
        item_len = None
        try:
            item_len = _queue_item_len(srv)
        except Exception:
            pass
        if item_len is None or getattr(srv, "loop", None) is None:
            print(f"[{tag}] 出错：读不到 ComfyUI 队列（这个版本的 ComfyUI 可能不兼容），没法自动排队，{total} 个副本都没排进去。")
            bad = total
        else:
            if None in numbers:
                start = srv.number
                srv.number += total
                numbers = [start + i for i in range(total)]
            ok, bad = _queue_direct(srv, item_len, prompts, extra, numbers, tag)
    finally:
        if fp:
            with _posting_lock:
                _posting[fp] -= 1
                if not _posting[fp]:
                    del _posting[fp]
    print(f"[{tag}] 排队用时 {time.time() - t0:.2f} 秒")
    _notify(srv, tag, ok, bad)
    return ok


def split_items(strings):
    """和 Impact 的 String Selector（多行模式）一样的分条规则（含空段）。"""
    result, cur = [], ""
    for line in strings.split("\n"):
        if line.startswith("#"):
            if cur:
                result.append(cur.strip())
                cur = ""
        cur += line + "\n"
    if cur:
        result.append(cur.strip())
    return result


def nonempty_index(strings):
    """非空段在 Impact 原始分条里的下标。空段 = 去掉 # 行和空行后没有内容。"""
    items = split_items(strings)
    idx = [i for i, t in enumerate(items)
           if any(l.strip() and not l.startswith("#") for l in t.split("\n"))]
    return idx or [0]


def count_items(strings):
    return len(nonempty_index(strings))


class PromptCycleAutoQueue:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "轮数": ("INT", {"default": 1, "min": 1, "max": 10000, "tooltip": "所有提示词轮流跑几遍。总次数 = 条数 × 轮数，自动排队。"}),
                "自动排队": ("BOOLEAN", {"default": True}),
                "序号": ("INT", {"default": 0, "min": 0, "max": 2**31 - 1, "tooltip": "自动管理，保持 0 即可"}),
            },
            "optional": {
                "每张随机种子": ("BOOLEAN", {"default": True, "tooltip": "开着时自动排进去的每一张都换新的随机种子（rgthree Seed 和采样器种子都会换）"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("INT", "INT", "INT")
    RETURN_NAMES = ("序号", "条数", "选择下标")
    OUTPUT_TOOLTIPS = ("第几次（0 起，连续），连「文件名/文件夹」的 序号", "非空提示词条数", "连 Impact String Selector 的 select：跳过空段后的原始下标")
    FUNCTION = "run"
    CATEGORY = "狗蛋工具"

    @classmethod
    def IS_CHANGED(cls, **kw):
        return uuid.uuid4().hex  # 每次都不同，保证不吃缓存

    def _count(self, prompt, my_id):
        for nid, node in (prompt or {}).items():
            if node.get("class_type") != "ImpactStringSelector":
                continue
            inp = node.get("inputs", {})
            sel = inp.get("select")
            if isinstance(sel, list) and str(sel[0]) == str(my_id) and inp.get("multiline") is True and isinstance(inp.get("strings"), str):
                return nonempty_index(inp["strings"])
        raise RuntimeError("没找到连着「序号」的提示词列表节点（Impact String Selector，多行模式）")

    def run(self, 轮数, 自动排队, 序号, 每张随机种子=True, prompt=None, extra_pnginfo=None, unique_id=None):
        idx = self._count(prompt, unique_id)
        n = len(idx)
        total = n * 轮数
        if 自动排队 and 序号 == 0 and total > 1 and prompt:
            fp = fingerprint(prompt, extra_pnginfo, unique_id, ("序号", "自动排队"))
            if is_duplicate(fp, extra_pnginfo):
                print("[提示词循环] 队列里已经有一套内容完全相同的任务（重复点了运行），这次整次跳过，不再出图也不再排队。"
                      "改过提示词/模型/LoRA/轮数的新任务会正常排在后面。")
                return blocked_outputs(3)
            copies = [make_copy(prompt, extra_pnginfo, k, unique_id, {"序号": k, "自动排队": False}, force_random=每张随机种子) for k in range(1, total)]
            queue_copies(copies, extra_pnginfo, "提示词循环", fp, prompt)
            print(f"[提示词循环] 共 {n} 条 × {轮数} 轮 = {total} 次，已把剩下 {total - 1} 次排进队列")
        print(f"[提示词循环] 第 {序号 + 1}/{total} 次：第 {序号 % n + 1} 条，第 {序号 // n + 1} 轮")
        # 序号连续（文件编号不跳号）；选择下标 = 第 序号%n 个非空段在 Impact 原始分条里的位置
        return (序号, n, idx[序号 % n])
