"""Internal UIA worker: legacy reads or staged direct calls via worker_client.py."""
import contextlib
import json
import sys
import types


def use_memory_com_cache():
    """Avoid comtypes' import-time disk cache in this disposable worker.

    The small adapter is verified with comtypes 1.4.16. Generated type-library
    modules live only in the worker and disappear when it exits.
    """
    import comtypes
    package = types.ModuleType("comtypes.gen")
    package.__path__ = list(comtypes.__path__)
    comtypes.gen = package
    sys.modules["comtypes.gen"] = package
    cache = types.ModuleType("comtypes.client._code_cache")
    cache._find_gen_dir = lambda: None
    sys.modules[cache.__name__] = cache


def main():
    try:
        request = json.load(sys.stdin)
        # Provider diagnostics must not corrupt the JSON protocol.
        with contextlib.redirect_stdout(sys.stderr):
            import window_backend as windows
            import controls_backend as controls
            windows.initialize_dpi_awareness()
            try:
                use_memory_com_cache()
            except ImportError:
                pass  # The normal dependency error is reported by the collector.
            if request.get("control_types") is not None:
                request["control_types"] = set(request["control_types"])
            result = controls.collect_uia_controls(**request)
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "error": str(exc), "error_code": getattr(exc, "code", "UIA_FAILED"),
                    "required_next_step": getattr(exc, "next_step", "check the UI Automation dependency and target window")}
    print(json.dumps(response, ensure_ascii=True))


def direct_main():
    # Set the apartment before comtypes' first import (the reader retains its old protocol).
    sys.coinit_flags = 0  # COINIT_MULTITHREADED
    output = sys.stdout
    def emit(message):
        output.write(json.dumps(message, ensure_ascii=True, allow_nan=False) + '\n')
        output.flush()
    from worker_client import UIA_MESSAGE_LIMIT, write_uia_marker
    controls = None
    directory = operation_id = None
    while True:
        line = sys.stdin.buffer.readline(UIA_MESSAGE_LIMIT + 1)
        if not line:
            return
        if len(line) > UIA_MESSAGE_LIMIT or not line.endswith(b'\n'):
            return
        request_id = None
        try:
            request = json.loads(line)
            request_id = request['id']
            with contextlib.redirect_stdout(sys.stderr):
                command = request['command']
                if command == 'init' and controls is None:
                    from uia_actions import Controls
                    import window_backend as windows
                    windows.initialize_dpi_awareness()
                    try:
                        use_memory_com_cache()
                    except ImportError:
                        pass  # Controls reports the normal UIA_UNAVAILABLE diagnostic.
                    controls = Controls(request['window'])
                    directory, operation_id = request['directory'], request['operation_id']
                    controls.check_window()
                    result = {'ready': True}
                elif controls is None:
                    raise ValueError('worker must be initialized first')
                elif command == 'inspect':
                    result = controls.inspect(request)
                elif command == 'prepare':
                    _, result = controls.prepare(request)
                elif command == 'perform':
                    def dispatch():
                        emit({'id': request_id, 'stage': 'dispatching'})
                        permit = json.loads(sys.stdin.buffer.readline(UIA_MESSAGE_LIMIT + 1))
                        if permit != {'id': request_id, 'permit': True}:
                            raise ValueError('mutation was not permitted')
                    def returned(answer):
                        write_uia_marker(directory, operation_id, 'returned', result=answer)
                        emit({'id': request_id, 'stage': 'returned', 'result': answer})
                    result = controls.perform(request, dispatch, returned)
                else:
                    raise ValueError('unknown worker command')
            emit({'id': request_id, 'ok': True, 'result': result})
        except Exception as exc:
            emit({'id': request_id, 'ok': False, 'error_code': getattr(exc, 'code', 'UIA_FAILED'), 'error': str(exc)})


if __name__ == "__main__":
    if sys.argv[1:] == ['--direct']:
        direct_main()
    else:
        main()
