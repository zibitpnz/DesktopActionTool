#!/usr/bin/env python
"""Compatible stdio MCP entry point for DesktopActionTool."""
import sys


def main():
    sys.dont_write_bytecode = True
    from desktop_action_tool.mcp_server import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
