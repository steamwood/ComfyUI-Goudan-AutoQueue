"""文件名/文件夹（自动）：按提示词序号生成「日期/编号_后缀」形式的保存路径前缀。"""
import datetime
import re

DATE_TOKEN = re.compile(r"dd?|MM?|hh?|mm?|ss?|yyy?y?")
REPLACE_PATTERN = re.compile(r"%([^%]+)%")
NODE_CLASS = "PromptCycleFilenames"
DATE_INPUT = "日期"


def format_date(fmt, now):
    """和 ComfyUI 前端 formatDate 相同的规则：yyyy/yy 年，M/MM 月，d/dd 日，h/hh 时（24 小时制），m/mm 分，s/ss 秒。"""
    parts = {"d": now.day, "M": now.month, "h": now.hour, "m": now.minute, "s": now.second}

    def repl(match):
        token = match.group(0)
        if token == "yy":
            return str(now.year)[2:]
        if token == "yyyy":
            return str(now.year)
        if token[0] in parts:
            return str(parts[token[0]]).rjust(len(token), "0")
        return token

    return DATE_TOKEN.sub(repl, fmt)


def replace_dates(text, now=None):
    """把文字里的 %date:格式% 换成日期（和前端 applyTextReplacements 对日期的处理一样），其他 %...% 原样保留。"""
    if now is None:
        now = datetime.datetime.now()

    def repl(match):
        body = match.group(1)
        if len(body.split(".")) != 2 and body.startswith("date:"):
            return format_date(body[5:], now)
        return match.group(0)

    return REPLACE_PATTERN.sub(repl, text)


def resolve_prompt_dates(prompt, now=None):
    """点运行（POST /prompt）时调用：把本节点「日期」里的 %date:...% 换成这一刻的日期，写回任务本身。
    自动排出来的副本是从这份任务复制的，所以整批共用同一个日期，跑过零点也不会变。返回替换了几个节点。"""
    if now is None:
        now = datetime.datetime.now()
    changed = 0
    for node in prompt.values():
        if not isinstance(node, dict) or node.get("class_type") != NODE_CLASS:
            continue
        inputs = node.get("inputs", {})
        value = inputs.get(DATE_INPUT)
        if isinstance(value, str) and "%" in value:
            resolved = replace_dates(value, now)
            if resolved != value:
                inputs[DATE_INPUT] = resolved
                changed += 1
    return changed


def join_path(*parts):
    return "/".join(p for p in parts if p)


def join_name(number, suffix):
    if suffix:
        return f"{number}_{suffix}"
    return number


class PromptCycleFilenames:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "序号": ("INT", {"forceInput": True, "tooltip": "连「提示词循环」的 序号"}),
                "条数": ("INT", {"forceInput": True, "tooltip": "连「提示词循环」的 条数"}),
                DATE_INPUT: ("STRING", {"default": "%date:yyyy-MM-dd%", "tooltip": "日期文件夹。%date:yyyy-MM-dd% 在点运行那一刻确定，整批自动排队的副本都用同一个日期（跑过零点也不变）。也可以直接写固定文字。"}),
                "二采后缀": ("STRING", {"default": "Krea2-2ST", "tooltip": "①二采出图的文件名：日期/编号_二采后缀"}),
                "高清后缀": ("STRING", {"default": "Krea2-USD", "tooltip": "②高清重绘的文件名：日期/编号_高清后缀"}),
                "放大后缀": ("STRING", {"default": "seedvr2_after", "tooltip": "③SeedVR2 放大的文件名：日期/放大子文件夹/编号_放大后缀"}),
                "放大子文件夹": ("STRING", {"default": "seedvr", "tooltip": "SeedVR2 放大图放在日期文件夹下的这个子文件夹里"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("①二采出图→保存图像", "②高清重绘→保存图像", "③SeedVR2放大→保存图像")
    OUTPUT_TOOLTIPS = ("连①二采出图的「保存图像」文件名前缀", "连②高清重绘的「保存图像」文件名前缀", "连③SeedVR2 放大的「保存图像」文件名前缀")
    FUNCTION = "build"
    CATEGORY = "狗蛋工具"
    DESCRIPTION = "按提示词序号生成保存路径：日期/01_后缀。编号默认两位，超过 99 条自动加位。"

    def build(self, 序号, 条数, 日期, 二采后缀, 高清后缀, 放大后缀, 放大子文件夹):
        count = max(1, int(条数))
        width = max(2, len(str(count)))
        number = str(int(序号) % count + 1).zfill(width)
        date = replace_dates(日期)
        first = join_path(date, join_name(number, 二采后缀))
        second = join_path(date, join_name(number, 高清后缀))
        upscale = join_path(date, 放大子文件夹, join_name(number, 放大后缀))
        return (first, second, upscale)
