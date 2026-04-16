"""Optimized Dolibarr MCP Server - Token-efficient implementation.

This is the main entry point for the Dolibarr MCP server.
It provides a declarative tool registry with dynamic dispatch.
"""

import asyncio
import json
import sys
import logging
from contextlib import asynccontextmanager
from typing import Any, List, Optional

from mcp.server import Server
from mcp.types import Tool, TextContent

from ..config import Config
from ..client import DolibarrClient, DolibarrAPIError
from .tools import TOOL_REGISTRY
from .handlers import dispatch_tool_legacy
from .responses import error_response
from ..transports.stdio import run_stdio_server
from ..transports.http import run_http_server, get_current_auth_token

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)

# Server version
VERSION = "2.0.0"

# Create MCP server instance
server = Server("dolibarr-mcp")


# =============================================================================
# TOOL DEFINITIONS
# =============================================================================

@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    """List all available tools from the registry.

    This generates Tool objects from TOOL_REGISTRY, providing
    enhanced descriptions for AI agent compatibility.
    """
    tools = []
    for name, definition in TOOL_REGISTRY.items():
        tools.append(Tool(
            name=name,
            description=definition["description"],
            inputSchema=definition["schema"]
        ))
    return tools


# =============================================================================
# TOOL HANDLERS
# =============================================================================

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> List[TextContent]:
    """Handle tool calls with dynamic dispatch.

    Uses the tool registry and dispatch_tool_legacy for backward
    compatibility with the original response format.
    """
    try:
        config = Config()
        auth_token = get_current_auth_token()
        async with DolibarrClient(config, auth_token=auth_token) as client:
            result = await dispatch_tool_legacy(client, name, arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    except DolibarrAPIError as e:
        error_data = e.to_dict() if hasattr(e, 'to_dict') else {
            "error": str(e),
            "status": e.status_code or 500
        }
        return [TextContent(type="text", text=json.dumps(error_data, indent=2))]

    except Exception as e:
        error_data = error_response(
            "TOOL_EXECUTION_ERROR",
            f"Tool failed: {e}",
            status=500,
            retriable=True,
            details={"tool": name}
        )
        return [TextContent(type="text", text=json.dumps(error_data, indent=2))]


# =============================================================================
# SERVER STARTUP
# =============================================================================

@asynccontextmanager
async def test_api_connection(config: Config | None = None):
    """Test API connection before starting server."""
    try:
        if config is None:
            config = Config()

        if not config.dolibarr_url or "your-dolibarr" in config.dolibarr_url:
            print("⚠️ DOLIBARR_URL not configured", file=sys.stderr)
            yield False
            return

        if not config.api_key or "your_" in config.api_key:
            print("⚠️ DOLIBARR_API_KEY not configured", file=sys.stderr)
            yield False
            return

        async with DolibarrClient(config) as client:
            await client.get_status()
            print("✅ Dolibarr API connected", file=sys.stderr)
            yield True

    except Exception as e:
        print(f"⚠️ API test failed: {e}", file=sys.stderr)
        yield False


async def main() -> None:
    """Run the Dolibarr MCP server."""
    config = Config()

    # Test API connection
    async with test_api_connection(config) as ok:
        if not ok:
            print("⚠️ Starting without valid API", file=sys.stderr)

    print(f"🚀 Dolibarr MCP server v{VERSION} ready", file=sys.stderr)
    print(f"📋 {len(TOOL_REGISTRY)} tools available", file=sys.stderr)

    # Start appropriate transport
    if config.mcp_transport == "http":
        await run_http_server(
            server,
            host=config.mcp_http_host,
            port=config.mcp_http_port,
            log_level=config.log_level
        )
    else:
        await run_stdio_server(server, VERSION)


def run() -> None:
    """Entry point for the server."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped", file=sys.stderr)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
