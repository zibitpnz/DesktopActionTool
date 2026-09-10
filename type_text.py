#!/usr/bin/env python
"""Compatible command-line entry point for DesktopActionTool."""
import json
import os
import sys


def main():
    if any(flag in sys.argv for flag in ('--dry-run', '--version', '--check-updates')):
        sys.dont_write_bytecode = True
    from desktop_action_tool.maintenance import startup_allowed
    if not startup_allowed():
        return 1
    if os.name != "nt":
        print(json.dumps({"ok": False, "error_code": "WINDOWS_REQUIRED", "error": "DesktopActionTool requires Windows"}))
        return 1
    from desktop_action_tool.desktop_cli import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
