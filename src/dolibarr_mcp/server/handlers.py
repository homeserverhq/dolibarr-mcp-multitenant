"""Tool dispatch handlers for Dolibarr MCP Server.

This module implements the dynamic dispatcher that routes tool calls
to the appropriate client methods using the TOOL_REGISTRY.

Features:
- Dynamic tool dispatch via lookup table
- DragonflyDB caching with TTL strategies
- TOON format output (default) with JSON fallback
"""

import logging
from typing import Any, Dict, List, Optional

from .tools import TOOL_REGISTRY
from .responses import (
    success_response,
    error_response,
    paginated_response,
    list_response,
)
from ..schemas.fields import LINE_FIELDS
from ..cache.strategies import (
    should_cache,
    get_ttl_for_entity,
    get_invalidation_targets,
)
from ..formats.formatter import (
    format_response,
    OutputFormat,
    get_format_from_request,
)

logger = logging.getLogger(__name__)


def _escape_sqlfilter(value: str) -> str:
    """Escape single quotes for SQL filters to prevent injection."""
    return value.replace("'", "''")


def _filter_fields(data: Any, fields: List[str]) -> Any:
    """Filter response to include only specified fields.

    Args:
        data: Response data (dict or list of dicts)
        fields: List of field names to include

    Returns:
        Filtered data with only specified fields
    """
    if isinstance(data, list):
        return [_filter_fields(item, fields) for item in data]
    if isinstance(data, dict):
        result = {k: v for k, v in data.items() if k in fields}
        # Handle nested lines with LINE_FIELDS
        if "lines" in data and "lines" in fields:
            result["lines"] = [
                _filter_fields(line, LINE_FIELDS)
                for line in data.get("lines", [])
            ]
        return result
    return data


