"""Windows 系统自带的选择窗口，直接在 ComfyUI 进程里用 ctypes 调用（不启动任何外部程序，点按钮几乎立刻弹出）。

- 首选新式 IFileOpenDialog（COM，Vista 起都有）：文件夹用 FOS_PICKFOLDERS，图片用带图片类型筛选的打开窗口。
- 新式窗口用不了时退回老式：文件夹 SHBrowseForFolderW，图片 GetOpenFileNameW。
- 每次弹窗都在一个专用线程里跑（先 CoInitializeEx 成单线程套间 STA），窗口挂在一个置顶的隐形小窗口下，
  并把它拉到最前面，保证出现在浏览器前面。
只在 Windows 上 import 时才会碰到 windll；其他系统 import 本文件也不会出错（AVAILABLE = False）。
"""
import ctypes
import os
import sys
import threading

AVAILABLE = sys.platform == "win32"

FOS_NOCHANGEDIR = 0x00000008
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_PATHMUSTEXIST = 0x00000800
FOS_FILEMUSTEXIST = 0x00001000
SIGDN_FILESYSPATH = 0x80058000 - 0x100000000  # 作为有符号 int 传
HRESULT_CANCELLED = 0x800704C7 - 0x100000000
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4
RPC_E_CHANGED_MODE = 0x80010106 - 0x100000000
CLSCTX_INPROC_SERVER = 0x1

# IFileDialog 的虚函数表序号（IUnknown 0-2，IModalWindow::Show 3，之后是 IFileDialog）
VT_RELEASE = 2
VT_SHOW = 3
VT_SETFILETYPES = 4
VT_SETFILETYPEINDEX = 5
VT_SETOPTIONS = 9
VT_GETOPTIONS = 10
VT_SETFOLDER = 12
VT_SETTITLE = 17
VT_GETRESULT = 20
VT_ITEM_GETDISPLAYNAME = 5  # IShellItem::GetDisplayName

IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff")
IMAGE_SPEC = chr(59).join("*." + e for e in IMAGE_EXTS)  # Windows filter list
FILTERS = (("图片 (" + IMAGE_SPEC + ")", IMAGE_SPEC), ("所有文件 (*.*)", "*.*"))
DIALOG_TIMEOUT = 30 * 60


class PickerError(Exception):
    """系统选择窗口出错。"""


def _guid(text):
    """把 "{xxxxxxxx-xxxx-...}" 形式的 GUID 变成 16 字节结构（不调用任何系统函数，纯 Python）。"""
    h = text.strip("{}").replace("-", "")
    b = bytes.fromhex(h)
    data = (b[3::-1] + b[5:3:-1] + b[7:5:-1] + b[8:])
    return (ctypes.c_ubyte * 16).from_buffer_copy(data)


CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_IFILE_OPEN_DIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
IID_ISHELL_ITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"


def build_filter_string(filters=FILTERS):
    """GetOpenFileNameW 用的筛选串："名字\\0模式\\0...\\0\\0"。"""
    return "".join(name + "\0" + spec + "\0" for name, spec in filters) + "\0"


def _check(hr, what):
    if hr == HRESULT_CANCELLED:
        raise _Cancelled()
    if hr < 0:
        raise PickerError(f"{what} 失败（HRESULT 0x{hr & 0xFFFFFFFF:08X}）")


class _Cancelled(Exception):
    pass


