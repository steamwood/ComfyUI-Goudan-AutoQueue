"""还原记录：每保存一张图，就在这个日期文件夹的「还原记录.csv」里追加一行（UTF-8 BOM，Excel 直接打开）。
第一列是文件名（相对 CSV 所在文件夹，例如 usd/01_Krea2-USD_00001_.jpg），按文件名搜索就能找到这张图的
模型、LoRA、实际提示词（随机展开 + 触发词之后）、种子、各步采样设置、尺寸、段/轮。
数据从正在执行的工作流（PROMPT）里自动读：从保存节点往上游找，开关节点只走实际生效的那一路。
实际提示词靠保存节点的可选输入「实际提示词」（连随机展开后的文字）。只写本地文件，不联网。"""
import csv
import datetime
import io
import os
import threading
import time

FILE_NAME = "还原记录.csv"
COLUMNS = ("文件", "时间", "保存节点", "段/轮", "实际提示词", "种子", "模型", "LoRA", "采样设置", "其他设置", "尺寸", "源文件")
SAMPLER_KEYS = ("seed", "noise_seed", "steps", "cfg", "sampler_name", "scheduler", "denoise", "start_at_step", "end_at_step", "upscale_by")
SETTING_CLASSES = ("GoudanScaleSettings", "SeedVR2PostProcessing", "ImageSmartSharpen+", "LatentUpscaleBy", "ResolutionSelector",
                   "TESpeedVOSR2Loader", "TESpeedVOSR2Settings", "TESpeedVOSR2Image",
                   "ColorMatchV2", "ColorMatch", "easy imageColorMatch")
# 只用来排执行顺序的输入（不是图像来源），还原记录不往这些输入的上游追
ORDER_ONLY = {"VRAM_Debug": ("any_input",)}
_lock = threading.Lock()


def _titles(extra_pnginfo):
    """节点标题表。子图里的节点按执行 id（如 "405:18"）登记，嵌套最多 3 层。"""
    out = {}
    try:
        wf = (extra_pnginfo or {}).get("workflow", {}) or {}
        defs = {d.get("id"): d for d in (wf.get("definitions", {}) or {}).get("subgraphs", []) or []}

        def walk(nodes, prefix, depth):
            for n in nodes or []:
                nid = prefix + str(n.get("id"))
                if n.get("title"):
                    out[nid] = n["title"]
                sub = defs.get(n.get("type"))
                if sub and depth < 3:
                    walk(sub.get("nodes"), nid + ":", depth + 1)

        walk(wf.get("nodes", []), "", 0)
    except (AttributeError, TypeError):
        pass
    return out


def _id_key(x):
    return tuple(int(p) if p.isdigit() else 0 for p in str(x).split(":"))


def _is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)


def _literal(prompt, v, depth=0):
    """把连线换成上游的常量（Seed/Primitive 这类只有一个常量输入的节点）。换不出来返回 None。"""
    if not _is_link(v):
        return v
    if depth > 4:
        return None
    node = prompt.get(v[0]) or {}
    vals = [x for k, x in node.get("inputs", {}).items() if not isinstance(x, dict)]
    if len(vals) == 1:
        return _literal(prompt, vals[0], depth + 1)
    seed = node.get("inputs", {}).get("seed")
    return _literal(prompt, seed, depth + 1) if seed is not None else None


def upstream(prompt, start):
    """保存节点上游实际用到的节点（ComfySwitchNode 按开关值只走一路；开关值读不出时两路都算）。"""
    seen, todo = [], [str(start)]
    while todo:
        nid = todo.pop()
        if nid in seen or nid not in prompt:
            continue
        seen.append(nid)
        node = prompt[nid]
        inp = node.get("inputs", {})
        keys = list(inp)
        if node.get("class_type") in ORDER_ONLY:
            keys = [k for k in keys if k not in ORDER_ONLY[node["class_type"]]]
        if node.get("class_type") == "ComfySwitchNode":
            sw = _literal(prompt, inp.get("switch"))
            if isinstance(sw, bool):
                keys = ["on_true" if sw else "on_false"]
        for k in keys:
            if _is_link(inp.get(k)):
                todo.append(inp[k][0])
    return seen


def _fmt(v):
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def collect(prompt, unique_id, extra_pnginfo):
    titles = _titles(extra_pnginfo)
    ids = upstream(prompt, unique_id)
    models, loras, samplers, seeds, others = [], [], [], [], []
    for nid in sorted(ids, key=_id_key):
        node = prompt[nid]
        ct = node.get("class_type", "")
        inp = node.get("inputs", {})
        name = titles.get(nid) or ct
        if ct == "Power Lora Loader (rgthree)":
            for k, v in sorted(inp.items()):
                if k.lower().startswith("lora_") and isinstance(v, dict) and v.get("on") and v.get("lora"):
                    loras.append(f"{v['lora']} : {_fmt(v.get('strength', 1))}")
            continue
        if ct in SETTING_CLASSES:
            vals = [f"{k}={_fmt(v)}" for k, v in inp.items() if not _is_link(v) and not isinstance(v, dict)]
            others.append(f"{name}: " + " ".join(vals))
            continue
        if "Loader" in ct or "LoraLoader" in ct:
            for k, v in inp.items():
                if k.endswith("_name") and isinstance(v, str):
                    if "lora" in k:
                        loras.append(f"{v} : {_fmt(inp.get('strength_model', 1))}")
                    else:
                        models.append(f"{k}={v}")
            continue
        if "steps" in inp or "sampler_name" in inp:
            parts = []
            for k in SAMPLER_KEYS:
                if k in inp:
                    val = _literal(prompt, inp[k])
                    if val is not None:
                        parts.append(f"{k}={_fmt(val)}")
                        if k in ("seed", "noise_seed"):
                            seeds.append(f"{name}: {val}")
            samplers.append(f"[{nid}] {name}: " + " ".join(parts))
            continue
    return {"模型": "\n".join(models), "LoRA": "\n".join(loras), "采样设置": "\n".join(samplers),
            "种子": "\n".join(seeds), "其他设置": "\n".join(others)}