async def _handle_search(
    client: Any,
    tool_def: Dict[str, Any],
    args: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle search tool operations with SQL filter building.

    Args:
        client: DolibarrClient instance
        tool_def: Tool definition from registry
        args: Tool arguments

    Returns:
        Formatted response with search results
    """
    handler_type = tool_def.get("search_handler")
    method_name = tool_def["method"]
    method = getattr(client, method_name)
    limit = args.get("limit", 20)
    fields = tool_def.get("fields")

    if handler_type == "ref_prefix":
        # Search products by reference prefix
        ref = _escape_sqlfilter(args["ref_prefix"])
        sqlfilter = f"(t.ref:like:'{ref}%')"
        result = await method(sqlfilter, limit)

    elif handler_type == "label":
        # Search products by label
        label = _escape_sqlfilter(args["query"])
        sqlfilter = f"(t.label:like:'%{label}%')"
        result = await method(sqlfilter, limit)

    elif handler_type == "customer":
        # Search customers by name or alias
        q = _escape_sqlfilter(args["query"])
        sqlfilter = f"((t.nom:like:'%{q}%') OR (t.name_alias:like:'%{q}%'))"
        result = await method(sqlfilter, limit)

    elif handler_type == "project":
        # Search projects by ref or title
        q = _escape_sqlfilter(args["query"])
        sqlfilter = f"((t.ref:like:'%{q}%') OR (t.title:like:'%{q}%'))"
        result = await method(sqlfilter, limit)

    elif handler_type == "proposal":
        # Search proposals by ref only (customer name search requires JOIN not supported by API)
        q = _escape_sqlfilter(args["query"])
        sqlfilter = f"(t.ref:like:'%{q}%')"
        result = await method(sqlfilter, limit)

    elif handler_type == "resolve_ref":
        # Resolve exact product reference
        ref = args["ref"]
        products = await method(f"(t.ref:like:'{_escape_sqlfilter(ref)}')", 2)

        if not products:
            return success_response(
                {"status": "not_found", "ref": ref},
                {"action": "resolve_ref"}
            )

        if len(products) == 1:
            filtered = _filter_fields(products[0], fields) if fields else products[0]
            return success_response(
                {"status": "ok", "product": filtered},
                {"action": "resolve_ref"}
            )

        # Check for exact match
        exact = [p for p in products if p.get("ref") == ref]
        if len(exact) == 1:
            filtered = _filter_fields(exact[0], fields) if fields else exact[0]
            return success_response(
                {"status": "ok", "product": filtered},
                {"action": "resolve_ref"}
            )

        # Ambiguous - multiple matches
        filtered = _filter_fields(products, fields) if fields else products
        return success_response(
            {"status": "ambiguous", "products": filtered},
            {"action": "resolve_ref"}
        )

    else:
        raise ValueError(f"Unknown search handler: {handler_type}")

    # Filter and return results
    if fields:
        result = _filter_fields(result, fields)

    return list_response(result, limit)


async def dispatch_tool(
    client: Any,
    name: str,
    args: Dict[str, Any]
) -> Dict[str, Any]:
    """Dispatch tool call to appropriate client method.

    This replaces the original 59 if-statements with a single
    lookup table approach.

    Args:
        client: DolibarrClient instance
        name: Tool name
        args: Tool arguments

    Returns:
        Formatted response (success or error)
    """
    # Get tool definition from registry
    tool_def = TOOL_REGISTRY.get(name)
    if not tool_def:
        return error_response(
            "UNKNOWN_TOOL",
            f"Tool '{name}' not found",
            status=404,
            retriable=False,
            details={"tool_name": name}
        )

    # Handle search tools specially
    if tool_def.get("search_handler"):
        return await _handle_search(client, tool_def, args)

    # Get the client method
    method_name = tool_def["method"]
    method = getattr(client, method_name, None)
    if not method:
        return error_response(
            "TOOL_EXECUTION_ERROR",
            f"Client method '{method_name}' not found",
            status=500,
            details={"method": method_name}
        )

    # Extract ID parameters if specified
    args_copy = args.copy()
    id_param = tool_def.get("id_param")
    line_param = tool_def.get("line_param")

    positional_args = []
    if id_param and id_param in args_copy:
        positional_args.append(args_copy.pop(id_param))
    if line_param and line_param in args_copy:
        positional_args.append(args_copy.pop(line_param))

    # Call the method
    if positional_args:
        result = await method(*positional_args, **args_copy)
    else:
        result = await method(**args_copy)

    # Apply field filtering if specified
    fields = tool_def.get("fields")
    if fields and result:
        result = _filter_fields(result, fields)

    # Wrap response with pagination if needed
    if tool_def.get("paginated") and isinstance(result, list):
        limit = args.get("limit", 100)
        return list_response(result, limit)

    return success_response(result)


async def dispatch_tool_cached(
    client: Any,
    name: str,
    args: Dict[str, Any],
    cache: Optional[Any] = None,
    auth_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch tool call with caching support.

    Checks cache before executing, stores results on success.
    Invalidates related caches for mutation operations.

    For multi-tenancy support, cache keys include auth context to ensure
    different users get isolated cached results.

    Args:
        client: DolibarrClient instance
        name: Tool name
        args: Tool arguments
        cache: Optional DragonflyCache instance
        auth_context: Optional auth token for cache isolation

    Returns:
        Formatted response (success or error)
    """
    import hashlib

    # Check if we should try cache
    cache_key = None
    if cache and should_cache(name):
        cache_key = cache.make_tool_key(name, args, auth_context=auth_context)
        cached = await cache.get(cache_key)
        if cached is not None:
            logger.debug(f"Cache HIT for {name}")
            if isinstance(cached, dict) and "metadata" in cached:
                cached["metadata"]["cached"] = True
            return cached

    # Execute the tool
    response = await dispatch_tool(client, name, args)

    # Cache successful read results
    if cache and response.get("success") and should_cache(name) and cache_key:
        ttl = get_ttl_for_entity(name)
        await cache.set(cache_key, response, ttl)
        logger.debug(f"Cache SET for {name} (TTL: {ttl}s)")

    # Invalidate related caches for mutations (per-user for multi-tenancy)
    if cache and response.get("success"):
        targets = get_invalidation_targets(name)
        if targets:
            for target in targets:
                if auth_context:
                    auth_hash = hashlib.md5(auth_context.encode()).hexdigest()[:8]
                    pattern = f"tool:{target}:{auth_hash}:*"
                else:
                    pattern = f"tool:{target}:*"
                await cache.invalidate_pattern(pattern)
            logger.debug(f"Cache INVALIDATE for {name}: {targets}")

    return response


async def dispatch_tool_formatted(
    client: Any,
    name: str,
    args: Dict[str, Any],
    cache: Optional[Any] = None,
    output_format: Optional[OutputFormat] = None,
    auth_context: Optional[str] = None,
) -> str:
    """Dispatch tool call and return formatted string output.

    Uses TOON format by default for optimal token efficiency.

    Args:
        client: DolibarrClient instance
        name: Tool name
        args: Tool arguments
        cache: Optional DragonflyCache instance
        output_format: Output format (default: TOON)
        auth_context: Optional auth token for cache isolation

    Returns:
        Formatted string (TOON or JSON)
    """
    # Extract format from args if not specified
    if output_format is None:
        output_format = get_format_from_request(args)

    # Remove format args before dispatch
    args_clean = {k: v for k, v in args.items()
                  if k not in ('format', 'output_format')}

    # Get response (cached or fresh)
    if cache:
        response = await dispatch_tool_cached(client, name, args_clean, cache, auth_context=auth_context)
    else:
        response = await dispatch_tool(client, name, args_clean)

    # Format the response
    return format_response(response, output_format)


async def dispatch_tool_legacy(
    client: Any,
    name: str,
    args: Dict[str, Any]
) -> Any:
    """Dispatch tool call and return raw result (legacy format).

    This provides backward compatibility with the original response format
    that returns raw data without the success/data/metadata wrapper.

    Args:
        client: DolibarrClient instance
        name: Tool name
        args: Tool arguments

    Returns:
        Raw result data (not wrapped in success response)
    """
    response = await dispatch_tool(client, name, args)

    # For backward compatibility, return raw data on success
    if response.get("success"):
        return response.get("data")

    # For errors, return the error structure
    return response.get("error", response)
