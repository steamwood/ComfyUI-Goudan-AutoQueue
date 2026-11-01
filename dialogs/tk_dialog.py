"""用 tkinter 弹出选择窗口（Windows 没有 PowerShell、macOS/Linux 的备用方式）。

单独的进程运行（browse.py 用 sys.executable 启动），避免在 ComfyUI 的线程里建 Tk。
参数用环境变量传（GOUDAN_BROWSE_KIND / INITIAL / TITLE），结果打印成一行纯 ASCII：
GOUDAN_OK:<UTF-8 路径的 base64> 或 GOUDAN_CANCEL；没有 tkinter 时打印 GOUDAN_UNAVAILABLE。
"""
import base64
import os
import sys

IMAGE_TYPES = [("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"), ("所有文件", "*.*")]


def emit(path):
    if not path:
        print("GOUDAN_CANCEL")
        return
    print("GOUDAN_OK:" + base64.b64encode(path.encode("utf-8")).decode("ascii"))


def main():
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        print("GOUDAN_UNAVAILABLE")
        return 0
    kind = os.environ.get("GOUDAN_BROWSE_KIND", "dir")
    initial = os.environ.get("GOUDAN_BROWSE_INITIAL", "") or None
    title = os.environ.get("GOUDAN_BROWSE_TITLE", "")
    try:
        root = tkinter.Tk()
    except tkinter.TclError as e:
        print("GOUDAN_UNAVAILABLE")
        print(f"tkinter 无法打开窗口：{e}", file=sys.stderr)
        return 0
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    if kind == "file":
        path = filedialog.askopenfilename(parent=root, title=title, initialdir=initial, filetypes=IMAGE_TYPES)
    else:
        path = filedialog.askdirectory(parent=root, title=title, initialdir=initial, mustexist=True)
    root.destroy()
    emit(os.path.normpath(path) if path else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
