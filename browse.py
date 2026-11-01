"""「选择文件夹…」「选择图片…」按钮的后端：在运行 ComfyUI 的电脑上弹出系统自带的选择窗口，返回选中的路径。

路由：POST /goudan/browse，JSON {"kind": "dir" | "file", "current": "当前路径框里的内容"}（kind 也可以放在 ?kind= 里）。
返回：{"status": "ok", "path": ...} / {"status": "cancelled"} / {"status": "busy"}（HTTP 409，已经有一个窗口开着）
      / {"status": "error", "error": ...}（没有可用的选择窗口等，前端提示改用粘贴路径）。

各系统的弹窗方式（按顺序尝试，前一种用不了就换下一种）：
- Windows：PowerShell（-STA）+ System.Windows.Forms。文件夹用新式 IFileOpenDialog（失败时退回 FolderBrowserDialog），
  图片用 OpenFileDialog。窗口挂在一个置顶的隐形窗口下，保证出现在浏览器前面。不依赖 tkinter。然后 tkinter。
- macOS：osascript，然后 tkinter。
- 其他（Linux）：tkinter，然后 zenity，然后 kdialog。
弹窗在后台线程里跑（不阻塞 ComfyUI），同一时间只允许一个窗口。
"""
import asyncio
import base64
import ipaddress
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PS_SCRIPT = os.path.join(HERE, "dialogs", "windows_dialog.ps1")
TK_SCRIPT = os.path.join(HERE, "dialogs", "tk_dialog.py")
ROUTE = "/goudan/browse"
KINDS = ("dir", "file")
TITLES = {"dir": "选择图片文件夹", "file": "选择一张图片"}
WIN_FILTER = "图片 (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff)|*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff|所有文件 (*.*)|*.*"
IMAGE_PATTERNS = "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"
DIALOG_TIMEOUT = 30 * 60
OK_PREFIX = "GOUDAN_OK:"
CANCEL_MARK = "GOUDAN_CANCEL"
UNAVAILABLE_MARK = "GOUDAN_UNAVAILABLE"
PASTE_HINT = "可以直接把路径粘贴到「路径」框里（带不带引号都行）。"
TAG = "[狗蛋选择窗口]"


class DialogUnavailable(Exception):
    """这种弹窗方式在这台电脑上用不了（没装、没有图形界面等），换下一种。"""


class DialogFailed(Exception):
    """弹窗方式存在但运行出错。"""


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


def parse_marker_output(stdout_text):
    """解析弹窗脚本的输出：返回路径；取消返回 None；脚本说用不了时抛 DialogUnavailable。"""
    for line in (stdout_text or "").splitlines():
        line = line.strip().lstrip("\ufeff")
        if line.startswith(OK_PREFIX):
            raw = base64.b64decode(line[len(OK_PREFIX):].strip())
            return raw.decode("utf-8")
        if line == CANCEL_MARK:
            return None
        if line == UNAVAILABLE_MARK:
            raise DialogUnavailable("没有可用的 tkinter 图形界面")
    raise DialogFailed("选择窗口没有返回结果")


