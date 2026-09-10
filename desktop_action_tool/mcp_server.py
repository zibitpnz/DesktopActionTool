#!/usr/bin/env python
"""Local stdio MCP interface; the independent CLI remains type_text.py."""
import argparse
import logging
import os
import sys

from .project_paths import PROJECT_ROOT


def create_server(bridge):
    from mcp import MCPError, types
    from mcp.server import Server
    from jsonschema import ValidationError
    from .mcp_bridge import READ_TOOLS, SERVER_VERSION
    from .mcp_contract import SPECS, HELP_ROOT, HELP_TOPICS, SERVER_INSTRUCTIONS, error_help_resource

    async def list_tools(context, parameters):
        return types.ListToolsResult(tools=[types.Tool(**spec) for name, spec in SPECS.items()
                                           if not bridge.read_only or name in READ_TOOLS])

    async def call_tool(context, parameters):
        try:
            result, images = await bridge.call(parameters.name, parameters.arguments or {})
        except (ValueError, ValidationError) as exc:
            # Do not echo invalid arguments: they may contain private text.
            raise MCPError(code=-32602, message="Invalid tool name or arguments; inspect tools/list") from exc
        if not result.get("ok", False):
            result = {**result, "help_resource": error_help_resource(result.get("error_code"))}
        import json
        return types.CallToolResult(content=[types.TextContent(text=json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
                                            *(types.ImageContent(**image) for image in images)],
                                    structuredContent=result, isError=not result.get("ok", False))

    async def list_resources(context, parameters):
        return types.ListResourcesResult(resources=[
            types.Resource(uri=HELP_ROOT + topic, name=topic, title=title,
                           description=description, mimeType="text/markdown", size=len(body.encode("utf-8")))
            for topic, (title, description, body) in HELP_TOPICS.items()])

    async def read_resource(context, parameters):
        uri = str(parameters.uri)
        # Match fixed URIs only; never resolve a supplied URI as a path or URL.
        topic = next((key for key in HELP_TOPICS if uri == HELP_ROOT + key), None)
        if topic is None:
            raise MCPError(code=-32602, message="Unknown help resource; inspect resources/list")
        return types.ReadResourceResult(contents=[types.TextResourceContents(
            uri=uri, mimeType="text/markdown", text=HELP_TOPICS[topic][2])])

    async def list_resource_templates(context, parameters):
        return types.ListResourceTemplatesResult(resourceTemplates=[])

    return Server("DesktopActionTool", version=SERVER_VERSION, on_list_tools=list_tools, on_call_tool=call_tool,
                  on_list_resources=list_resources, on_read_resource=read_resource,
                  on_list_resource_templates=list_resource_templates, instructions=SERVER_INSTRUCTIONS)


async def serve(bridge):
    import anyio
    from mcp.server.stdio import stdio_server
    server = create_server(bridge)
    try:
        async with stdio_server() as (incoming, outgoing):
            sender, receiver = anyio.create_memory_object_stream(0)
            async def forward():
                try:
                    async with sender:
                        async for message in incoming:
                            await sender.send(message)
                finally:
                    bridge.cancel_all()
            async with anyio.create_task_group() as group:
                group.start_soon(forward)
                await server.run(receiver, outgoing, server.create_initialization_options())
                group.cancel_scope.cancel()
    finally:
        await bridge.close()


def main():
    sys.dont_write_bytecode = True
    from .maintenance import startup_allowed
    if not startup_allowed(mcp=True):
        return 1
    from .release_info import add_arguments, run_cli
    information = run_cli(sys.argv[1:])
    if information is not None:
        return information
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    parser.add_argument("--read-only", action="store_true", help="Expose observation and screenshot tools only; no input or window changes.")
    parser.add_argument("--max-image-mib", type=int, choices=range(1, 9), default=8,
                        help="Maximum combined PNG bytes per response (default: 8 MiB); images are never silently resized.")
    args = parser.parse_args()
    if args.update_timeout_s is not None:
        parser.error('--update-timeout-s requires --check-updates')
    if os.name != "nt":
        print("DesktopActionTool MCP requires an interactive Windows desktop.", file=sys.stderr)
        return 1
    sys.dont_write_bytecode = True
    try:
        import anyio
        from .mcp_bridge import Bridge
        # Import SDK before starting the transport so missing-dependency errors are clear.
        from mcp.server import Server
    except ImportError:
        print("MCP support is unavailable. Run: uv sync --locked --extra mcp --extra uia", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    bridge = Bridge(PROJECT_ROOT, read_only=args.read_only,
                    max_image_bytes=args.max_image_mib * 1024 * 1024)
    try:
        anyio.run(serve, bridge)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
