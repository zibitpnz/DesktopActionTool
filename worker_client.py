"""Bounded JSON reads in a disposable process; never terminates a target app."""
import json
from pathlib import Path
import subprocess
import sys
import time

from action_runtime import ActionError


def run_uia_worker(request, timeout, check_cancelled, *, command=None):
    deadline = time.monotonic() + timeout
    check_cancelled()
    command = command or [sys.executable, "-B", str(Path(__file__).with_name("uia_worker.py"))]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
