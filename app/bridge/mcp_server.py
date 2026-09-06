"""Restricted SDK stdio executable: python -m app.bridge.mcp_server.

Only the operator supplies --credential-file. No application, database or browser
runtime is imported. The SDK is optional outside this executable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from app.bridge.client import BridgeClient
from app.bridge.protocol import BODY_MODELS, Reply, refusal


_DESCRIPTIONS = {
    'get_preparation_readiness': 'Read sanitised preparation readiness.',
    'request_approved_preparation': 'Request already operator-approved preparation; replay uses the same persisted key.',
    'get_preparation_run': 'Read a sanitised preparation run by opaque identifier.',
    'list_preparation_handoffs': 'List bounded sanitised preparation handoffs.',
    'pause_preparation': 'Pause preparation; only the local operator can clear the pause.',
}


def create_server(credential_file: Path):
    from mcp import types
    from mcp.server.lowlevel import Server

    server = Server('argus-preparation', version='1.0.0')
    client = BridgeClient(credential_file)

    @server.list_tools()
    async def list_tools():
        return [types.Tool(
            name=name,
            description=_DESCRIPTIONS[name],
            inputSchema=model.model_json_schema(),
            outputSchema=Reply.model_json_schema(),
            annotations=types.ToolAnnotations(
                readOnlyHint=name not in {'request_approved_preparation', 'pause_preparation'},
                destructiveHint=name == 'pause_preparation',
                idempotentHint=True,
                openWorldHint=False,
            ),
        ) for name, model in BODY_MODELS.items()]

    async def call_tool(request: types.CallToolRequest):
        # Register below the SDK's convenience decorator: its schema errors and
        # unknown-tool cache warnings can reflect attacker-controlled values.
        # Validate with our closed protocol before private config or HTTP access.
        try:
            # SDK retains the JSON-RPC framing fields as model extras.
            if (set(request.model_extra or {}) - {'id', 'jsonrpc'}
                    or request.params.model_extra or request.params.task is not None):
                result = refusal('INVALID_REQUEST')
            else:
                body = request.params.arguments
                result = await asyncio.to_thread(
                    client.call, request.params.name, {} if body is None else body,
                )
        except Exception:
            # Do not let the SDK serialize arbitrary exception text.
            result = refusal('TRANSPORT_ERROR')
        return types.ServerResult(types.CallToolResult(
            content=[types.TextContent(type='text', text=json.dumps(result, separators=(',', ':')))],
            structuredContent=result,
            isError=result['status'] == 'REFUSED',
        ))

    server.request_handlers[types.CallToolRequest] = call_tool
    return server


async def serve(credential_file: Path) -> None:
    from mcp.server.stdio import stdio_server

    server = create_server(credential_file)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


class _PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unrecognised arguments (possibly private).
        self.exit(2, 'INVALID_CONFIGURATION\n')


def main() -> int:
    parser = _PrivateParser(description='Restricted ARGUS preparation MCP facade', allow_abbrev=False)
    parser.add_argument('--credential-file', required=True, type=Path)
    args = parser.parse_args()
    # The SDK's lower-level malformed-envelope diagnostics may include input.
    # This process has no outward log channel; stdout is reserved for MCP.
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(serve(args.credential_file))
    except KeyboardInterrupt:
        return 0
    except Exception:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
