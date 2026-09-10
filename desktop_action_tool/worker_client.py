"""Bounded UIA worker processes; never terminates a target app."""
import json
from pathlib import Path
import subprocess
import sys
import time
import ctypes
import queue
import threading

from .action_runtime import ActionError
from .project_paths import PROJECT_ROOT

UIA_RECOVERY_NAME = '.uia_recovery.json'
# Value and Text can each contain 65536 non-BMP characters (12 JSON bytes each).
UIA_MESSAGE_LIMIT = 2 * 1024 * 1024


def write_uia_marker(directory, operation_id, status, **details):
    path = Path(directory) / UIA_RECOVERY_NAME
    if path.exists():
        current = json.loads(path.read_text(encoding='utf-8'))
        if current.get('operation_id') != operation_id:
            raise ActionError('UIA_RECOVERY_REQUIRED', 'another UIA operation has an unresolved outcome')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'operation_id': operation_id, 'status': status,
        'required_next_step': 'read the application state; do not replay; after resolving the outcome manually remove ' + UIA_RECOVERY_NAME,
        **details}), encoding='utf-8')
    temporary.replace(path)


class WorkerJob:
    """Kill only our disposable worker if its CLI owner exits, including hard kill."""
    def __init__(self, process):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('user', ctypes.c_int64), ('job', ctypes.c_int64), ('flags', w.DWORD),
                        ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', w.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6), ('memory', ctypes.c_size_t * 4)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, arguments, result in (
                ('CreateJobObjectW', [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
                ('SetInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
                ('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL),
                ('CloseHandle', [w.HANDLE], w.BOOL)):
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not self.api.AssignProcessToJobObject(self.handle, int(process._handle))):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class UiaSession:
    """Bounded duplex protocol. The worker cannot mutate until dispatch is permitted."""
    def __init__(self, check, *, command=None):
        self.check, self.counter, self.job = check, 0, None
        self.incoming, self.outgoing = queue.Queue(maxsize=16), queue.Queue(maxsize=4)
        self.failed = threading.Event()
        self.process = subprocess.Popen(command or [sys.executable, '-B', '-m', 'desktop_action_tool.uia_worker', '--direct'],
            cwd=PROJECT_ROOT if command is None else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.threads = []
        try:
            self.job = WorkerJob(self.process)
            def reader():
                try:
                    while True:
                        line = self.process.stdout.readline(UIA_MESSAGE_LIMIT + 1)
                        if not line:
                            return
                        if len(line) > UIA_MESSAGE_LIMIT or not line.endswith(b'\n'):
                            raise ValueError('oversized worker message')
                        self.incoming.put_nowait(json.loads(line))
                except (OSError, ValueError, queue.Full):
                    self.failed.set()
            def writer():
                try:
                    while True:
                        payload = self.outgoing.get()
                        if payload is None:
                            return
                        self.process.stdin.write(payload)
                        self.process.stdin.flush()
                except (OSError, ValueError):
                    self.failed.set()
            def drain_errors():
                total = 0
                try:
                    while data := self.process.stderr.read(8192):
                        total += len(data)
                        if total > 256 * 1024:
                            self.failed.set()
                except (OSError, ValueError):
                    pass
            self.threads = [threading.Thread(target=f, daemon=True) for f in (reader, writer, drain_errors)]
            for thread in self.threads:
                thread.start()
        except BaseException:
            self.close()
            raise

    def send(self, message):
        data = (json.dumps(message, ensure_ascii=True, allow_nan=False) + '\n').encode('utf-8')
        if len(data) > UIA_MESSAGE_LIMIT:
            raise ValueError('UIA request exceeds the message limit')
        try:
            self.outgoing.put_nowait(data)
        except queue.Full as exc:
            raise ActionError('UIA_WORKER_FAILED', 'worker write queue overflow') from exc

    def request(self, payload, timeout, progress=None):
        self.check()
        self.counter += 1
        request_id = self.counter
        self.send({'id': request_id, **payload})
        deadline = time.monotonic() + timeout
        stage = None
        while True:
            # Process a queued returned-stage before checking cancellation.
            try:
                message = self.incoming.get_nowait()
            except queue.Empty:
                self.check()
                if self.failed.is_set() or self.process.poll() is not None:
                    raise ActionError('UIA_WORKER_FAILED', 'UIA worker exited or returned an invalid message')
                if time.monotonic() >= deadline:
                    raise ActionError('UIA_TIMEOUT', 'UIA phase exceeded its timeout', 'read the control state; never replay an unconfirmed action')
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
                continue
            if not isinstance(message, dict) or message.get('id') != request_id:
                raise ActionError('UIA_WORKER_FAILED', 'unexpected UIA protocol response')
            if message.get('stage'):
                if message['stage'] not in ('dispatching', 'returned') or progress is None:
                    raise ActionError('UIA_WORKER_FAILED', 'unexpected UIA stage')
                expected_stage = 'dispatching' if stage is None else 'returned' if stage == 'dispatching' else None
                if message['stage'] != expected_stage:
                    raise ActionError('UIA_WORKER_FAILED', 'repeated or out-of-order UIA stage')
                stage = message['stage']
                progress(message)
                if message['stage'] == 'dispatching':
                    self.check()
                    self.send({'id': request_id, 'permit': True})
                continue
            if not message.get('ok'):
                raise ActionError(message.get('error_code', 'UIA_FAILED'), message.get('error', 'UIA failed'),
                                  'read the control state; do not replay an unconfirmed action')
            self.check()
            if time.monotonic() > deadline:
                raise ActionError('UIA_TIMEOUT', 'UIA phase exceeded its timeout')
            return message['result']

    def close(self):
        if self.job:
            self.job.close()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=3)
        try:
            self.outgoing.put_nowait(None)
        except queue.Full:
            pass
        for thread in self.threads:
            thread.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def run_uia_worker(request, timeout, check_cancelled, *, command=None):
    deadline = time.monotonic() + timeout
    check_cancelled()
    directory = PROJECT_ROOT if command is None else None
    command = command or [sys.executable, "-B", "-m", "desktop_action_tool.uia_worker"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               cwd=directory,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        payload = json.dumps(request)
        while True:
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ActionError("UIA_TIMEOUT", "UI Automation exceeded its timeout",
                                  "retry with a narrower search or a longer --timeout-s")
            try:
                output, errors = process.communicate(payload, timeout=min(0.02, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
        check_cancelled()
        if process.returncode:
            raise ActionError("UIA_WORKER_FAILED", errors.strip() or "UI Automation worker failed")
        try:
            response = json.loads(output)
        except ValueError as exc:
            raise ActionError("UIA_WORKER_FAILED", "invalid response from UI Automation worker") from exc
        if not response.get("ok"):
            raise ActionError(response.get("error_code", "UIA_FAILED"), response.get("error", "UI Automation failed"),
                              response.get("required_next_step", "retry the UI Automation query"))
        return response["result"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