def _method(ptr, index, restype, *argtypes):
    """取 COM 对象虚函数表里第 index 个函数，返回已经绑好 this 的可调用对象。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = proto(vtbl[index])
    return lambda *a: fn(ptr, *a)


def _release(ptr):
    if ptr:
        _method(ptr, VT_RELEASE, ctypes.c_ulong)()


class _Win:
    """延迟加载的 Win32 函数（只在 Windows 上用到）。"""

    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        self.ole32 = ctypes.WinDLL("ole32")
        self.shell32 = ctypes.WinDLL("shell32")
        self.user32 = ctypes.WinDLL("user32")
        self.comdlg32 = ctypes.WinDLL("comdlg32")
        self.kernel32 = ctypes.WinDLL("kernel32")
        o, s, u, k = self.ole32, self.shell32, self.user32, self.kernel32
        o.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
        o.CoInitializeEx.restype = ctypes.c_long
        o.CoUninitialize.argtypes = []
        o.CoUninitialize.restype = None
        o.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        o.CoCreateInstance.restype = ctypes.c_long
        o.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        o.CoTaskMemFree.restype = None
        s.SHCreateItemFromParsingName.argtypes = [w.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        s.SHCreateItemFromParsingName.restype = ctypes.c_long
        s.SHBrowseForFolderW.argtypes = [ctypes.c_void_p]
        s.SHBrowseForFolderW.restype = ctypes.c_void_p
        s.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, w.LPWSTR]
        s.SHGetPathFromIDListW.restype = w.BOOL
        u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        u.CreateWindowExW.restype = w.HWND
        u.DestroyWindow.argtypes = [w.HWND]
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetForegroundWindow.restype = w.HWND
        u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.c_void_p]
        u.GetWindowThreadProcessId.restype = w.DWORD
        u.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
        u.SetForegroundWindow.argtypes = [w.HWND]
        u.BringWindowToTop.argtypes = [w.HWND]
        u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
        u.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.SendMessageW.restype = w.LPARAM
        u.AllowSetForegroundWindow.argtypes = [w.DWORD]
        k.GetCurrentThreadId.restype = w.DWORD
        self.comdlg32.GetOpenFileNameW.argtypes = [ctypes.c_void_p]
        self.comdlg32.GetOpenFileNameW.restype = w.BOOL
        self.comdlg32.CommDlgExtendedError.restype = w.DWORD


_WIN = None
_WIN_LOCK = threading.Lock()


def _win():
    global _WIN
    with _WIN_LOCK:
        if _WIN is None:
            _WIN = _Win()
        return _WIN


WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
SWP_NOACTIVATE = 0x0010


def _make_owner(win):
    """建一个置顶、不显示在任务栏的 1x1 隐形窗口（放在屏幕中间）当选择窗口的父窗口，并把它拉到最前面。
    选择窗口挂在置顶窗口下面，就一定会在浏览器前面。"""
    u = win.user32
    cx, cy = u.GetSystemMetrics(0) // 2, u.GetSystemMetrics(1) // 2
    hwnd = u.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, "STATIC", "Goudan", WS_POPUP,
                             cx, cy, 1, 1, None, None, None, None)
    if not hwnd:
        return None
    _bring_to_front(win, hwnd)
    return hwnd


def _bring_to_front(win, hwnd):
    """Windows 不让后台程序随便抢前台：临时把输入挂到当前前台窗口的线程上再 SetForegroundWindow（常用做法）。"""
    u, k = win.user32, win.kernel32
    try:
        u.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
        fg = u.GetForegroundWindow()
        me = k.GetCurrentThreadId()
        other = u.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(other and other != me and u.AttachThreadInput(me, other, True))
        try:
            u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(me, other, False)
    except Exception:
        pass


# ---------- 新式 IFileOpenDialog ----------

class COMDLG_FILTERSPEC(ctypes.Structure):
    _fields_ = [("pszName", ctypes.c_wchar_p), ("pszSpec", ctypes.c_wchar_p)]


def modern_dialog(win, kind, initial, title, owner):
    """返回路径；取消返回 None；新式窗口建不起来抛 PickerError（换老式）。"""
    w = win.w
    dlg = ctypes.c_void_p()
    clsid, iid = _guid(CLSID_FILE_OPEN_DIALOG), _guid(IID_IFILE_OPEN_DIALOG)
    _check(win.ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(dlg)),
           "CoCreateInstance(FileOpenDialog)")
    p = dlg.value
    try:
        opts = w.DWORD()
        _check(_method(p, VT_GETOPTIONS, ctypes.c_long, ctypes.POINTER(w.DWORD))(ctypes.byref(opts)), "GetOptions")
        flags = opts.value | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR
        flags |= FOS_PICKFOLDERS if kind == "dir" else FOS_FILEMUSTEXIST
        _check(_method(p, VT_SETOPTIONS, ctypes.c_long, w.DWORD)(flags), "SetOptions")
        _method(p, VT_SETTITLE, ctypes.c_long, w.LPCWSTR)(title)
        keep = None
        if kind == "file":
            keep = (COMDLG_FILTERSPEC * len(FILTERS))(*[COMDLG_FILTERSPEC(n, s) for n, s in FILTERS])
            _method(p, VT_SETFILETYPES, ctypes.c_long, w.UINT, ctypes.c_void_p)(len(FILTERS), keep)
            _method(p, VT_SETFILETYPEINDEX, ctypes.c_long, w.UINT)(1)
        if initial:
            item = ctypes.c_void_p()
            iid_item = _guid(IID_ISHELL_ITEM)
            if win.shell32.SHCreateItemFromParsingName(initial, None, ctypes.byref(iid_item), ctypes.byref(item)) >= 0 and item.value:
                try:
                    _method(p, VT_SETFOLDER, ctypes.c_long, ctypes.c_void_p)(item.value)
                finally:
                    _release(item.value)
        try:
            _check(_method(p, VT_SHOW, ctypes.c_long, w.HWND)(owner), "Show")
        except _Cancelled:
            return None
        res = ctypes.c_void_p()
        _check(_method(p, VT_GETRESULT, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(res)), "GetResult")
        try:
            name = ctypes.c_void_p()
            _check(_method(res.value, VT_ITEM_GETDISPLAYNAME, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                SIGDN_FILESYSPATH, ctypes.byref(name)), "GetDisplayName")
            try:
                return ctypes.wstring_at(name.value)
            finally:
                win.ole32.CoTaskMemFree(name.value)
        finally:
            _release(res.value)
    finally:
        _release(p)


# ---------- 老式窗口（新式用不了时） ----------

OFN_NOCHANGEDIR = 0x00000008
OFN_PATHMUSTEXIST = 0x00000800
OFN_FILEMUSTEXIST = 0x00001000
OFN_EXPLORER = 0x00080000
OFN_HIDEREADONLY = 0x00000004
OFN_DONTADDTORECENT = 0x02000000
BIF_RETURNONLYFSDIRS = 0x0001
BIF_NEWDIALOGSTYLE = 0x0040
BFFM_INITIALIZED = 1
BFFM_SETSELECTIONW = 0x0400 + 103
MAX_PATH_BUF = 32768


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [("lStructSize", ctypes.c_uint32), ("hwndOwner", ctypes.c_void_p), ("hInstance", ctypes.c_void_p),
                ("lpstrFilter", ctypes.c_wchar_p), ("lpstrCustomFilter", ctypes.c_void_p), ("nMaxCustFilter", ctypes.c_uint32),
                ("nFilterIndex", ctypes.c_uint32), ("lpstrFile", ctypes.c_void_p), ("nMaxFile", ctypes.c_uint32),
                ("lpstrFileTitle", ctypes.c_void_p), ("nMaxFileTitle", ctypes.c_uint32), ("lpstrInitialDir", ctypes.c_wchar_p),
                ("lpstrTitle", ctypes.c_wchar_p), ("Flags", ctypes.c_uint32), ("nFileOffset", ctypes.c_uint16),
                ("nFileExtension", ctypes.c_uint16), ("lpstrDefExt", ctypes.c_wchar_p), ("lCustData", ctypes.c_void_p),
                ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", ctypes.c_void_p), ("pvReserved", ctypes.c_void_p),
                ("dwReserved", ctypes.c_uint32), ("FlagsEx", ctypes.c_uint32)]


def legacy_file_dialog(win, initial, title, owner):
    buf = ctypes.create_unicode_buffer(MAX_PATH_BUF)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = owner
    filt = ctypes.create_unicode_buffer(build_filter_string(), len(build_filter_string()) + 1)
    ofn.lpstrFilter = ctypes.cast(filt, ctypes.c_wchar_p)
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(buf, ctypes.c_void_p)
    ofn.nMaxFile = MAX_PATH_BUF
    ofn.lpstrInitialDir = initial or None
    ofn.lpstrTitle = title
    ofn.Flags = OFN_EXPLORER | OFN_PATHMUSTEXIST | OFN_FILEMUSTEXIST | OFN_NOCHANGEDIR | OFN_HIDEREADONLY | OFN_DONTADDTORECENT
    if win.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return buf.value or None
    err = win.comdlg32.CommDlgExtendedError()
    if err:
        raise PickerError(f"GetOpenFileNameW 出错（{err}）")
    return None


BFFCALLBACK = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p) if AVAILABLE else None


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [("hwndOwner", ctypes.c_void_p), ("pidlRoot", ctypes.c_void_p), ("pszDisplayName", ctypes.c_void_p),
                ("lpszTitle", ctypes.c_wchar_p), ("ulFlags", ctypes.c_uint), ("lpfn", ctypes.c_void_p),
                ("lParam", ctypes.c_void_p), ("iImage", ctypes.c_int)]


def legacy_folder_dialog(win, initial, title, owner):
    display = ctypes.create_unicode_buffer(MAX_PATH_BUF)
    start = ctypes.create_unicode_buffer(initial or "")

    def on_event(hwnd, msg, lparam, data):
        if msg == BFFM_INITIALIZED and initial:
            win.user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1, ctypes.cast(start, ctypes.c_void_p).value)
        return 0

    cb = BFFCALLBACK(on_event)
    bi = BROWSEINFOW()
    bi.hwndOwner = owner
    bi.pszDisplayName = ctypes.cast(display, ctypes.c_void_p)
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
    bi.lpfn = ctypes.cast(cb, ctypes.c_void_p)
    pidl = win.shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return None
    try:
        out = ctypes.create_unicode_buffer(MAX_PATH_BUF)
        if not win.shell32.SHGetPathFromIDListW(pidl, out):
            raise PickerError("选中的不是磁盘上的文件夹")
        return out.value or None
    finally:
        win.ole32.CoTaskMemFree(pidl)


# ---------- 入口 ----------

def _dialog_thread_body(kind, initial, title, box):
    win = _win()
    hr = win.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    inited = hr >= 0
    owner = None
    try:
        owner = _make_owner(win)
        try:
            box["path"] = modern_dialog(win, kind, initial, title, owner)
            return
        except PickerError as e:
            box["modern_error"] = str(e)
        if kind == "dir":
            box["path"] = legacy_folder_dialog(win, initial, title, owner)
        else:
            box["path"] = legacy_file_dialog(win, initial, title, owner)
    except BaseException as e:  # 线程里的异常带回调用方
        box["error"] = e
    finally:
        if owner:
            win.user32.DestroyWindow(owner)
        if inited:
            win.ole32.CoUninitialize()


def pick(kind, initial, title, timeout=DIALOG_TIMEOUT):
    """弹出系统选择窗口（阻塞到用户选好或取消）。kind 为 "dir" 或 "file"；返回路径，取消返回 None。"""
    if not AVAILABLE:
        raise PickerError("不是 Windows")
    if initial and not os.path.isdir(initial):
        initial = ""
    box = {}
    t = threading.Thread(target=_dialog_thread_body, args=(kind, initial, title, box), name="goudan-picker", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise PickerError("选择窗口开着太久（超过 30 分钟）")
    if "error" in box:
        e = box["error"]
        raise e if isinstance(e, PickerError) else PickerError(f"{type(e).__name__}: {e}")
    return box.get("path")
