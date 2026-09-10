"""Bounded, cancellable CLI subprocesses for the local MCP server."""
import base64
from dataclasses import dataclass, field
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import anyio

from .project_paths import PROJECT_ROOT

from .action_runtime import ActionError, ActionLock
from .controller_runtime import ControllerEvent, ENVIRONMENT_KEY, MAX_IMAGE_BYTES, RECOVERY_NAME
from .mcp_contract import SPECS, build_command
from .image_contract import IMAGE_TOOLS, SAVE_TOOLS
from .worker_client import UIA_RECOVERY_NAME

from .release_info import current_version

SERVER_VERSION = current_version()
READ_TOOLS = {"desktop_status", "check_updates", "list_windows", "active_window", "cursor_position", "capture_window",
              "preview_target", "list_controls", "wait_control", "session_status", "get_control_state", "wait_control_state", "wait"}
MAX_OUTPUT = 16 * 1024 * 1024
READ_TOOLS.update(IMAGE_TOOLS)


def failure(code, message, **details):
    return {"ok": False, "error_code": code, "error": message, **details}


@dataclass
class Job:
    command: object
    controller: object
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    images: list = field(default_factory=list)
    interrupted: str | None = None


class Bridge:
    def __init__(self, directory=None, *, read_only=False, max_image_bytes=MAX_IMAGE_BYTES,
                 child_command=None, grace_seconds=5):
        self.directory = Path(directory or PROJECT_ROOT).resolve()
        self.read_only = read_only
        self.max_image_bytes = max_image_bytes
        self.child_command = child_command or [sys.executable, "-B", str(self.directory / "type_text.py")]
        self.grace_seconds = grace_seconds
        self.jobs = {}
        self.active = None
        self.image_active = None
        self.last = None
        self.owned_session = None
        self.verifications = {}
        self.closing = False
        self.uncertain = False
        self.lock = threading.RLock()

    def status(self):
        from .interaction_profiles import PROFILES
        from .image_store import settings as image_settings
        try:
            image_options = image_settings(self.directory)
            image_limits = {key: image_options[key] for key in ('image_max_pixels', 'image_max_inputs',
                'image_working_max_mib', 'image_temporary_max_mib', 'image_processing_timeout_s', 'image_region_ttl_s')}
        except (ValueError, OSError):
            image_limits = {'error_code': 'IMAGE_ARGUMENT_INVALID'}
        with self.lock:
            return {"ok": True, "mode": "mcp-status", "server_version": SERVER_VERSION,
                    "mcp_version": importlib.metadata.version("mcp"), "python_version": sys.version.split()[0],
                    "read_only": self.read_only, "uia_available": importlib.util.find_spec("uiautomation") is not None,
                    "interaction_profiles": list(PROFILES),
                    "images_available": importlib.util.find_spec('PIL') is not None,
                    "image_limits": image_limits,
                    "active_image_operation": self.image_active,
                    "busy": self.active is not None or self.image_active is not None, "active_operation": self.active,
                    "last_operation": self.last, "owned_session_id": self.owned_session,
                    "input_recovery_required": self.uncertain or (self.directory / RECOVERY_NAME).exists(),
                    "uia_recovery_required": (self.directory / UIA_RECOVERY_NAME).exists(),
                    "recovery_instructions": "resolve UIA outcomes by reading application state before removing " + UIA_RECOVERY_NAME
                        + "; for input cleanup inspect held keys/buttons before removing " + RECOVERY_NAME + "; restart this server afterwards",
                    "max_image_bytes": self.max_image_bytes, "scope": "one project copy in the current Windows desktop"}

    def cancel_all(self):
        with self.lock:
            self.closing = True
            for job in self.jobs.values():
                job.interrupted = "connection-closed"
                job.controller.cancel()

    def _verification(self, command):
        if not command.verification_id or command.dry_run:
            return None
        with self.lock:
            record = self.verifications.get(command.verification_id)
        if not record or time.monotonic() > record["deadline"]:
            raise ActionError("VERIFICATION_REQUIRED", "unknown or expired MCP verification_id", "create a new preview")
        stages = {"move_mouse": {"target_previewed"}, "capture_window": {"moved_unverified", "cursor_verified"},
                  "click_mouse": {"cursor_verified"}, "double_click_mouse": {"cursor_verified"}, "drag_mouse": {"cursor_verified"}}
        if record["stage"] not in stages.get(command.name, set()):
            raise ActionError("VERIFICATION_REQUIRED", "this token does not represent the required visual step",
                              "inspect the target PNG, move, inspect the cursor PNG, then click")
        return record["digest"]

    async def call(self, name, arguments):
        if name in IMAGE_TOOLS:
            from .image_store import settings as image_settings
            options = image_settings(self.directory)
            arguments = dict(arguments)
            arguments.setdefault('operation_timeout_s', options['image_processing_timeout_s'])
            if name == 'preview_region':
                arguments.setdefault('show_overlay', bool(options['image_overlay_enabled']))
        command = build_command(name, arguments)
        if self.read_only and name == 'preview_region' and arguments.get('show_overlay'):
            return failure('READ_ONLY', 'read-only server forbids showing a screen region frame'), []
        if self.read_only and name not in READ_TOOLS:
            return failure("READ_ONLY", "this server exposes observation tools only"), []
        if name == "desktop_status":
            return self.status(), []
        try:
            digest = self._verification(command)
        except ActionError as exc:
            return failure(exc.code, str(exc), required_next_step=exc.next_step), []
        with self.lock:
            if self.closing:
                return failure("SERVER_CLOSING", "the MCP connection is closing"), []
            independent = name in {"session_status", "session_end", "session_heartbeat", "check_updates"}
            image_lane = name in IMAGE_TOOLS and name != 'capture_image' and not (name == 'preview_region' and arguments.get('show_overlay'))
            if image_lane:
                independent = True
                if self.image_active is not None:
                    return failure('BUSY', 'another image operation is running; no work was queued'), []
            if self.active is not None and not independent:
                return failure("BUSY", "another MCP operation is running; no action was queued"), []
            if command.changes_desktop and not command.dry_run and (self.directory / UIA_RECOVERY_NAME).exists():
                try:
                    with ActionLock(self.directory / '.action_state.json'):
                        if (self.directory / UIA_RECOVERY_NAME).exists():
                            return failure("UIA_RECOVERY_REQUIRED", "a direct UIA action has an unresolved outcome",
                                required_next_step="read the application state; do not replay; resolve " + UIA_RECOVERY_NAME + " and restart this server"), []
                except ActionError as exc:
                    return failure(exc.code, str(exc), required_next_step=exc.next_step), []
            if command.changes_desktop and not command.dry_run and (self.uncertain or (self.directory / RECOVERY_NAME).exists()):
                return failure("INPUT_RECOVERY_REQUIRED", "input cleanup was not confirmed",
                               required_next_step="inspect held keys/buttons, then manually remove " + RECOVERY_NAME), []
            controller = ControllerEvent(session_id=command.session_id, state_digest=digest,
                                         read_only=self.read_only,
                                         max_image_bytes=self.max_image_bytes)
            if name in SAVE_TOOLS and not command.dry_run:
                arguments.setdefault('request_id', controller.payload['request_id'])
                command.text = json.dumps(arguments, allow_nan=False)
            job = Job(command, controller)
            request_id = controller.payload["request_id"]
            self.jobs[request_id] = job
            if image_lane:
                self.image_active = {'request_id': request_id, 'tool': name}
            if not independent:
                self.active = {"request_id": request_id, "tool": name}
        try:
            await anyio.to_thread.run_sync(self._execute, job, abandon_on_cancel=True)
            return job.result, job.images
        except anyio.get_cancelled_exc_class():
            with self.lock:
                if not job.done.is_set():
                    job.interrupted = "request-cancelled"
                    job.controller.cancel()
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(job.done.wait, self.grace_seconds + 3)
                if name == "session_start" and job.result and job.result.get("session"):
                    await anyio.to_thread.run_sync(self._end_owned_session, job.result["session"]["session_id"])
            raise

    def _run_process(self, job):
        command = job.command
        job.controller.payload['operation_deadline'] = time.monotonic() + command.timeout
        argv = [*self.child_command, *command.argv]
        if command.name == "session_start":
            argv.append("--session-owner-pid=" + str(os.getpid()))
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                       ENVIRONMENT_KEY: json.dumps(job.controller.payload)}
        process = subprocess.Popen(argv, cwd=self.directory, env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()
        def read(stream, target, limit):
            try:
                while chunk := stream.read(65536):
                    available = limit - len(target)
                    target.extend(chunk[:max(0, available)])
                    if len(chunk) > available:
                        overflow.set()
            except (OSError, ValueError):
                pass
            finally:
                stream.close()
        def write():
            try:
                process.stdin.write((command.text or "").encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        readers = [threading.Thread(target=read, args=(stream, buf, limit), daemon=True)
                   for stream, buf, limit in ((process.stdout, buffers[0], MAX_OUTPUT),
                                               (process.stderr, buffers[1], 256 * 1024))]
        writer = threading.Thread(target=write, daemon=True)
        for thread in [*readers, writer]:
            thread.start()
        deadline = time.monotonic() + command.timeout
        cancelled_at = None
        try:
            while process.poll() is None:
                if overflow.is_set() or time.monotonic() >= deadline:
                    job.interrupted = "output-limit" if overflow.is_set() else "operation-timeout"
                    job.controller.cancel()
                if job.interrupted and cancelled_at is None:
                    cancelled_at = time.monotonic()
                if cancelled_at is not None and time.monotonic() - cancelled_at > self.grace_seconds:
                    process.kill()  # Only our child; cleanup is explicitly marked unknown below.
                    process.wait(timeout=2)
                    if command.name == 'check_updates':
                        return failure('UPDATE_TIMEOUT' if job.interrupted == 'operation-timeout' else 'UPDATE_CHECK_CANCELLED',
                                       'release check did not finish; no desktop action or installation was performed',
                                       update_available=None)
                    if command.name in IMAGE_TOOLS:
                        return self._image_failure(job, 'image child did not confirm cancellation')
                    return failure("ACTION_OUTCOME_UNKNOWN", "child did not confirm graceful cancellation",
                                   required_next_step="inspect the window and held input; never replay this action automatically")
                time.sleep(0.02)
            for reader in readers:
                reader.join(timeout=2)
            if any(reader.is_alive() for reader in readers) or overflow.is_set():
                if command.name in IMAGE_TOOLS:
                    return self._image_failure(job, 'image response was incomplete or too large')
                return failure("ACTION_OUTCOME_UNKNOWN", "child response was incomplete or too large")
            try:
                result = json.loads(buffers[0].decode("utf-8"))
                if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                    raise ValueError("invalid result object")
                if process.returncode not in (0, 1, 2) or (process.returncode != 0 and result["ok"]):
                    raise ValueError("inconsistent child exit status")
                return result
            except (UnicodeError, ValueError):
                if command.name in IMAGE_TOOLS:
                    return self._image_failure(job, 'image child did not return a valid result')
                return failure("ACTION_OUTCOME_UNKNOWN", "child did not return a valid CLI result")
        finally:
            if process.poll() is None:
                job.controller.cancel()
                try:
                    process.wait(timeout=self.grace_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            writer.join(timeout=0.2)

    def _image_failure(self, job, message):
        arguments = json.loads(job.command.text or '{}')
        return failure('IMAGE_TIMEOUT' if job.interrupted == 'operation-timeout' else 'IMAGE_OPERATION_INTERRUPTED',
            message, saved=None, request_id=arguments.get('request_id'),
            required_next_step='read_image(request_id) to inspect a saved result; do not repeat desktop input')

    def _execute(self, job):
        result = None
        try:
            result = self._run_process(job)
            images = result.pop("_controller_images", [])
            verification = result.pop("_controller_verification", None)
            total = 0
            for image in images:
                if image.get("mimeType") != "image/png":
                    raise ValueError("unexpected image type")
                data = base64.b64decode(image["data"], validate=True)
                total += len(data)
                if data[:8] != b"\x89PNG\r\n\x1a\n" or total > self.max_image_bytes:
                    raise ValueError("invalid image content")
            if result.get("ok") and verification is not None and not job.interrupted:
                if verification["stage"] != "moved_unverified" and not images:
                    raise ValueError("visual verification requires an image")
                token = uuid.uuid4().hex
                with self.lock:
                    self.verifications = {k: v for k, v in self.verifications.items() if v["deadline"] > time.monotonic()}
                    while len(self.verifications) >= 128:
                        self.verifications.pop(next(iter(self.verifications)))
                    self.verifications[token] = {**verification, "deadline": time.monotonic() + verification.get("ttl_seconds", 120)}
                result["verification_id"] = token
                result["verification_stage"] = verification["stage"]
            if job.command.name == "session_start" and result.get("ok") and result.get("session"):
                with self.lock:
                    self.owned_session = result["session"]["session_id"]
            if job.command.name == "session_end" and result.get("ok"):
                with self.lock:
                    if self.owned_session == job.command.session_id:
                        self.owned_session = None
            if job.interrupted:
                result["controller_interrupted"] = job.interrupted
            job.result, job.images = result, images
        except Exception:
            job.result = self._image_failure(job, 'could not confirm image result') if job.command.name in IMAGE_TOOLS else failure("ACTION_OUTCOME_UNKNOWN", "controller could not confirm the operation result",
                                 required_next_step="inspect the window before another action")
            if job.command.name in IMAGE_TOOLS and isinstance(result, dict) and result.get('saved'):
                job.result.update({key: result[key] for key in ('image_id', 'image_path', 'metadata_path', 'request_id', 'saved') if key in result})
                job.result['delivery'] = {'delivered': False, 'error_code': 'IMAGE_DELIVERY_FAILED'}
        finally:
            if job.result is None:
                job.result = self._image_failure(job, 'image operation ended without a result') if job.command.name in IMAGE_TOOLS else failure("ACTION_OUTCOME_UNKNOWN", "operation ended without a result")
            if (job.command.name == 'act_on_control' and job.result.get('error_code') == 'ACTION_OUTCOME_UNKNOWN'
                    and 'cursor_follow' not in job.result):
                enabled = True if '--uia-cursor-follow' in job.command.argv else False if '--no-uia-cursor-follow' in job.command.argv else None
                job.result['cursor_follow'] = {'enabled': enabled, 'status': 'unknown', 'steps': None,
                    'duration_ms': None, 'pause_ms': None, 'final_position': None}
            if (job.command.changes_desktop and not job.command.dry_run
                    and (job.result.get("error_code") == "ACTION_OUTCOME_UNKNOWN" or job.result.get("release_errors"))):
                self._mark_uncertain(job)
            with self.lock:
                self.last = {"tool": job.command.name, **{key: job.result[key] for key in
                             ("ok", "error_code", "completed", "typed_chars", "aborted", "action_completed", "controller_interrupted",
                              "execution_status", "effect_status", "completed_calls", "changed", "operation_id", "cursor_follow", "interaction",
                              "current_version", "latest_version", "update_available", "status", 'image_id', 'image_path',
                              'metadata_path', 'request_id', 'saved', 'delivery') if key in job.result}}
                request_id = job.controller.payload["request_id"]
                if self.active and self.active["request_id"] == request_id:
                    self.active = None
                if self.image_active and self.image_active['request_id'] == request_id:
                    self.image_active = None
                self.jobs.pop(request_id, None)
                job.controller.close()
                job.done.set()

    def _mark_uncertain(self, job):
        with self.lock:
            self.uncertain = True
        direct = job.command.name == 'act_on_control'
        path = self.directory / (UIA_RECOVERY_NAME if direct else RECOVERY_NAME)
        try:
            with ActionLock(self.directory / ".action_state.json"):
                if not path.exists():
                    path.write_text(json.dumps({"request_id": job.controller.payload["request_id"],
                        "status": "outcome-unknown", "required_next_step": "read the control/application state before manually removing this file" if direct else "inspect held input before manually removing this file"}),
                        encoding="utf-8")
        except (OSError, ActionError):
            # An active child has its own recovery marker and the common mutex.
            pass

    def _end_owned_session(self, session_id):
        with self.lock:
            if self.owned_session != session_id:
                return
        command = build_command("session_end", {"session_id": session_id, "operation_timeout_s": 5})
        controller = ControllerEvent(session_id=session_id)
        try:
            result = self._run_process(Job(command, controller))
            if result.get("ok") or result.get("error_code") == "SESSION_CHANGED":
                with self.lock:
                    if self.owned_session == session_id:
                        self.owned_session = None
        finally:
            controller.close()

    async def close(self):
        self.cancel_all()
        with anyio.CancelScope(shield=True):
            with self.lock:
                jobs = list(self.jobs.values())
            for job in jobs:
                await anyio.to_thread.run_sync(job.done.wait, self.grace_seconds + 3)
            if self.owned_session:
                await anyio.to_thread.run_sync(self._end_owned_session, self.owned_session)
