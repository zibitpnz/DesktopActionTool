"""Disposable image worker; Windows job ownership bounds native image codecs."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from .action_runtime import ActionAborted, ActionError
from .image_contract import SAVE_TOOLS
from .image_geometry import error
from .image_runtime import execute, failure
from .image_store import settings
from .project_paths import PROJECT_ROOT

MAX_RESPONSE = 16 * 1024 * 1024
CANCEL_GRACE_S = .75


def cleanup_owned_overlay(root, owner_pid):
    """A killed codec cannot run its finally. Remove only its own frame endpoint."""
    from .action_runtime import ActionLock
    from . import activity_indicator as indicator
    from .image_store import no_links
    root = Path(root).resolve()
    path = root / 'screenshots/.image_store/.region_overlay.json'
    with ActionLock(root / '.action_state.json'):
        no_links(path, root)
        state = indicator.read_state(path)
        if not state or state.get('owner_pid') != owner_pid:
            return
        client = indicator.find_client(path)
        if client:
            try:
                client.stop()
            finally:
                client.close()
        # The action mutex prevents another frame from replacing this endpoint.
        path.unlink(missing_ok=True)


def supervise(name, args, root, config, controller):
    from .controller_runtime import ControllerEvent, ENVIRONMENT_KEY
    from .worker_client import WorkerJob
    options = settings(root, config)
    timeout = args.get('operation_timeout_s', options['image_processing_timeout_s'])
    request_id = args.get('request_id')
    if name in SAVE_TOOLS and not args.get('dry_run') and request_id is None:
        request_id = args['request_id'] = uuid.uuid4().hex
    event = ControllerEvent(session_id=(controller.payload.get('session_id') if controller else None),
                            max_image_bytes=controller.payload.get('max_image_bytes') if controller else None,
                            read_only=bool(controller and controller.payload.get('read_only')))
    process = job = response = None
    incoming = queue.Queue(maxsize=1)
    deadline = time.monotonic() + timeout
    try:
        environment = {**os.environ, ENVIRONMENT_KEY: json.dumps(event.payload)}
        process = subprocess.Popen([sys.executable, '-B', '-m', 'desktop_action_tool.image_worker', '--worker'],
            cwd=PROJECT_ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job = WorkerJob(process)
        def read():
            try:
                incoming.put(process.stdout.read(MAX_RESPONSE + 1))
            except (OSError, ValueError):
                incoming.put(b'')
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        process.stdin.write(json.dumps({'name': name, 'args': args, 'root': str(root), 'config': str(Path(config).resolve()) if config else None,
            'deliver_images': controller is not None}).encode('utf-8'))
        process.stdin.close()
        while True:
            if controller:
                controller.check()
            if time.monotonic() >= deadline:
                error('IMAGE_TIMEOUT', 'image operation exceeded its deadline; inspect saved request before retrying')
            try:
                raw = incoming.get(timeout=.02)
                break
            except queue.Empty:
                continue
        if len(raw) > MAX_RESPONSE:
            error('IMAGE_LIMIT_EXCEEDED', 'worker response exceeded its bounded channel')
        response = json.loads(raw)
        if not isinstance(response, dict) or type(response.get('ok')) is not bool:
            error('IMAGE_OPERATION_FAILED', 'invalid image worker response')
        return response
    except (ActionError, ActionAborted, OSError, ValueError, KeyboardInterrupt) as exc:
        response = failure(exc, request_id)
        event.cancel()
        if process:
            try:
                # Allow frame STOP/finally and atomic image-store recovery to finish.
                # A stuck native call is still terminated by the owned job below.
                process.wait(timeout=CANCEL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
        return response
    finally:
        if job:
            job.close()
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()
            if name == 'preview_region':
                try:
                    cleanup_owned_overlay(root, process.pid)
                except (ActionError, OSError) as exc:
                    if response is not None:
                        response['overlay_cleanup'] = failure(exc)
        event.close()


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('Use type_text.py --images-help')
    from .controller_runtime import from_environment
    owned = from_environment()
    try:
        payload = json.loads(sys.stdin.buffer.read(131073))
        try:
            result = execute(payload['name'], payload['args'], payload['root'], config=payload['config'],
                controller=owned, deliver_images=payload['deliver_images'])
        except (ActionError, ActionAborted, OSError, ValueError, KeyboardInterrupt) as exc:
            result = failure(exc, payload.get('args', {}).get('request_id'))
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    finally:
        if owned:
            owned.close()
