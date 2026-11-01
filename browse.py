"""「选择文件夹…」「选择图片…」按钮的后端：在运行 ComfyUI 的电脑上弹出系统自带的选择窗口，返回选中的路径。

路由：POST /goudan/browse，JSON {"kind": "dir" | "file", "current": "当前路径框里的内容"}（kind 也可以放在 ?kind= 里）。
返回：{"status": "ok", "path": ...} / {"status": "cancelled"} / {"status": "busy"}（HTTP 409，已经有一个窗口开着）
      / {"status": "error", "error": ...}（打不开选择窗口，前端提示改用粘贴路径；非 Windows 时还带 "unsupported": true）。

Windows：在 ComfyUI 进程里直接用 ctypes 调系统的选择窗口（见 win_picker.py），不启动任何外部程序，
点按钮立刻弹出。文件夹用新式 IFileOpenDialog（FOS_PICKFOLDERS），图片用带图片筛选的打开窗口；
新式窗口用不了时退回 SHBrowseForFolderW / GetOpenFileNameW。
其他系统：不支持弹窗，返回 unsupported，前端提示把路径粘贴到「路径」框里。
同一时间只允许一个窗口。
"""
import asyncio
import ipaddress
import os
import sys

from . import win_picker

ROUTE = "/goudan/browse"
KINDS = ("dir", "file")
TITLES = {"dir": "选择图片文件夹", "file": "选择一张图片"}
PASTE_HINT = "可以直接把路径粘贴到「路径」框里（带不带引号都行）。"
UNSUPPORTED_DETAIL = "选择窗口目前只支持 Windows"
TAG = "[狗蛋选择窗口]"


class DialogUnavailable(Exception):
    """这台电脑上没有可用的选择窗口（不是 Windows 等）。"""


class DialogFailed(Exception):
    """选择窗口出错。"""


def clean_path(p):
    """去掉首尾空白和引号（Windows「复制文件地址」会带双引号），展开 ~，规范化。"""
    p = (p or "").strip().strip('"').strip("'").strip()
    return os.path.normpath(os.path.expanduser(p)) if p else ""


def start_folder(current):
    """选择窗口从哪个文件夹开始：路径框里是文件夹就用它，是文件就用它所在的文件夹，
    不存在就往上找第一个存在的上级文件夹；都没有就返回空（系统默认位置）。"""
    p = clean_path(current)
    if not p:
        return ""
    if os.path.isfile(p):
        p = os.path.dirname(p)
    for _ in range(64):
        if os.path.isdir(p):
            return p
        parent = os.path.dirname(p)
        if not parent or parent == p:
            return ""
        p = parent
    return ""


def supported():
    return sys.platform == "win32" and win_picker.AVAILABLE


def open_dialog(kind, current=""):
    """弹出选择窗口（阻塞）。返回选中的路径；取消返回 None；不支持抛 DialogUnavailable，出错抛 DialogFailed。"""
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 dir 或 file：{kind}")
    if not supported():
        raise DialogUnavailable(UNSUPPORTED_DETAIL)
    try:
        return win_picker.pick(kind, start_folder(current), TITLES[kind])
    except win_picker.PickerError as e:
        raise DialogFailed(str(e))


def _parse_ip(text):
    """把地址文字变成 ipaddress 对象（去掉 IPv6 的 %网卡 后缀，::ffff:1.2.3.4 换成 IPv4）；不是 IP 就返回 None。"""
    if not text or not isinstance(text, str):
        return None
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    return addr


def is_local_address(remote, local=None):
    """请求是不是从运行 ComfyUI 的这台电脑发来的。
    - 对方地址是 127.0.0.1、::1 这类回环地址 → 是本机；
    - 对方地址和这次连接在服务器这边的地址（local）相同，或者是本机某块网卡自己的地址 → 也是本机（本机浏览器用局域网 IP 打开 ComfyUI 的情况）。
    只比较地址，不做任何网络操作。选择窗口弹在服务器这台电脑上，别的电脑用浏览器连过来时看不到窗口，所以只给本机用。"""
    addr = _parse_ip(remote)
    if addr is None:
        return False
    if addr.is_loopback:
        return True
    own = _parse_ip(local)
    if own is not None and not own.is_unspecified and own == addr:
        return True
    return addr in _own_addresses()


def _own_addresses():
    """这台电脑各个网卡自己的 IP（用 ComfyUI 本来就带的 psutil 读本机网卡配置，不发任何网络请求）。
    本机浏览器用另一块网卡的地址打开 ComfyUI 时，对方地址会是本机某块网卡的地址。"""
    addrs = set()
    try:
        import psutil
        for entries in psutil.net_if_addrs().values():
            for e in entries:
                a = _parse_ip(e.address)
                if a is not None and not a.is_unspecified:
                    addrs.add(a)
    except Exception:
        pass
    return addrs


def _local_address(request):
    """这次连接在服务器这边的地址（aiohttp 已经有的连接信息，不新建连接）。"""
    try:
        sockname = request.transport.get_extra_info("sockname")
    except Exception:
        return None
    if isinstance(sockname, (tuple, list)) and sockname:
        return sockname[0]
    return None


class BrowseService:
    """同一时间只允许一个选择窗口；弹窗放在线程里跑，ComfyUI 照常响应别的请求。"""

    def __init__(self, dialog=None):
        self.dialog = dialog or open_dialog
        self.lock = asyncio.Lock()

    async def handle(self, request):
        from aiohttp import web
        if not is_local_address(request.remote, _local_address(request)):
            return web.json_response({"status": "error", "error": "选择窗口只能在运行 ComfyUI 的这台电脑上用。" + PASTE_HINT}, status=403)
        data = {}
        if request.can_read_body:
            try:
                data = await request.json()
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        kind = request.query.get("kind") or data.get("kind") or "dir"
        current = data.get("current") or request.query.get("current") or ""
        if kind not in KINDS or not isinstance(current, str):
            return web.json_response({"status": "error", "error": "参数不对：kind 只能是 dir 或 file"}, status=400)
        if self.lock.locked():
            return web.json_response({"status": "busy", "error": "已经有一个选择窗口开着，请先在那个窗口里选好或取消（它可能在别的窗口后面）。"}, status=409)
        async with self.lock:
            loop = asyncio.get_running_loop()
            try:
                path = await loop.run_in_executor(None, self.dialog, kind, current)
            except DialogUnavailable as e:
                return web.json_response({"status": "error", "unsupported": True,
                                          "error": "这台电脑上打不开选择窗口。" + PASTE_HINT, "detail": str(e)})
            except Exception as e:
                print(f"{TAG} 选择窗口出错：{e}")
                return web.json_response({"status": "error", "error": "选择窗口出错。" + PASTE_HINT, "detail": str(e)})
        if not path:
            return web.json_response({"status": "cancelled"})
        path = os.path.normpath(path)
        print(f"{TAG} 选中：{path}")
        return web.json_response({"status": "ok", "path": path, "kind": kind})


SERVICE = BrowseService()


def register(routes):
    routes.post(ROUTE)(SERVICE.handle)


try:
    from server import PromptServer
    register(PromptServer.instance.routes)
except Exception:
    pass
