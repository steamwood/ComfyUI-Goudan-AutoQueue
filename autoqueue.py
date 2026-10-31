"""自动排队：在第一次运行时，把剩下的运行次数自己排进 ComfyUI 队列。"""
import copy
import hashlib
import json
import random
import ssl
import threading
import urllib.request
import uuid

SEED_KEYS = ("seed", "noise_seed")


def _server_url():
    try:
        from server import PromptServer
        from comfy.cli_args import args
        srv = PromptServer.instance
        port = getattr(srv, "port", None) or args.port
        addr = getattr(srv, "address", None) or "127.0.0.1"
        if addr in ("0.0.0.0", "::", "", None) or "," in str(addr):
            addr = "127.0.0.1"
        if ":" in addr and not addr.startswith("["):
            addr = f"[{addr}]"
        scheme = "https" if getattr(args, "tls_keyfile", None) else "http"
        return f"{scheme}://{addr}:{port}/prompt"
    except Exception:
        return "http://127.0.0.1:8188/prompt"


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


def queue_copies(prompts, extra_pnginfo, tag, fp=None, prompt=None):
    url = _server_url()
    base = _running_number(prompt) if prompt is not None else None
    ctx = ssl._create_unverified_context() if url.startswith("https") else None
    extra = dict(extra_pnginfo or {})
    extra.pop(DUP_KEY, None)
    if fp:
        extra[FP_KEY] = fp
        extra[COPY_KEY] = True
        with _posting_lock:
            _posting[fp] = _posting.get(fp, 0) + 1

    def run():
        ok = 0
        try:
            for i, p in enumerate(prompts, 1):
                data = {"prompt": p, "extra_data": {"extra_pnginfo": extra}}
                if base is not None:
                    data["number"] = base + i / (len(prompts) + 1)
                body = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                try:
                    urllib.request.urlopen(req, timeout=60, context=ctx).read()
                    ok += 1
                except Exception as e:
                    print(f"[{tag}] 自动排队失败（已排 {ok} 个）：{e}")
                    return
            print(f"[{tag}] 已自动排队 {ok} 次")
        finally:
            if fp:
                with _posting_lock:
                    _posting[fp] -= 1
                    if not _posting[fp]:
                        del _posting[fp]

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(120)  # 等副本都进了队列再往下跑，免得后面点的任务先开始、两套交错


def count_items(strings):
    """和 Impact 的 String Selector（多行模式）一样的分条规则。"""
    result, cur = [], ""
    for line in strings.split("\n"):
        if line.startswith("#"):
            if cur:
                result.append(cur.strip())
                cur = ""
        cur += line + "\n"
    if cur:
        result.append(cur.strip())
    return max(1, len(result))


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

    RETURN_TYPES = ("INT", "INT")
    RETURN_NAMES = ("序号", "条数")
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
                return count_items(inp["strings"])
        raise RuntimeError("没找到连着「序号」的提示词列表节点（Impact String Selector，多行模式）")

    def run(self, 轮数, 自动排队, 序号, 每张随机种子=True, prompt=None, extra_pnginfo=None, unique_id=None):
        n = self._count(prompt, unique_id)
        total = n * 轮数
        if 自动排队 and 序号 == 0 and total > 1 and prompt:
            fp = fingerprint(prompt, extra_pnginfo, unique_id, ("序号", "自动排队"))
            if is_duplicate(fp, extra_pnginfo):
                print("[提示词循环] 队列里已经有一套内容完全相同的任务（重复点了运行），这次整次跳过，不再出图也不再排队。"
                      "改过提示词/模型/LoRA/轮数的新任务会正常排在后面。")
                return blocked_outputs(2)
            copies = [make_copy(prompt, extra_pnginfo, k, unique_id, {"序号": k, "自动排队": False}, force_random=每张随机种子) for k in range(1, total)]
            queue_copies(copies, extra_pnginfo, "提示词循环", fp, prompt)
            print(f"[提示词循环] 共 {n} 条 × {轮数} 轮 = {total} 次，正在把剩下 {total - 1} 次排进队列")
        print(f"[提示词循环] 第 {序号 + 1}/{total} 次：第 {序号 % n + 1} 条，第 {序号 // n + 1} 轮")
        return (序号, n)
