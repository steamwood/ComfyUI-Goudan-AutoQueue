"""自动排队：在第一次运行时，把剩下的运行次数自己排进 ComfyUI 队列。"""
import copy, json, random, ssl, threading, urllib.request, uuid

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


_posting = {}  # 正在后台往队列里提交副本的节点类型 -> 线程数（副本还没进队列时也要算“已排”）
_posting_lock = threading.Lock()


def pending_copies(class_type):
    """队列里已经有本节点排出来的副本（自动排队=False）就返回数量，防止重复点运行/批量次数>1 时又排一整套。"""
    with _posting_lock:
        if _posting.get(class_type):
            return _posting[class_type]
    try:
        from server import PromptServer
        running, queued = PromptServer.instance.prompt_queue.get_current_queue()
        n = 0
        for item in list(queued):
            p = item[2]
            if any(isinstance(v, dict) and v.get("class_type") == class_type and v.get("inputs", {}).get("自动排队") is False for v in p.values()):
                n += 1
        return n
    except Exception:
        return 0


def queue_copies(prompts, extra_pnginfo, tag, class_type=None):
    url = _server_url()
    ctx = ssl._create_unverified_context() if url.startswith("https") else None
    if class_type:
        with _posting_lock:
            _posting[class_type] = _posting.get(class_type, 0) + 1

    def run():
        ok = 0
        try:
            for p in prompts:
                body = json.dumps({"prompt": p, "extra_data": {"extra_pnginfo": extra_pnginfo}}).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                try:
                    urllib.request.urlopen(req, timeout=60, context=ctx).read()
                    ok += 1
                except Exception as e:
                    print(f"[{tag}] 自动排队失败（已排 {ok} 个）：{e}")
                    return
            print(f"[{tag}] 已自动排队 {ok} 次")
        finally:
            if class_type:
                with _posting_lock:
                    _posting[class_type] -= 1

    threading.Thread(target=run, daemon=True).start()


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
        if 自动排队 and 序号 == 0 and total > 1 and prompt and pending_copies("PromptCycleAutoQueue"):
            print("[提示词循环] 队列里已经有排好的任务，这次不再重复排队（不要连点运行，批量次数保持 1）")
        elif 自动排队 and 序号 == 0 and total > 1 and prompt:
            copies = [make_copy(prompt, extra_pnginfo, k, unique_id, {"序号": k, "自动排队": False}, force_random=每张随机种子) for k in range(1, total)]
            queue_copies(copies, extra_pnginfo, "提示词循环", "PromptCycleAutoQueue")
            print(f"[提示词循环] 共 {n} 条 × {轮数} 轮 = {total} 次，正在把剩下 {total - 1} 次排进队列")
        print(f"[提示词循环] 第 {序号 + 1}/{total} 次：第 {序号 % n + 1} 条，第 {序号 // n + 1} 轮")
        return (序号, n)
