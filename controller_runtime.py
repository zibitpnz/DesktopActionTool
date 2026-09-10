"""Local controller cancellation and guarded delivery for a CLI subprocess.

No MCP or third-party imports: the ordinary CLI remains independent of its host.
"""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from action_runtime import ActionAborted, ActionError

ENVIRONMENT_KEY = "DESKTOPACTION_CONTROLLER"
EVENT_PREFIX = "Local\\DesktopActionTool-Controller-"
RECOVERY_NAME = ".mcp_recovery.json"
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def state_digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


class Signals:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateEventW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "OpenEventW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetEvent": ([wintypes.HANDLE], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetProcessTimes": ([wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    def process(self, pid):
        handle = self.api.OpenProcess(0x100000 | 0x1000, False, pid)
        if not handle:
            raise ActionError("CONTROLLER_LOST", "controller process is unavailable")
        return handle

    def created(self, handle):
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime

    def signalled(self, handle):
        status = self.api.WaitForSingleObject(handle, 0)
        if status not in (0, 258):
            raise ActionError("CONTROLLER_LOST", "cannot inspect controller signal")
        return status == 0


class ControllerEvent:
    """Owned by the server; only this request's child receives the descriptor."""
    def __init__(self, **expectations):
        self.signals = Signals()
        self.name = EVENT_PREFIX + uuid.uuid4().hex
        self.handle = self.signals.api.CreateEventW(None, True, False, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            owner = self.signals.process(os.getpid())
            try:
                created = self.signals.created(owner)
            finally:
                self.signals.api.CloseHandle(owner)
            self.payload = {"event": self.name, "owner_pid": os.getpid(), "owner_created": created,
                            "request_id": uuid.uuid4().hex, **expectations}
        except BaseException:
            self.close()
            raise

    def cancel(self):
        if self.handle and not self.signals.api.SetEvent(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.signals.api.CloseHandle(self.handle)
            self.handle = None


class ChildController:
    def __init__(self, payload):
        self.payload = payload
        self.signals = Signals()
        self.owner = self.event = None
        self.recovery_path = None
        try:
            name = payload.get("event", "")
            if not isinstance(name, str) or not re.fullmatch(re.escape(EVENT_PREFIX) + "[0-9a-f]{32}", name):
                raise ValueError("invalid event")
            if type(payload.get("owner_pid")) is not int or type(payload.get("owner_created")) is not int:
                raise ValueError("invalid owner")
            if not re.fullmatch("[0-9a-f]{32}", payload.get("request_id", "")):
                raise ValueError("invalid request")
            self.owner = self.signals.process(payload["owner_pid"])
            if self.signals.created(self.owner) != payload["owner_created"]:
                raise ValueError("controller process changed")
            self.event = self.signals.api.OpenEventW(0x100000, False, name)
            if not self.event:
                raise ValueError("event is unavailable")
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.signals.signalled(self.owner):
            raise ActionAborted("controller-exited")
        if self.signals.signalled(self.event):
            raise ActionAborted("controller-cancelled")

    def verify_session(self, session_id):
        if session_id != self.payload.get("session_id"):
            raise ActionError("SESSION_CHANGED", "activity session differs from the explicit MCP session",
                              "inspect session_status and explicitly select the session")

    def verify_state(self, store):
        expected = self.payload.get("state_digest")
        if expected is not None and state_digest(store.read()) != expected:
            raise ActionError("VERIFICATION_CHANGED", "verification was replaced by another command",
                              "create a new target preview, move, inspect the cursor, then click")

    def begin_input(self, directory):
        path = Path(directory) / RECOVERY_NAME
        path.write_text(json.dumps({"request_id": self.payload["request_id"],
                                   "status": "running-or-interrupted", "process_id": os.getpid(),
                                   "required_next_step": "inspect held input before manually removing this recovery file"}),
                        encoding="utf-8")
        self.recovery_path = path

    def finish_input(self, error):
        if self.recovery_path is not None and not getattr(error, "release_errors", None):
            self.recovery_path.unlink(missing_ok=True)
            self.recovery_path = None

    def prepare_result(self, result, store, directory, *, action_completed=False):
        """Read PNGs while the caller still holds the common action mutex."""
        screenshot = result.get("screenshot") if isinstance(result.get("screenshot"), dict) else result
        images = []
        try:
            if screenshot.get("mode") == "screenshot-window" and screenshot.get("ok"):
                paths = [screenshot.get("path")]
                for key in ("target_detail_screenshot", "cursor_detail_screenshot", "uia_highlight_control_screenshot"):
                    detail = screenshot.get(key, {})
                    if detail.get("path"):
                        paths.append(detail["path"])
                root = (Path(directory) / "screenshots").resolve()
                limit = self.payload.get("max_image_bytes", MAX_IMAGE_BYTES)
                if type(limit) is not int or not 1024 <= limit <= MAX_IMAGE_BYTES:
                    raise ValueError("invalid image limit")
                total = 0
                for name in dict.fromkeys(paths):
                    self.check()
                    path = Path(name).resolve(strict=True)
                    if path.parent != root or path.suffix.lower() != ".png":
                        raise ValueError("screenshot is outside the permitted directory")
                    with path.open("rb") as stream:
                        data = stream.read(limit - total + 1)
                    total += len(data)
                    if total > limit:
                        raise ValueError("screenshots exceed the MCP image size limit")
                    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
                        raise ValueError("invalid PNG screenshot")
                    images.append({"data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"})
                current = store.read()
                if current.get("stage") in ("target_previewed", "moved_unverified", "cursor_verified"):
                    result["_controller_verification"] = {"digest": state_digest(current),
                        "stage": current["stage"], "action_id": current.get("action_id"),
                        "ttl_seconds": current.get("ttl_seconds", 120)}
            result["_controller_images"] = images
            if result.get("action_state", {}).get("stage") == "moved_unverified":
                current = store.read()
                result["_controller_verification"] = {"digest": state_digest(current),
                    "stage": current["stage"], "action_id": current.get("action_id"),
                    "ttl_seconds": current.get("ttl_seconds", 120)}
            return result
        except (Exception, KeyboardInterrupt) as exc:
            store.invalidate("MCP screenshot delivery failed")
            if isinstance(exc, (ActionAborted, KeyboardInterrupt)):
                if action_completed:
                    exc.action_result = result
                raise
            return {"ok": False, "error_code": "IMAGE_DELIVERY_FAILED", "error": str(exc),
                    "action_completed": action_completed, "action_result": result,
                    "required_next_step": "inspect the window; do not repeat a completed action; create a fresh preview"}

    def close(self):
        for name in ("event", "owner"):
            handle = getattr(self, name, None)
            if handle:
                self.signals.api.CloseHandle(handle)
                setattr(self, name, None)


def from_environment():
    raw = os.environ.pop(ENVIRONMENT_KEY, None)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid descriptor")
        return ChildController(payload)
    except ActionAborted:
        raise
    except Exception as exc:
        raise ActionError("CONTROLLER_INVALID", "invalid or expired controller descriptor") from exc


def check_recovery(directory):
    from worker_client import UIA_RECOVERY_NAME
    if (Path(directory) / UIA_RECOVERY_NAME).exists():
        raise ActionError("UIA_RECOVERY_REQUIRED", "a direct UIA action has an unresolved outcome",
                          "read the application state; do not replay; after resolving the outcome manually remove " + UIA_RECOVERY_NAME)
    if (Path(directory) / RECOVERY_NAME).exists():
        raise ActionError("INPUT_RECOVERY_REQUIRED", "a controlled action did not confirm input cleanup",
                          "inspect held keys/buttons, then manually remove " + RECOVERY_NAME)
