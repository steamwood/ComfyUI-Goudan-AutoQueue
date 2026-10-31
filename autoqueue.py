"""自动排队：在第一次运行时，把剩下的运行次数自己排进 ComfyUI 队列。"""
import copy, json, random, ssl, threading, urllib.request

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


def make_copy(prompt, extra_pnginfo, k, my_id, overrides):
    p = copy.deepcopy(prompt)
    p[str(my_id)]["inputs"].update(overrides)
    for nid, (keys, mode) in _seed_rules(extra_pnginfo).items():
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


def queue_copies(prompts, extra_pnginfo, tag):
    url = _server_url()
    ctx = ssl._create_unverified_context() if url.startswith("https") else None

    def run():
        ok = 0
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
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("INT", "INT")
    RETURN_NAMES = ("序号", "条数")
    FUNCTION = "run"
    CATEGORY = "狗蛋工具"

    @classmethod
    def IS_CHANGED(cls, **kw):
        return float("nan")

    def _count(self, prompt, my_id):
        for nid, node in (prompt or {}).items():
            if node.get("class_type") != "ImpactStringSelector":
                continue
            inp = node.get("inputs", {})
            sel = inp.get("select")
            if isinstance(sel, list) and str(sel[0]) == str(my_id) and inp.get("multiline") is True and isinstance(inp.get("strings"), str):
                return count_items(inp["strings"])
        raise RuntimeError("没找到连着「序号」的提示词列表节点（Impact String Selector，多行模式）")

    def run(self, 轮数, 自动排队, 序号, prompt=None, extra_pnginfo=None, unique_id=None):
        n = self._count(prompt, unique_id)
        total = n * 轮数
        if 自动排队 and 序号 == 0 and total > 1 and prompt:
            copies = [make_copy(prompt, extra_pnginfo, k, unique_id, {"序号": k, "自动排队": False}) for k in range(1, total)]
            queue_copies(copies, extra_pnginfo, "提示词循环")
            print(f"[提示词循环] 共 {n} 条 × {轮数} 轮 = {total} 次，正在把剩下 {total - 1} 次排进队列")
        print(f"[提示词循环] 第 {序号 + 1}/{total} 次：第 {序号 % n + 1} 条，第 {序号 // n + 1} 轮")
        return (序号, n)
