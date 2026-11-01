# Goudan AutoQueue: native Windows folder / image picker.
# Run by browse.py as: powershell.exe -NoProfile -NonInteractive -STA -ExecutionPolicy Bypass -File windows_dialog.ps1
# Input (environment variables, Unicode safe):
#   GOUDAN_BROWSE_KIND     dir | file
#   GOUDAN_BROWSE_INITIAL  folder to start in (may be empty)
#   GOUDAN_BROWSE_TITLE    dialog title
#   GOUDAN_BROWSE_FILTER   OpenFileDialog filter string
# Output (stdout, one line, pure ASCII so no code page can garble it):
#   GOUDAN_OK:<base64 of the UTF-8 path>   or   GOUDAN_CANCEL
# This file must stay pure ASCII: Windows PowerShell 5.1 reads BOM-less files with the ANSI code page.

$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
} catch {
}

function Write-GoudanResult([string]$path) {
    if ([string]::IsNullOrEmpty($path)) {
        [Console]::Out.WriteLine('GOUDAN_CANCEL')
        return
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($path)
    [Console]::Out.WriteLine('GOUDAN_OK:' + [Convert]::ToBase64String($bytes))
}

$kind = $env:GOUDAN_BROWSE_KIND
$initial = $env:GOUDAN_BROWSE_INITIAL
$title = $env:GOUDAN_BROWSE_TITLE
$filter = $env:GOUDAN_BROWSE_FILTER

if ($env:GOUDAN_BROWSE_SELFTEST -eq '1') {
    # Used by the tests: echo the start folder back without showing any window.
    Write-GoudanResult $initial
    exit 0
}

if ([string]::IsNullOrEmpty($initial) -or -not [System.IO.Directory]::Exists($initial)) {
    $initial = ''
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$pickerSource = @'
using System;
using System.Runtime.InteropServices;

public static class GoudanFolderPicker
{
    private const uint FOS_NOCHANGEDIR = 0x00000008;
    private const uint FOS_PICKFOLDERS = 0x00000020;
    private const uint FOS_FORCEFILESYSTEM = 0x00000040;
    private const uint FOS_PATHMUSTEXIST = 0x00000800;
    private const uint SIGDN_FILESYSPATH = 0x80058000;
    private const int ERROR_CANCELLED_HRESULT = unchecked((int)0x800704C7);

    [ComImport]
    [Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
    private class FileOpenDialogCoClass
    {
    }

    [ComImport]
    [Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem
    {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdnName, out IntPtr ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }

    [ComImport]
    [Guid("42F85136-DB7E-439C-85F1-E4075D135FC8")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileDialog
    {
        [PreserveSig]
        int Show(IntPtr parent);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(uint fos);
        void GetOptions(out uint pfos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem ppsi);
        void GetCurrentSelection(out IShellItem ppsi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
        void AddPlace(IShellItem psi, int fdap);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr pFilter);
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath, IntPtr pbc, ref Guid riid, out IShellItem ppv);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr lpdwProcessId);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool BringWindowToTop(IntPtr hWnd);

    // Bring our (invisible, topmost) owner window to the foreground, so the dialog is not hidden behind the browser.
    public static void ForceForeground(IntPtr hwnd)
    {
        IntPtr fg = GetForegroundWindow();
        uint fgThread = GetWindowThreadProcessId(fg, IntPtr.Zero);
        uint myThread = GetCurrentThreadId();
        bool attached = false;
        if (fgThread != 0 && fgThread != myThread)
        {
            attached = AttachThreadInput(myThread, fgThread, true);
        }
        BringWindowToTop(hwnd);
        SetForegroundWindow(hwnd);
        if (attached)
        {
            AttachThreadInput(myThread, fgThread, false);
        }
    }

    // Modern (Vista+) folder picker. Returns the chosen folder, or null when cancelled.
    public static string PickFolder(IntPtr owner, string title, string initial)
    {
        IFileDialog dialog = (IFileDialog)new FileOpenDialogCoClass();
        try
        {
            uint options;
            dialog.GetOptions(out options);
            dialog.SetOptions(options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR);
            if (!string.IsNullOrEmpty(title))
            {
                dialog.SetTitle(title);
            }
            if (!string.IsNullOrEmpty(initial))
            {
                try
                {
                    Guid shellItemGuid = typeof(IShellItem).GUID;
                    IShellItem folder;
                    SHCreateItemFromParsingName(initial, IntPtr.Zero, ref shellItemGuid, out folder);
                    dialog.SetFolder(folder);
                }
                catch (Exception)
                {
                }
            }
            int hr = dialog.Show(owner);
            if (hr == ERROR_CANCELLED_HRESULT)
            {
                return null;
            }
            if (hr != 0)
            {
                Marshal.ThrowExceptionForHR(hr);
            }
            IShellItem result;
            dialog.GetResult(out result);
            IntPtr namePtr;
            result.GetDisplayName(SIGDN_FILESYSPATH, out namePtr);
            try
            {
                return Marshal.PtrToStringUni(namePtr);
            }
            finally
            {
                Marshal.FreeCoTaskMem(namePtr);
            }
        }
        finally
        {
            Marshal.ReleaseComObject(dialog);
        }
    }
}
'@

$pickerLoaded = $false
try {
    Add-Type -TypeDefinition $pickerSource -Language CSharp
    $pickerLoaded = $true
} catch {
    [Console]::Error.WriteLine('modern picker unavailable: ' + $_.Exception.Message)
}

[System.Windows.Forms.Application]::EnableVisualStyles()

# Invisible topmost owner window, centred on the screen where the mouse is (the user just clicked in the browser there).
$screen = [System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position)
$area = $screen.WorkingArea
$owner = New-Object System.Windows.Forms.Form
$owner.Text = $title
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Location = New-Object System.Drawing.Point(([int]($area.Left + $area.Width / 2)), ([int]($area.Top + $area.Height / 3)))
$owner.Opacity = 0
$owner.Show()
$owner.Activate()
if ($pickerLoaded) {
    try {
        [GoudanFolderPicker]::ForceForeground($owner.Handle)
    } catch {
    }
}

$chosen = $null
try {
    if ($kind -eq 'file') {
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = $title
        $dialog.Filter = $filter
        $dialog.FilterIndex = 1
        $dialog.Multiselect = $false
        $dialog.CheckFileExists = $true
        $dialog.RestoreDirectory = $true
        if ($initial) {
            $dialog.InitialDirectory = $initial
        }
        if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
            $chosen = $dialog.FileName
        }
        $dialog.Dispose()
    } else {
        $done = $false
        if ($pickerLoaded) {
            try {
                $chosen = [GoudanFolderPicker]::PickFolder($owner.Handle, $title, $initial)
                $done = $true
            } catch {
                [Console]::Error.WriteLine('modern picker failed, using FolderBrowserDialog: ' + $_.Exception.Message)
            }
        }
        if (-not $done) {
            $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
            $dialog.Description = $title
            $dialog.ShowNewFolderButton = $false
            if ($initial) {
                $dialog.SelectedPath = $initial
            }
            if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
                $chosen = $dialog.SelectedPath
            }
            $dialog.Dispose()
        }
    }
} finally {
    $owner.Close()
    $owner.Dispose()
}

Write-GoudanResult $chosen
exit 0