def segment_info(prompt, ids=None):
    """提示词循环的 第几段 / 第几轮（没有提示词循环时为空）。"""
    from .autoqueue import nonempty_index
    for nid, node in prompt.items():
        if not isinstance(node, dict) or node.get("class_type") != "PromptCycleAutoQueue" or (ids is not None and nid not in ids):
            continue
        k = node.get("inputs", {}).get("序号")
        if not isinstance(k, int):
            continue
        for other in prompt.values():
            oi = other.get("inputs", {}) if isinstance(other, dict) else {}
            sel = oi.get("select")
            if other.get("class_type") == "ImpactStringSelector" and _is_link(sel) and sel[0] == str(nid):
                n = len(nonempty_index(oi.get("strings") or ""))
                return f"第 {k % n + 1}/{n} 段 第 {k // n + 1} 轮（序号 {k}）"
    return ""


def _clean_text(text):
    """去掉分段用的 # 行，只留真正的提示词内容。"""
    lines = [l for l in str(text or "").split("\n") if not l.startswith("#")]
    return "\n".join(lines).strip()


def record_dir_for_output(subfolder, out_dir):
    """ComfyUI output 里：日期文件夹（子文件夹的第一层）；没有子文件夹就是 output 本身。"""
    first = (subfolder or "").replace("\\", "/").strip("/").split("/")[0]
    return os.path.join(out_dir, first) if first else out_dir


PENDING_NAME = "还原记录_待合并.csv"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(v):
    """以 = + - @ 开头的文字 Excel 会当公式执行，前面加 ' 让它按文字显示。"""
    v = "" if v is None else str(v)
    return "'" + v if v.startswith(FORMULA_START) else v


def _proc_lock(f):
    """进程间锁（同一文件夹可能被几个 ComfyUI 同时写）。拿不到锁不影响写入。"""
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return True
    except Exception:
        return False


def _proc_unlock(f):
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def _write_rows(path, body):
    with open(path, "a", encoding="utf-8", newline="") as f:
        locked = _proc_lock(f)
        try:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                head = io.StringIO()
                csv.writer(head).writerow(COLUMNS)
                f.write("\ufeff" + head.getvalue())
            f.write(body)
            f.flush()
        finally:
            if locked:
                _proc_unlock(f)


def _merge_pending(record_dir):
    """之前因为 CSV 被占用（例如 Excel 开着）写进「待合并」的行，主文件能写时并回去。"""
    pend = os.path.join(record_dir, PENDING_NAME)
    if not os.path.exists(pend):
        return
    try:
        with open(pend, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))[1:]
        if rows:
            buf = io.StringIO()
            csv.writer(buf).writerows(rows)
            _write_rows(os.path.join(record_dir, FILE_NAME), buf.getvalue())
        os.remove(pend)
        print(f"[还原记录] 已把 {len(rows)} 行待合并记录并回 {FILE_NAME}")
    except OSError:
        pass  # 主文件还被占用，下次再并


def append(record_dir, rows, retries=3, wait=0.5):
    """往 record_dir/还原记录.csv 追加几行（进程内 + 进程间加锁，新文件先写 BOM 和表头）。
    文件被占用（Windows 上 Excel 打开着）时重试几次；仍失败就写进「还原记录_待合并.csv」，不丢行，
    以后主文件能写时自动并回去。返回实际写入的文件路径。"""
    if not rows:
        return None
    path = os.path.join(record_dir, FILE_NAME)
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([_safe_cell(r.get(c, "")) for c in COLUMNS])
    body = buf.getvalue()
    with _lock:
        os.makedirs(record_dir, exist_ok=True)
        err = None
        for i in range(retries):
            try:
                _write_rows(path, body)
                _merge_pending(record_dir)
                return path
            except OSError as e:
                err = e
                time.sleep(wait)
        pend = os.path.join(record_dir, PENDING_NAME)
        try:
            _write_rows(pend, body)
            print(f"[还原记录] {FILE_NAME} 被占用（{err}；可能在 Excel 里开着），这 {len(rows)} 行先写进 {PENDING_NAME}，"
                  "关掉 Excel 后下次保存会自动并回去。")
            return pend
        except OSError as e2:
            print(f"[还原记录] 写入失败，这 {len(rows)} 行没有记下（不影响保存）：{e2}")
            return None


def write_records(record_dir, files, sizes, prompt, unique_id, extra_pnginfo, text=None, source=None):
    """files：相对 record_dir 的文件名列表；sizes：对应的 (宽, 高)。出错只打印，不影响保存。"""
    try:
        if not isinstance(prompt, dict) or unique_id is None:
            info, seg, node_name = {}, "", ""
        else:
            info = collect(prompt, unique_id, extra_pnginfo)
            seg = segment_info(prompt, upstream(prompt, unique_id))
            node_name = _titles(extra_pnginfo).get(str(unique_id)) or prompt.get(str(unique_id), {}).get("class_type", "")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for f, (wd, ht) in zip(files, sizes):
            r = dict(info)
            r.update({"文件": f.replace("\\", "/"), "时间": now, "保存节点": node_name, "段/轮": seg,
                      "实际提示词": _clean_text(text), "尺寸": f"{wd}x{ht}", "源文件": source or ""})
            rows.append(r)
        append(record_dir, rows)
    except Exception as e:
        print(f"[还原记录] 写入失败（不影响保存）：{e}")
