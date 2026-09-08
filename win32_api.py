"""ctypes structures and Windows DLL signatures."""
import ctypes

WORD = ctypes.c_ushort


UINT = ctypes.c_uint


DWORD = ctypes.c_ulong


LONG = ctypes.c_long


ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    )


class INPUT_UNION(ctypes.Union):
    _fields_ = (
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    )


class INPUT(ctypes.Structure):
    _fields_ = (
        ("type", DWORD),
        ("union", INPUT_UNION),
    )


class POINT(ctypes.Structure):
    _fields_ = (
        ("x", LONG),
        ("y", LONG),
    )


class RECT(ctypes.Structure):
    _fields_ = (
        ("left", LONG),
        ("top", LONG),
        ("right", LONG),
        ("bottom", LONG),
    )


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", DWORD),
        ("biWidth", LONG),
        ("biHeight", LONG),
        ("biPlanes", WORD),
        ("biBitCount", WORD),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", LONG),
        ("biYPelsPerMeter", LONG),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    )


class BITMAPINFO(ctypes.Structure):
    _fields_ = (
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", DWORD * 1),
    )


user32 = ctypes.WinDLL("user32", use_last_error=True)


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


EnumChildWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)


user32.SendInput.restype = ctypes.c_uint


user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)


user32.GetAsyncKeyState.restype = ctypes.c_short


user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)


user32.SetCursorPos.restype = ctypes.c_bool


user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)


user32.GetCursorPos.restype = ctypes.c_bool


user32.EnumWindows.argtypes = (EnumWindowsProc, ctypes.c_void_p)


user32.EnumWindows.restype = ctypes.c_bool


user32.EnumChildWindows.argtypes = (ctypes.c_void_p, EnumChildWindowsProc, ctypes.c_void_p)


user32.EnumChildWindows.restype = ctypes.c_bool


user32.IsWindow.argtypes = (ctypes.c_void_p,)


user32.IsWindow.restype = ctypes.c_bool


user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)


user32.IsWindowVisible.restype = ctypes.c_bool


user32.IsWindowEnabled.argtypes = (ctypes.c_void_p,)


user32.IsWindowEnabled.restype = ctypes.c_bool


user32.IsIconic.argtypes = (ctypes.c_void_p,)


user32.IsIconic.restype = ctypes.c_bool


user32.GetDlgCtrlID.argtypes = (ctypes.c_void_p,)


user32.GetDlgCtrlID.restype = ctypes.c_int


user32.GetWindowTextLengthW.argtypes = (ctypes.c_void_p,)


user32.GetWindowTextLengthW.restype = ctypes.c_int


user32.GetWindowTextW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)


user32.GetWindowTextW.restype = ctypes.c_int


user32.GetClassNameW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)


user32.GetClassNameW.restype = ctypes.c_int


user32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(RECT))


user32.GetWindowRect.restype = ctypes.c_bool


user32.GetClientRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(RECT))


user32.GetClientRect.restype = ctypes.c_bool


user32.ClientToScreen.argtypes = (ctypes.c_void_p, ctypes.POINTER(POINT))


user32.ClientToScreen.restype = ctypes.c_bool


user32.ScreenToClient.argtypes = (ctypes.c_void_p, ctypes.POINTER(POINT))


user32.ScreenToClient.restype = ctypes.c_bool


user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(DWORD))


user32.GetWindowThreadProcessId.restype = DWORD


user32.GetForegroundWindow.restype = ctypes.c_void_p


user32.WindowFromPoint.argtypes = (POINT,)


user32.WindowFromPoint.restype = ctypes.c_void_p


user32.GetAncestor.argtypes = (ctypes.c_void_p, UINT)


user32.GetAncestor.restype = ctypes.c_void_p


user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)


user32.ShowWindow.restype = ctypes.c_bool


user32.BringWindowToTop.argtypes = (ctypes.c_void_p,)


user32.BringWindowToTop.restype = ctypes.c_bool


user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)


user32.SetForegroundWindow.restype = ctypes.c_bool


user32.SetWindowPos.argtypes = (
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
)


user32.SetWindowPos.restype = ctypes.c_bool


user32.AttachThreadInput.argtypes = (DWORD, DWORD, ctypes.c_bool)


user32.AttachThreadInput.restype = ctypes.c_bool


user32.GetDC.argtypes = (ctypes.c_void_p,)


user32.GetDC.restype = ctypes.c_void_p


user32.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)


user32.ReleaseDC.restype = ctypes.c_int


kernel32.GetCurrentThreadId.restype = DWORD


kernel32.OpenProcess.argtypes = (DWORD, ctypes.c_bool, DWORD)


kernel32.OpenProcess.restype = ctypes.c_void_p


kernel32.QueryFullProcessImageNameW.argtypes = (
    ctypes.c_void_p,
    DWORD,
    ctypes.c_wchar_p,
    ctypes.POINTER(DWORD),
)


kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool


kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)


kernel32.CloseHandle.restype = ctypes.c_bool


kernel32.GetProcessTimes.argtypes = (ctypes.c_void_p,) + (ctypes.POINTER(ctypes.c_ulonglong),) * 4


kernel32.GetProcessTimes.restype = ctypes.c_bool


gdi32.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)


gdi32.CreateCompatibleDC.restype = ctypes.c_void_p


gdi32.DeleteDC.argtypes = (ctypes.c_void_p,)


gdi32.DeleteDC.restype = ctypes.c_bool


gdi32.CreateCompatibleBitmap.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)


gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p


gdi32.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)


gdi32.SelectObject.restype = ctypes.c_void_p


gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)


gdi32.DeleteObject.restype = ctypes.c_bool


gdi32.BitBlt.argtypes = (
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    DWORD,
)


gdi32.BitBlt.restype = ctypes.c_bool


gdi32.GetDIBits.argtypes = (
    ctypes.c_void_p,
    ctypes.c_void_p,
    UINT,
    UINT,
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO),
    UINT,
)


gdi32.GetDIBits.restype = ctypes.c_int


def hwnd(value: int) -> ctypes.c_void_p:
    return ctypes.c_void_p(value)