def _decode(data):
    if isinstance(data, str):
        return data
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _run(cmd, env=None, creationflags=0):
    try:
        return subprocess.run(cmd, capture_output=True, env=env, timeout=DIALOG_TIMEOUT,
                              creationflags=creationflags, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise DialogUnavailable(str(e))
    except subprocess.TimeoutExpired:
        raise DialogFailed("选择窗口开着太久（超过 30 分钟），已关闭")


def _dialog_env(kind, initial):
    env = dict(os.environ)
    env["GOUDAN_BROWSE_KIND"] = kind
    env["GOUDAN_BROWSE_INITIAL"] = initial
    env["GOUDAN_BROWSE_TITLE"] = TITLES[kind]
    env["GOUDAN_BROWSE_FILTER"] = WIN_FILTER
    env.pop("GOUDAN_BROWSE_SELFTEST", None)
    return env


def powershell_exe():
    """优先用系统自带的 Windows PowerShell 5.1（一定有 System.Windows.Forms），找不到再用 PATH 里的。"""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    builtin = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(builtin):
        return builtin
    return shutil.which("powershell") or shutil.which("pwsh")


def powershell_command(exe):
    return [exe, "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-File", PS_SCRIPT]


def dialog_windows(kind, initial):
    exe = powershell_exe()
    if not exe:
        raise DialogUnavailable("找不到 PowerShell")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = _run(powershell_command(exe), env=_dialog_env(kind, initial), creationflags=flags)
    out = _decode(r.stdout)
    try:
        return parse_marker_output(out)
    except DialogFailed:
        err = _decode(r.stderr).strip()
        raise DialogFailed(f"PowerShell 选择窗口出错（退出码 {r.returncode}）：{err[-400:] or out[-400:]}")


def dialog_tk(kind, initial):
    if not os.path.isfile(TK_SCRIPT):
        raise DialogUnavailable("缺少 tk_dialog.py")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = _run([sys.executable, TK_SCRIPT], env=_dialog_env(kind, initial), creationflags=flags)
    out = _decode(r.stdout)
    try:
        return parse_marker_output(out)
    except DialogFailed:
        err = _decode(r.stderr).strip()
        raise DialogFailed(f"tkinter 选择窗口出错：{err[-400:]}")


def _need_display():
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise DialogUnavailable("没有图形界面（DISPLAY 为空）")


def dialog_zenity(kind, initial):
    exe = shutil.which("zenity")
    if not exe:
        raise DialogUnavailable("没有 zenity")
    _need_display()
    cmd = [exe, "--file-selection", "--title=" + TITLES[kind]]
    if kind == "dir":
        cmd.append("--directory")
    else:
        cmd.append("--file-filter=图片 | " + IMAGE_PATTERNS)
        cmd.append("--file-filter=所有文件 | *")
    if initial:
        cmd.append("--filename=" + os.path.join(initial, ""))
    r = _run(cmd)
    if r.returncode == 1:
        return None
    if r.returncode != 0:
        raise DialogFailed("zenity 出错：" + _decode(r.stderr).strip()[-400:])
    return _decode(r.stdout).strip() or None


def dialog_kdialog(kind, initial):
    exe = shutil.which("kdialog")
    if not exe:
        raise DialogUnavailable("没有 kdialog")
    _need_display()
    start = initial or os.path.expanduser("~")
    if kind == "dir":
        cmd = [exe, "--title", TITLES[kind], "--getexistingdirectory", start]
    else:
        cmd = [exe, "--title", TITLES[kind], "--getopenfilename", start, IMAGE_PATTERNS + "|图片"]
    r = _run(cmd)
    if r.returncode == 1:
        return None
    if r.returncode != 0:
        raise DialogFailed("kdialog 出错：" + _decode(r.stderr).strip()[-400:])
    return _decode(r.stdout).strip() or None


OSASCRIPT = """on run argv
set k to item 1 of argv
set t to item 2 of argv
set s to item 3 of argv
if k is "dir" then
if s is "" then
set r to choose folder with prompt t
else
set r to choose folder with prompt t default location (POSIX file s)
end if
else
if s is "" then
set r to choose file with prompt t of type {"public.image"}
else
set r to choose file with prompt t of type {"public.image"} default location (POSIX file s)
end if
end if
return POSIX path of r
end run"""


def dialog_osascript(kind, initial):
    exe = shutil.which("osascript")
    if not exe:
        raise DialogUnavailable("没有 osascript")
    r = _run([exe, "-e", OSASCRIPT, kind, TITLES[kind], initial or ""])
    if r.returncode != 0:
        err = _decode(r.stderr)
        if "-128" in err:
            return None
        raise DialogFailed("osascript 出错：" + err.strip()[-400:])
    path = _decode(r.stdout).strip()
    return os.path.normpath(path) if path else None


def backends():
    if sys.platform == "win32":
        return [("PowerShell", dialog_windows), ("tkinter", dialog_tk)]
    if sys.platform == "darwin":
        return [("osascript", dialog_osascript), ("tkinter", dialog_tk)]
    return [("tkinter", dialog_tk), ("zenity", dialog_zenity), ("kdialog", dialog_kdialog)]


def open_dialog(kind, current=""):
    """弹出选择窗口（阻塞，放在线程里调用）。返回选中的路径；取消返回 None；都用不了抛 DialogUnavailable / DialogFailed。"""
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 dir 或 file：{kind}")
    initial = start_folder(current)
    problems = []
    failure = None
    for name, fn in backends():
        try:
            return fn(kind, initial)
        except DialogUnavailable as e:
            problems.append(f"{name}：{e}")
        except DialogFailed as e:
            problems.append(f"{name}：{e}")
            failure = failure or e
        print(f"{TAG} {problems[-1]}")
    detail = "；".join(problems)
    if failure is not None:
        raise DialogFailed(detail)
    raise DialogUnavailable(detail)


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
                print(f"{TAG} 没有可用的选择窗口：{e}")
                return web.json_response({"status": "error", "error": "这台电脑上打不开选择窗口。" + PASTE_HINT, "detail": str(e)})
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
