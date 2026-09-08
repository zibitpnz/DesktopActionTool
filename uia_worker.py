"""Internal, read-only UI Automation worker. Invoked by worker_client.py."""
import contextlib
import json
import sys
import types


def use_memory_com_cache():
    """Avoid comtypes' import-time disk cache in this disposable reader.

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


if __name__ == "__main__":
    main()
