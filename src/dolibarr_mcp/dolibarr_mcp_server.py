"""Optimized Dolibarr MCP Server - Token-efficient implementation.

Features:
- TOON format output for ~60% token reduction
- DragonflyDB/Redis cache with automatic invalidation
- Field filtering for minimal response size
"""

import asyncio
import json
import sys
import logging
import os
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from mcp.server.models import InitializationOptions
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent

from .config import Config
from .dolibarr_client import DolibarrClient, DolibarrAPIError

# TOON format and Cache imports
from .formats.toon_encoder import ToonEncoder
from .cache.dragonfly import DragonflyCache
from .cache.strategies import should_cache, get_ttl_for_entity, get_invalidation_targets

# Global cache instance
_cache: Optional[DragonflyCache] = None
_toon_encoder = ToonEncoder()

# Multi-tenancy support: current auth token from request
_current_auth_token: Optional[str] = None

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
import uvicorn

# Authentication imports
from .auth.api_key import APIKeyAuth

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)

server = Server("dolibarr-mcp")

# =============================================================================
# RESPONSE FILTERS - Reduce token usage by returning only essential fields
# =============================================================================

CUSTOMER_FIELDS = ["id", "name", "name_alias", "email", "phone", "address", "town", "zip",
                   "country_code", "client", "fournisseur", "code_client", "status"]

PRODUCT_FIELDS = ["id", "ref", "label", "description", "price", "price_ttc", "type",
                  "status", "stock_reel", "barcode"]

INVOICE_FIELDS = ["id", "ref", "socid", "date", "date_lim_reglement", "total_ht", "total_tva",
                  "total_ttc", "paye", "status", "lines"]

ORDER_FIELDS = ["id", "ref", "socid", "date", "total_ht", "total_ttc", "status", "lines"]

PROPOSAL_FIELDS = ["id", "ref", "socid", "date", "fin_validite", "total_ht", "total_tva",
                   "total_ttc", "status", "lines"]

PROJECT_FIELDS = ["id", "ref", "title", "description", "socid", "status", "date_start", "date_end"]

CONTACT_FIELDS = ["id", "firstname", "lastname", "email", "phone", "socid"]

USER_FIELDS = ["id", "login", "lastname", "firstname", "email", "admin", "status"]

LINE_FIELDS = ["id", "fk_product", "desc", "qty", "subprice", "total_ht", "total_ttc", "tva_tx"]


def _filter_fields(data: Any, fields: List[str]) -> Any:
    """Filter response to include only specified fields."""
    if isinstance(data, list):
        return [_filter_fields(item, fields) for item in data]
    if isinstance(data, dict):
        result = {k: v for k, v in data.items() if k in fields}
        # Handle nested lines
        if "lines" in data and "lines" in fields:
            result["lines"] = [_filter_fields(line, LINE_FIELDS) for line in data.get("lines", [])]
        return result
    return data


def _escape_sqlfilter(value: str) -> str:
    """Escape single quotes for SQL filters."""
    return value.replace("'", "''")


# =============================================================================
# TOOL SCHEMA HELPERS - Reduce code duplication
# =============================================================================

def _id_schema(name: str) -> dict:
    """Generate simple ID-based schema."""
    return {
        "type": "object",
        "properties": {name: {"type": "integer", "description": f"{name}"}},
        "required": [name],
        "additionalProperties": False
    }

def _list_schema(with_status: bool = False, status_type: str = "string") -> dict:
    """Generate list/pagination schema."""
    props = {"limit": {"type": "integer", "default": 100}}
    if with_status:
        props["status"] = {"type": status_type}
    return {"type": "object", "properties": props, "additionalProperties": False}

def _search_schema() -> dict:
    """Generate search schema."""
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 20}
        },
        "required": ["query"],
        "additionalProperties": False
    }

def _line_schema(entity: str) -> dict:
    """Generate line item schema for invoices/proposals/orders."""
    return {
        "type": "object",
        "properties": {
            f"{entity}_id": {"type": "integer"},
            "desc": {"type": "string"},
            "qty": {"type": "number"},
            "subprice": {"type": "number"},
            "product_id": {"type": "integer"},
            "product_type": {"type": "integer", "default": 0},
            "tva_tx": {"type": "number"}
        },
        "required": [f"{entity}_id", "desc", "qty", "subprice"],
        "additionalProperties": False
    }


# =============================================================================
# TOOL DEFINITIONS - Concise descriptions for token efficiency
# =============================================================================

@server.list_tools()
async def handle_list_tools():
    """List all available tools."""
    return [
        # System
        Tool(name="test_connection", description="Test Dolibarr API connection",
             inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
        Tool(name="get_status", description="Get Dolibarr system status",
             inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),

        # Search (consolidated)
        Tool(name="search_products_by_ref", description="Search products by reference prefix",
             inputSchema={"type": "object", "properties": {"ref_prefix": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": ["ref_prefix"], "additionalProperties": False}),
        Tool(name="search_products_by_label", description="Search products by label/name",
             inputSchema=_search_schema()),
        Tool(name="search_customers",
             description="Search customers/thirdparties by name or alias. IMPORTANT: Use this first to get the 'id' (socid) when you need to query proposals, invoices, or orders for a customer. Returns customer ID that you can use with get_customer_proposals, get_customer_invoices, get_customer_orders.",
             inputSchema=_search_schema()),
        Tool(name="resolve_product_ref", description="Get exact product by reference",
             inputSchema={"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"], "additionalProperties": False}),

        # Users
        Tool(name="get_users", description="List users (paginated)",
             inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 100}, "page": {"type": "integer", "default": 1}}, "additionalProperties": False}),
        Tool(name="get_user_by_id", description="Get user by ID", inputSchema=_id_schema("user_id")),
        Tool(name="create_user", description="Create user",
             inputSchema={"type": "object", "properties": {"login": {"type": "string"}, "lastname": {"type": "string"}, "firstname": {"type": "string"}, "email": {"type": "string"}, "password": {"type": "string"}, "admin": {"type": "integer", "default": 0}}, "required": ["login", "lastname"], "additionalProperties": False}),
        Tool(name="update_user", description="Update user",
             inputSchema={"type": "object", "properties": {"user_id": {"type": "integer"}, "login": {"type": "string"}, "lastname": {"type": "string"}, "firstname": {"type": "string"}, "email": {"type": "string"}, "admin": {"type": "integer"}}, "required": ["user_id"], "additionalProperties": False}),
        Tool(name="delete_user", description="Delete user", inputSchema=_id_schema("user_id")),

        # Customers
        Tool(name="get_customers", description="List customers (paginated)",
             inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 100}, "page": {"type": "integer", "default": 1}}, "additionalProperties": False}),
        Tool(name="get_customer_by_id", description="Get customer by ID", inputSchema=_id_schema("customer_id")),
        Tool(name="create_customer", description="Create customer",
             inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}, "address": {"type": "string"}, "town": {"type": "string"}, "zip": {"type": "string"}, "country_id": {"type": "integer", "default": 1}, "type": {"type": "integer", "default": 1}, "status": {"type": "integer", "default": 1}}, "required": ["name"], "additionalProperties": False}),
        Tool(name="update_customer", description="Update customer",
             inputSchema={"type": "object", "properties": {"customer_id": {"type": "integer"}, "name": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}, "address": {"type": "string"}, "town": {"type": "string"}, "zip": {"type": "string"}, "status": {"type": "integer"}}, "required": ["customer_id"], "additionalProperties": False}),
        Tool(name="delete_customer", description="Delete customer", inputSchema=_id_schema("customer_id")),

        # Products
        Tool(name="get_products", description="List products", inputSchema=_list_schema()),
        Tool(name="get_product_by_id", description="Get product by ID", inputSchema=_id_schema("product_id")),
        Tool(name="create_product", description="Create product",
             inputSchema={"type": "object", "properties": {"label": {"type": "string"}, "price": {"type": "number"}, "description": {"type": "string"}, "stock": {"type": "integer"}}, "required": ["label", "price"], "additionalProperties": False}),
        Tool(name="update_product", description="Update product",
             inputSchema={"type": "object", "properties": {"product_id": {"type": "integer"}, "label": {"type": "string"}, "price": {"type": "number"}, "description": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}),
        Tool(name="delete_product", description="Delete product", inputSchema=_id_schema("product_id")),

        # Invoices
        Tool(name="get_invoices",
             description="List invoices with filters. RECOMMENDED: Use get_customer_invoices when filtering by customer. Status: 'draft', 'unpaid', 'paid'. Results sorted by date DESC.",
             inputSchema={"type": "object", "properties": {
                 "limit": {"type": "integer", "default": 50, "description": "Max results (default 50)"},
                 "status": {"type": "string", "description": "Filter by status: 'draft', 'unpaid', 'paid'"},
                 "socid": {"type": "integer", "description": "Filter by customer ID (use get_customer_invoices instead)"},
                 "year": {"type": "integer", "description": "Filter by year (e.g., 2026)"},
                 "month": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Filter by month (1-12), requires year"},
                 "date_start": {"type": "string", "description": "Filter from date (YYYY-MM-DD)"},
                 "date_end": {"type": "string", "description": "Filter to date (YYYY-MM-DD)"},
                 "sortorder": {"type": "string", "enum": ["ASC", "DESC"], "default": "DESC"}
             }, "additionalProperties": False}),
        Tool(name="get_customer_invoices",
             description="BEST tool for customer invoices. Get invoices for a specific customer. Use status='unpaid' for pending payments. First use search_customers to get the socid if you only have the customer name.",
             inputSchema={"type": "object", "properties": {
                 "socid": {"type": "integer", "description": "Customer ID (required). Use search_customers first if you only have the name."},
                 "limit": {"type": "integer", "default": 10, "description": "Max results (default 10)"},
                 "status": {"type": "string", "description": "Filter by status: 'draft', 'unpaid', 'paid'"},
                 "year": {"type": "integer", "description": "Filter by year (e.g., 2026)"},
                 "month": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Filter by month (1-12), requires year"}
             }, "required": ["socid"], "additionalProperties": False}),
        Tool(name="get_invoice_by_id", description="Get invoice by ID", inputSchema=_id_schema("invoice_id")),
        Tool(name="create_invoice", description="Create invoice with lines",
             inputSchema={"type": "object", "properties": {
                 "customer_id": {"type": "integer"},
                 "date": {"type": "string"},
                 "due_date": {"type": "string"},
                 "lines": {"type": "array", "items": {"type": "object", "properties": {"desc": {"type": "string"}, "qty": {"type": "number"}, "subprice": {"type": "number"}, "product_id": {"type": "integer"}, "product_type": {"type": "integer"}, "vat": {"type": "number"}}, "required": ["desc", "qty", "subprice"]}}
             }, "required": ["customer_id", "lines"], "additionalProperties": False}),
        Tool(name="update_invoice", description="Update invoice",
             inputSchema={"type": "object", "properties": {"invoice_id": {"type": "integer"}, "date": {"type": "string"}, "due_date": {"type": "string"}}, "required": ["invoice_id"], "additionalProperties": False}),
        Tool(name="delete_invoice", description="Delete invoice", inputSchema=_id_schema("invoice_id")),
        Tool(name="add_invoice_line", description="Add line to invoice", inputSchema=_line_schema("invoice")),
        Tool(name="update_invoice_line", description="Update invoice line",
             inputSchema={"type": "object", "properties": {"invoice_id": {"type": "integer"}, "line_id": {"type": "integer"}, "desc": {"type": "string"}, "qty": {"type": "number"}, "subprice": {"type": "number"}, "vat": {"type": "number"}}, "required": ["invoice_id", "line_id"], "additionalProperties": False}),
        Tool(name="delete_invoice_line", description="Delete invoice line",
             inputSchema={"type": "object", "properties": {"invoice_id": {"type": "integer"}, "line_id": {"type": "integer"}}, "required": ["invoice_id", "line_id"], "additionalProperties": False}),
        Tool(name="validate_invoice", description="Validate draft invoice",
             inputSchema={"type": "object", "properties": {"invoice_id": {"type": "integer"}, "warehouse_id": {"type": "integer", "default": 0}}, "required": ["invoice_id"], "additionalProperties": False}),

        # Orders
        Tool(name="get_orders",
             description="List orders with filters. RECOMMENDED: Use get_customer_orders when filtering by customer. Results sorted by date DESC.",
             inputSchema={"type": "object", "properties": {
                 "limit": {"type": "integer", "default": 50, "description": "Max results (default 50)"},
                 "status": {"type": "string", "description": "Filter by status"},
                 "socid": {"type": "integer", "description": "Filter by customer ID (use get_customer_orders instead)"},
                 "year": {"type": "integer", "description": "Filter by year (e.g., 2026)"},
                 "month": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Filter by month (1-12), requires year"},
                 "date_start": {"type": "string", "description": "Filter from date (YYYY-MM-DD)"},
                 "date_end": {"type": "string", "description": "Filter to date (YYYY-MM-DD)"},
                 "sortorder": {"type": "string", "enum": ["ASC", "DESC"], "default": "DESC"}
             }, "additionalProperties": False}),
        Tool(name="get_customer_orders",
             description="BEST tool for customer orders. Get orders for a specific customer. First use search_customers to get the socid if you only have the customer name.",
             inputSchema={"type": "object", "properties": {
                 "socid": {"type": "integer", "description": "Customer ID (required). Use search_customers first if you only have the name."},
                 "limit": {"type": "integer", "default": 10, "description": "Max results (default 10)"},
                 "status": {"type": "string", "description": "Filter by status"},
                 "year": {"type": "integer", "description": "Filter by year (e.g., 2026)"},
                 "month": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Filter by month (1-12), requires year"}
             }, "required": ["socid"], "additionalProperties": False}),
        Tool(name="get_order_by_id", description="Get order by ID", inputSchema=_id_schema("order_id")),
        Tool(name="create_order", description="Create order",
             inputSchema={"type": "object", "properties": {"customer_id": {"type": "integer"}, "date": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}),
        Tool(name="update_order", description="Update order",
             inputSchema={"type": "object", "properties": {"order_id": {"type": "integer"}, "date": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}),
        Tool(name="delete_order", description="Delete order", inputSchema=_id_schema("order_id")),

        # Contacts
        Tool(name="get_contacts", description="List contacts", inputSchema=_list_schema()),
        Tool(name="get_contact_by_id", description="Get contact by ID", inputSchema=_id_schema("contact_id")),
        Tool(name="create_contact", description="Create contact",
             inputSchema={"type": "object", "properties": {"firstname": {"type": "string"}, "lastname": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}, "socid": {"type": "integer"}}, "required": ["firstname", "lastname"], "additionalProperties": False}),
        Tool(name="update_contact", description="Update contact",
             inputSchema={"type": "object", "properties": {"contact_id": {"type": "integer"}, "firstname": {"type": "string"}, "lastname": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}}, "required": ["contact_id"], "additionalProperties": False}),
        Tool(name="delete_contact", description="Delete contact", inputSchema=_id_schema("contact_id")),

        # Projects
        Tool(name="get_projects", description="List projects. Status: 0=draft, 1=open, 2=closed",
             inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 100}, "page": {"type": "integer", "default": 1}, "status": {"type": "integer", "default": 1}}, "additionalProperties": False}),
        Tool(name="get_project_by_id", description="Get project by ID", inputSchema=_id_schema("project_id")),
        Tool(name="search_projects", description="Search projects by ref/title", inputSchema=_search_schema()),
        Tool(name="create_project", description="Create project",
             inputSchema={"type": "object", "properties": {"title": {"type": "string"}, "ref": {"type": "string"}, "description": {"type": "string"}, "socid": {"type": "integer"}, "status": {"type": "integer", "default": 1}}, "required": ["title"], "additionalProperties": False}),
        Tool(name="update_project", description="Update project",
             inputSchema={"type": "object", "properties": {"project_id": {"type": "integer"}, "title": {"type": "string"}, "description": {"type": "string"}, "status": {"type": "integer"}}, "required": ["project_id"], "additionalProperties": False}),
        Tool(name="delete_project", description="Delete project", inputSchema=_id_schema("project_id")),

        # Proposals
        Tool(name="get_proposals",
             description="List proposals/quotes with filters. RECOMMENDED: Use get_customer_proposals instead when filtering by customer. Status codes: 0=draft, 1=validated/open, 2=signed/won, 3=refused/lost. Results sorted by date DESC.",
             inputSchema={"type": "object", "properties": {
                 "limit": {"type": "integer", "default": 50, "description": "Max results (default 50)"},
                 "status": {"type": "integer", "description": "Filter by status: 0=draft, 1=validated, 2=signed/won, 3=refused/lost"},
                 "socid": {"type": "integer", "description": "Filter by customer ID (use get_customer_proposals instead)"},
                 "year": {"type": "integer", "description": "Filter by year (e.g., 2026)"},
                 "month": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Filter by month (1-12), requires year"},
                 "date_start": {"type": "string", "description": "Filter from date (YYYY-MM-DD)"},
                 "date_end": {"type": "string", "description": "Filter to date (YYYY-MM-DD)"},
                 "sortorder": {"type": "string", "enum": ["ASC", "DESC"], "default": "DESC", "description": "Sort order"}
             }, "additionalProperties": False}),
        Tool(name="get_customer_proposals",
             description="BEST tool for customer proposals. Get proposals for a specific customer with flexible status filtering. Use statuses=[0,1] for open/pending, status=2 for won, status=3 for lost. If no status filter specified, returns ALL proposals. First use search_customers to get the socid if you only have the customer name.",
             inputSchema={"type": "object", "properties": {
                 "socid": {"type": "integer", "description": "Customer ID (required). Use search_customers first if you only have the name."},
                 "limit": {"type": "integer", "default": 10, "description": "Max results (default 10)"},
                 "status": {"type": "integer", "description": "Filter by single status: 0=draft, 1=validated, 2=signed/won, 3=refused/lost"},
                 "statuses": {"type": "array", "items": {"type": "integer"}, "description": "Filter multiple statuses. Example: [0,1] for open proposals, [2,3] for closed"},
                 "year": {"type": "integer", "description": "Filter by year (e.g., 2026)"},
                 "month": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Filter by month (1-12), requires year"},
                 "include_draft": {"type": "boolean", "default": False, "description": "Include draft proposals (status=0)"},
                 "include_validated": {"type": "boolean", "default": False, "description": "Include validated/open proposals (status=1)"},
                 "include_signed": {"type": "boolean", "default": False, "description": "Include signed/won proposals (status=2)"},
                 "include_refused": {"type": "boolean", "default": False, "description": "Include refused/lost proposals (status=3)"}
             }, "required": ["socid"], "additionalProperties": False}),
        Tool(name="get_proposal_by_id",
             description="Get a single proposal by its ID. Use this when you have the exact proposal ID.",
             inputSchema=_id_schema("proposal_id")),
        Tool(name="search_proposals",
             description="Search proposals by reference number (e.g., 'OF26012770'). NOTE: This only searches by ref, NOT by customer name. To find proposals by customer, first use search_customers to get socid, then use get_customer_proposals.",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string", "description": "Search term for proposal reference (e.g., 'OF26')"},
                 "limit": {"type": "integer", "default": 20},
                 "sortorder": {"type": "string", "enum": ["ASC", "DESC"], "default": "DESC"}
             }, "required": ["query"], "additionalProperties": False}),
        Tool(name="create_proposal",
             description="Create proposal with optional lines. Required: customer_id or socid. Use get_customer_proposals with socid for proposal queries; use create_proposal for creation instead of dolibarr_raw_api.",
             inputSchema={"type": "object", "properties": {
                 "customer_id": {"type": "integer"},
                 "socid": {"type": "integer"},
                 "date": {"type": "string"},
                 "duree_validite": {"type": "integer", "default": 30},
                 "project_id": {"type": "integer"},
                 "fk_project": {"type": "integer"},
                 "ref_client": {"type": "string"},
                 "cond_reglement_id": {"type": "integer"},
                 "mode_reglement_id": {"type": "integer"},
                 "availability_id": {"type": "integer"},
                 "demand_reason_id": {"type": "integer"},
                 "fk_input_reason": {"type": "integer"},
                 "fk_delivery_address": {"type": "integer"},
                 "date_livraison": {"type": "string"},
                 "delivery_date": {"type": "string"},
                 "incoterms": {"type": "string"},
                 "tos": {"type": "string"},
                 "note_public": {"type": "string"},
                 "note_private": {"type": "string"},
                 "lines": {"type": "array", "items": {"type": "object", "properties": {"desc": {"type": "string"}, "description": {"type": "string"}, "qty": {"type": "number"}, "subprice": {"type": "number"}, "product_id": {"type": "integer"}, "product_type": {"type": "integer"}, "tva_tx": {"type": "number"}, "remise_percent": {"type": "number"}}, "required": ["qty", "subprice"], "anyOf": [{"required": ["desc"]}, {"required": ["description"]}]}}
             }, "anyOf": [{"required": ["customer_id"]}, {"required": ["socid"]}], "additionalProperties": False}),
        Tool(name="update_proposal",
             description="Update proposal fields. Use duree_validite to change validity period (fin_validite is auto-calculated). Use note_private for internal comments.",
             inputSchema={"type": "object", "properties": {
                 "proposal_id": {"type": "integer", "description": "Proposal ID (required)"},
                 "date": {"type": "string", "description": "Proposal date in YYYY-MM-DD format"},
                 "datep": {"type": "string", "description": "Dolibarr proposal date field (alias)"},
                 "duree_validite": {"type": "integer", "description": "Validity duration in days (auto-calculates fin_validite)"},
                 "note_public": {"type": "string", "description": "Public notes (visible to customer)"},
                 "note_private": {"type": "string", "description": "Private notes (internal only)"},
                 "ref_client": {"type": "string", "description": "Customer reference number"},
                 "project_id": {"type": "integer", "description": "Project ID (alias of fk_project)"},
                 "fk_project": {"type": "integer", "description": "Link to project ID"},
                 "cond_reglement_id": {"type": "integer", "description": "Payment terms ID"},
                 "mode_reglement_id": {"type": "integer", "description": "Payment method ID"},
                 "availability_id": {"type": "integer", "description": "Availability/delivery lead time ID"},
                 "demand_reason_id": {"type": "integer", "description": "Demand reason/source ID"},
                 "fk_input_reason": {"type": "integer", "description": "Input/source reason ID"},
                 "fk_delivery_address": {"type": "integer", "description": "Delivery address ID"},
                 "date_livraison": {"type": "string", "description": "Delivery date in YYYY-MM-DD format"},
                 "delivery_date": {"type": "string", "description": "Alias for delivery date in YYYY-MM-DD format"},
                 "incoterms": {"type": "string", "description": "Incoterms text/code"},
                 "tos": {"type": "string", "description": "Terms and conditions"}
             }, "required": ["proposal_id"], "additionalProperties": False}),
        Tool(name="append_proposal_note",
             description="Add a timestamped note to a proposal WITHOUT overwriting existing notes. Perfect for tracking comments, follow-ups, and conversation history.",
             inputSchema={"type": "object", "properties": {
                 "proposal_id": {"type": "integer", "description": "Proposal ID (required)"},
                 "note": {"type": "string", "description": "Note text to append"},
                 "note_type": {"type": "string", "enum": ["private", "public"], "default": "private", "description": "private=internal, public=visible to customer"},
                 "add_timestamp": {"type": "boolean", "default": True, "description": "Add timestamp prefix [YYYY-MM-DD HH:MM]"}
             }, "required": ["proposal_id", "note"], "additionalProperties": False}),
        Tool(name="delete_proposal", description="Delete proposal", inputSchema=_id_schema("proposal_id")),
        Tool(name="add_proposal_line", description="Add line to proposal", inputSchema=_line_schema("proposal")),
        Tool(name="update_proposal_line", description="Update proposal line",
             inputSchema={"type": "object", "properties": {"proposal_id": {"type": "integer"}, "line_id": {"type": "integer"}, "desc": {"type": "string"}, "description": {"type": "string"}, "qty": {"type": "number"}, "subprice": {"type": "number"}, "tva_tx": {"type": "number"}, "remise_percent": {"type": "number"}, "product_id": {"type": "integer"}, "product_type": {"type": "integer"}}, "required": ["proposal_id", "line_id"], "additionalProperties": False}),
        Tool(name="delete_proposal_line", description="Delete proposal line",
             inputSchema={"type": "object", "properties": {"proposal_id": {"type": "integer"}, "line_id": {"type": "integer"}}, "required": ["proposal_id", "line_id"], "additionalProperties": False}),
        Tool(name="validate_proposal", description="Validate draft proposal", inputSchema=_id_schema("proposal_id")),
        Tool(name="close_proposal", description="Close proposal: status 2=signed/won, 3=refused/lost",
             inputSchema={"type": "object", "properties": {"proposal_id": {"type": "integer"}, "status": {"type": "integer", "enum": [2, 3]}, "note": {"type": "string"}}, "required": ["proposal_id", "status"], "additionalProperties": False}),
        Tool(name="set_proposal_to_draft", description="Revert proposal to draft", inputSchema=_id_schema("proposal_id")),

        # Raw API (escape hatch)
        Tool(name="dolibarr_raw_api",
             description="WARNING: Only use this as last resort! Direct API call for advanced operations not covered by other tools. DO NOT use for standard proposals/invoices/orders workflows - use the specific tools instead. If you use sqlfilters, note that column names are internal (e.g., 't.fk_soc' not 't.socid').",
             inputSchema={"type": "object", "properties": {"method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]}, "endpoint": {"type": "string"}, "params": {"type": "object"}, "data": {"type": "object"}}, "required": ["method", "endpoint"], "additionalProperties": False}),
    ]


# =============================================================================
# TOOL HANDLERS - Optimized with response filtering
# =============================================================================

async def _get_cache() -> Optional[DragonflyCache]:
    """Get or initialize cache instance."""
    global _cache
    if _cache is None:
        cache_enabled = os.getenv("CACHE_ENABLED", "true").lower() == "true"
        if cache_enabled:
            _cache = DragonflyCache(
                host=os.getenv("DRAGONFLY_HOST", "localhost"),
                port=int(os.getenv("DRAGONFLY_PORT", "6379")),
                password=os.getenv("DRAGONFLY_PASSWORD"),
                prefix="dolibarr:",
                enabled=True
            )
            await _cache.connect()
    return _cache


def _format_response(data: Any, use_toon: bool = True) -> str:
    """Format response as TOON or JSON."""
    if use_toon:
        try:
            return _toon_encoder.encode(data)
        except Exception:
            pass  # Fallback to JSON
    return json.dumps(data, indent=2, default=str)


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict):
    """Handle tool calls with caching and TOON format responses."""
    import time
    global _cache
    use_toon = os.getenv("OUTPUT_FORMAT", "toon").lower() == "toon"
    start_time = time.time()

    # Log incoming request
    args_str = json.dumps(arguments, default=str) if arguments else "{}"
    print(f"📥 TOOL: {name} | Args: {args_str}", file=sys.stderr)

    try:
        # Initialize cache if needed
        cache = await _get_cache()
        cache_status = "DISABLED"

        # Check cache for read operations
        cache_key = None
        if cache and cache._connected and should_cache(name):
            cache_key = cache.make_tool_key(name, arguments)
            cached = await cache.get(cache_key)
            if cached is not None:
                elapsed = (time.time() - start_time) * 1000
                print(f"⚡ CACHE HIT: {name} | Time: {elapsed:.1f}ms", file=sys.stderr)
                return [TextContent(type="text", text=_format_response(cached, use_toon))]
            cache_status = "MISS"
        elif cache and cache._connected:
            cache_status = "SKIP (write op)"
        elif not cache:
            cache_status = "DISABLED"

        # Execute tool
        config = Config()
        async with DolibarrClient(config, auth_token=_current_auth_token) as client:
            result = await _dispatch_tool(client, name, arguments)

        # Cache result for read operations
        if cache and cache._connected and cache_key and should_cache(name):
            ttl = get_ttl_for_entity(name)
            await cache.set(cache_key, result, ttl)
            cache_status = f"MISS → STORED (TTL: {ttl}s)"

        # Invalidate related caches for write operations
        if cache and cache._connected:
            targets = get_invalidation_targets(name)
            if targets:
                for target in targets:
                    await cache.invalidate_pattern(f"tool:{target}:*")
                print(f"🗑️  CACHE INVALIDATED: {targets}", file=sys.stderr)

        elapsed = (time.time() - start_time) * 1000
        print(f"✅ DONE: {name} | Cache: {cache_status} | Time: {elapsed:.1f}ms", file=sys.stderr)

        return [TextContent(type="text", text=_format_response(result, use_toon))]

    except DolibarrAPIError as e:
        elapsed = (time.time() - start_time) * 1000
        print(f"❌ ERROR: {name} | {e} | Time: {elapsed:.1f}ms", file=sys.stderr)
        error_response = {"error": str(e), "status": e.status_code or 500}
        return [TextContent(type="text", text=_format_response(error_response, use_toon))]
    except Exception as e:
        elapsed = (time.time() - start_time) * 1000
        print(f"❌ ERROR: {name} | {e} | Time: {elapsed:.1f}ms", file=sys.stderr)
        error_response = {"error": f"Tool failed: {e}", "status": 500}
        return [TextContent(type="text", text=_format_response(error_response, use_toon))]


async def _dispatch_tool(client: DolibarrClient, name: str, args: dict) -> Any:
    """Dispatch tool call to appropriate handler with response filtering."""

    # System
    if name == "test_connection":
        return await client.get_status()
    if name == "get_status":
        return await client.get_status()

    # Search
    if name == "search_products_by_ref":
        ref = _escape_sqlfilter(args["ref_prefix"])
        result = await client.search_products(f"(t.ref:like:'{ref}%')", args.get("limit", 20))
        return _filter_fields(result, PRODUCT_FIELDS)
    if name == "search_products_by_label":
        label = _escape_sqlfilter(args["query"])
        result = await client.search_products(f"(t.label:like:'%{label}%')", args.get("limit", 20))
        return _filter_fields(result, PRODUCT_FIELDS)
    if name == "search_customers":
        q = _escape_sqlfilter(args["query"])
        result = await client.search_customers(f"((t.nom:like:'%{q}%') OR (t.name_alias:like:'%{q}%'))", args.get("limit", 20))
        return _filter_fields(result, CUSTOMER_FIELDS)
    if name == "resolve_product_ref":
        ref = args["ref"]
        products = await client.search_products(f"(t.ref:like:'{_escape_sqlfilter(ref)}')", 2)
        if not products:
            return {"status": "not_found", "ref": ref}
        if len(products) == 1:
            return {"status": "ok", "product": _filter_fields(products[0], PRODUCT_FIELDS)}
        exact = [p for p in products if p.get("ref") == ref]
        if len(exact) == 1:
            return {"status": "ok", "product": _filter_fields(exact[0], PRODUCT_FIELDS)}
        return {"status": "ambiguous", "products": _filter_fields(products, PRODUCT_FIELDS)}

    # Users
    if name == "get_users":
        result = await client.get_users(args.get("limit", 100), args.get("page", 1))
        return _filter_fields(result, USER_FIELDS)
    if name == "get_user_by_id":
        result = await client.get_user_by_id(args["user_id"])
        return _filter_fields(result, USER_FIELDS)
    if name == "create_user":
        return await client.create_user(**args)
    if name == "update_user":
        uid = args.pop("user_id")
        return await client.update_user(uid, **args)
    if name == "delete_user":
        return await client.delete_user(args["user_id"])

    # Customers
    if name == "get_customers":
        result = await client.get_customers(args.get("limit", 100), args.get("page", 1))
        return _filter_fields(result, CUSTOMER_FIELDS)
    if name == "get_customer_by_id":
        result = await client.get_customer_by_id(args["customer_id"])
        return _filter_fields(result, CUSTOMER_FIELDS)
    if name == "create_customer":
        return await client.create_customer(**args)
    if name == "update_customer":
        cid = args.pop("customer_id")
        return await client.update_customer(cid, **args)
    if name == "delete_customer":
        return await client.delete_customer(args["customer_id"])

    # Products
    if name == "get_products":
        result = await client.get_products(args.get("limit", 100))
        return _filter_fields(result, PRODUCT_FIELDS)
    if name == "get_product_by_id":
        result = await client.get_product_by_id(args["product_id"])
        return _filter_fields(result, PRODUCT_FIELDS)
    if name == "create_product":
        return await client.create_product(**args)
    if name == "update_product":
        pid = args.pop("product_id")
        return await client.update_product(pid, **args)
    if name == "delete_product":
        return await client.delete_product(args["product_id"])

    # Invoices
    if name == "get_invoices":
        result = await client.get_invoices(
            limit=args.get("limit", 50),
            status=args.get("status"),
            socid=args.get("socid"),
            year=args.get("year"),
            month=args.get("month"),
            date_start=args.get("date_start"),
            date_end=args.get("date_end"),
            sortorder=args.get("sortorder", "DESC"),
        )
        return _filter_fields(result, INVOICE_FIELDS)
    if name == "get_customer_invoices":
        result = await client.get_customer_invoices(
            socid=args["socid"],
            limit=args.get("limit", 10),
            status=args.get("status"),
            year=args.get("year"),
            month=args.get("month"),
        )
        return _filter_fields(result, INVOICE_FIELDS)
    if name == "get_invoice_by_id":
        result = await client.get_invoice_by_id(args["invoice_id"])
        return _filter_fields(result, INVOICE_FIELDS)
    if name == "create_invoice":
        return await client.create_invoice(**args)
    if name == "update_invoice":
        iid = args.pop("invoice_id")
        return await client.update_invoice(iid, **args)
    if name == "delete_invoice":
        return await client.delete_invoice(args["invoice_id"])
    if name == "add_invoice_line":
        iid = args.pop("invoice_id")
        return await client.add_invoice_line(iid, **args)
    if name == "update_invoice_line":
        iid, lid = args.pop("invoice_id"), args.pop("line_id")
        return await client.update_invoice_line(iid, lid, **args)
    if name == "delete_invoice_line":
        return await client.delete_invoice_line(args["invoice_id"], args["line_id"])
    if name == "validate_invoice":
        return await client.validate_invoice(args["invoice_id"], args.get("warehouse_id", 0))

    # Orders
    if name == "get_orders":
        result = await client.get_orders(
            limit=args.get("limit", 50),
            status=args.get("status"),
            socid=args.get("socid"),
            year=args.get("year"),
            month=args.get("month"),
            date_start=args.get("date_start"),
            date_end=args.get("date_end"),
            sortorder=args.get("sortorder", "DESC"),
        )
        return _filter_fields(result, ORDER_FIELDS)
    if name == "get_customer_orders":
        result = await client.get_customer_orders(
            socid=args["socid"],
            limit=args.get("limit", 10),
            status=args.get("status"),
            year=args.get("year"),
            month=args.get("month"),
        )
        return _filter_fields(result, ORDER_FIELDS)
    if name == "get_order_by_id":
        result = await client.get_order_by_id(args["order_id"])
        return _filter_fields(result, ORDER_FIELDS)
    if name == "create_order":
        return await client.create_order(**args)
    if name == "update_order":
        oid = args.pop("order_id")
        return await client.update_order(oid, **args)
    if name == "delete_order":
        return await client.delete_order(args["order_id"])

    # Contacts
    if name == "get_contacts":
        result = await client.get_contacts(args.get("limit", 100))
        return _filter_fields(result, CONTACT_FIELDS)
    if name == "get_contact_by_id":
        result = await client.get_contact_by_id(args["contact_id"])
        return _filter_fields(result, CONTACT_FIELDS)
    if name == "create_contact":
        return await client.create_contact(**args)
    if name == "update_contact":
        cid = args.pop("contact_id")
        return await client.update_contact(cid, **args)
    if name == "delete_contact":
        return await client.delete_contact(args["contact_id"])

    # Projects
    if name == "get_projects":
        result = await client.get_projects(args.get("limit", 100), args.get("page", 1), args.get("status"))
        return _filter_fields(result, PROJECT_FIELDS)
    if name == "get_project_by_id":
        result = await client.get_project_by_id(args["project_id"])
        return _filter_fields(result, PROJECT_FIELDS)
    if name == "search_projects":
        q = _escape_sqlfilter(args["query"])
        result = await client.search_projects(f"((t.ref:like:'%{q}%') OR (t.title:like:'%{q}%'))", args.get("limit", 20))
        return _filter_fields(result, PROJECT_FIELDS)
    if name == "create_project":
        return await client.create_project(**args)
    if name == "update_project":
        pid = args.pop("project_id")
        return await client.update_project(pid, **args)
    if name == "delete_project":
        return await client.delete_project(args["project_id"])

    # Proposals
    if name == "get_proposals":
        result = await client.get_proposals(
            limit=args.get("limit", 50),
            status=args.get("status"),
            socid=args.get("socid"),
            year=args.get("year"),
            month=args.get("month"),
            date_start=args.get("date_start"),
            date_end=args.get("date_end"),
            sortorder=args.get("sortorder", "DESC"),
        )
        return _filter_fields(result, PROPOSAL_FIELDS)
    if name == "get_customer_proposals":
        result = await client.get_customer_proposals(
            socid=args["socid"],
            limit=args.get("limit", 10),
            status=args.get("status"),
            statuses=args.get("statuses"),
            year=args.get("year"),
            month=args.get("month"),
            include_draft=args.get("include_draft", False),
            include_validated=args.get("include_validated", False),
            include_signed=args.get("include_signed", False),
            include_refused=args.get("include_refused", False),
        )
        return _filter_fields(result, PROPOSAL_FIELDS)
    if name == "get_proposal_by_id":
        result = await client.get_proposal_by_id(args["proposal_id"])
        return _filter_fields(result, PROPOSAL_FIELDS)
    if name == "search_proposals":
        q = _escape_sqlfilter(args["query"])
        # Note: Only search by ref - searching by customer name requires JOIN not supported by API
        result = await client.search_proposals(
            f"(t.ref:like:'%{q}%')",
            args.get("limit", 20),
            sortorder=args.get("sortorder", "DESC"),
        )
        return _filter_fields(result, PROPOSAL_FIELDS)
    if name == "create_proposal":
        return await client.create_proposal(**args)
    if name == "update_proposal":
        pid = args.pop("proposal_id")
        return await client.update_proposal(pid, **args)
    if name == "append_proposal_note":
        return await client.append_proposal_note(
            proposal_id=args["proposal_id"],
            note=args["note"],
            note_type=args.get("note_type", "private"),
            add_timestamp=args.get("add_timestamp", True),
        )
    if name == "delete_proposal":
        return await client.delete_proposal(args["proposal_id"])
    if name == "add_proposal_line":
        pid = args.pop("proposal_id")
        return await client.add_proposal_line(pid, **args)
    if name == "update_proposal_line":
        pid, lid = args.pop("proposal_id"), args.pop("line_id")
        return await client.update_proposal_line(pid, lid, **args)
    if name == "delete_proposal_line":
        return await client.delete_proposal_line(args["proposal_id"], args["line_id"])
    if name == "validate_proposal":
        return await client.validate_proposal(args["proposal_id"])
    if name == "close_proposal":
        pid = args.pop("proposal_id")
        return await client.close_proposal(pid, args["status"], args.get("note", ""))
    if name == "set_proposal_to_draft":
        return await client.set_proposal_to_draft(args["proposal_id"])

    # Raw API
    if name == "dolibarr_raw_api":
        return await client.dolibarr_raw_api(**args)

    return {"error": f"Unknown tool: {name}"}


# =============================================================================
# SERVER STARTUP
# =============================================================================

@asynccontextmanager
async def test_api_connection(config: Config | None = None):
    """Test API connection."""
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


async def _run_stdio_server(_config: Config) -> None:
    """Run MCP server over STDIO."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="dolibarr-mcp",
                server_version="1.2.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def _build_http_app(session_manager: StreamableHTTPSessionManager, auth: Optional[APIKeyAuth] = None, auth_enabled: bool = True) -> Starlette:
    """Create HTTP app for StreamableHTTP transport with authentication."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from .auth.api_key import extract_bearer_token

    class AuthMiddleware(BaseHTTPMiddleware):
        """Middleware for API Key authentication."""

        async def dispatch(self, request, call_next):
            # Skip auth for health checks and OPTIONS
            if request.url.path in ["/health", "/healthz", "/ready"]:
                return await call_next(request)
            if request.method == "OPTIONS":
                return await call_next(request)
            # Skip auth if disabled
            if not auth_enabled:
                return await call_next(request)
            # Extract client IP
            client_ip = request.client.host if request.client else None
            # Check if IP is blocked
            if auth and client_ip and auth.is_blocked(client_ip):
                return JSONResponse({"error": "Access denied", "code": "IP_BLOCKED"}, status_code=403)
            # Extract and verify API key
            auth_header = request.headers.get("Authorization", "")
            api_key = extract_bearer_token(auth_header)
            if not api_key:
                return JSONResponse({
                    "error": "Missing API key",
                    "code": "AUTH_REQUIRED",
                    "hint": "Include 'Authorization: Bearer <your-api-key>' header"
                }, status_code=401)
            if auth and not auth.verify(api_key, client_ip):
                return JSONResponse({"error": "Invalid API key", "code": "AUTH_FAILED"}, status_code=401)
            return await call_next(request)

    class ASGIEndpoint:
        def __init__(self, handler):
            self.handler = handler
        async def __call__(self, scope: Scope, receive: Receive, send: Send):
            await self.handler(scope, receive, send)

    async def options_handler(request):
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
        })

    async def health_handler(request):
        """Health check endpoint (no auth required)."""
        return JSONResponse({
            "status": "healthy",
            "service": "dolibarr-mcp",
            "version": "2.1.0",
            "auth_enabled": auth_enabled,
        })

    async def lifespan(app):
        async with session_manager.run():
            yield

    async def asgi_handler(scope, receive, send):
        global _current_auth_token
        _current_auth_token = None
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                auth_header = value.decode("utf-8")
                if auth_header.startswith("Bearer "):
                    _current_auth_token = auth_header[7:]
                elif auth_header.startswith("Token "):
                    _current_auth_token = auth_header[6:]
                else:
                    _current_auth_token = auth_header
                break
        await session_manager.handle_request(scope, receive, send)

    app = Starlette(
        routes=[
            # Health check endpoints (no auth)
            Route("/health", health_handler, methods=["GET"]),
            Route("/healthz", health_handler, methods=["GET"]),
            Route("/ready", health_handler, methods=["GET"]),
            # MCP endpoints
            Route("/", ASGIEndpoint(asgi_handler), methods=["GET", "POST", "DELETE"]),
            Route("/{path:path}", ASGIEndpoint(asgi_handler), methods=["GET", "POST", "DELETE"]),
            # CORS preflight
            Route("/", options_handler, methods=["OPTIONS"]),
            Route("/{path:path}", options_handler, methods=["OPTIONS"]),
        ],
        lifespan=lifespan,
    )
    # Add authentication middleware
    if auth_enabled and auth:
        app.add_middleware(AuthMiddleware)
    # Add CORS middleware
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["Authorization", "Content-Type", "Accept"], allow_credentials=False)
    return app


async def _run_http_server(config: Config) -> None:
    """Run MCP server over HTTP with authentication."""
    # Determine if auth should be enabled
    auth_enabled = os.getenv("MCP_AUTH_ENABLED", "true").lower() == "true"

    # Create auth instance
    auth = APIKeyAuth() if auth_enabled else None

    # Warn if no keys configured
    if auth_enabled and auth and not auth._key_hashes:
        print("⚠️  Auth enabled but no API keys configured!", file=sys.stderr)
        print("   Set MCP_API_KEY or MCP_API_KEYS environment variable", file=sys.stderr)
        print("   Or disable auth with MCP_AUTH_ENABLED=false", file=sys.stderr)

    session_manager = StreamableHTTPSessionManager(server, json_response=False, stateless=False)
    app = _build_http_app(session_manager, auth=auth, auth_enabled=auth_enabled)

    auth_status = "🔐 Auth enabled" if auth_enabled else "⚠️  Auth disabled"
    print(f"🌐 HTTP server on {config.mcp_http_host}:{config.mcp_http_port} | {auth_status}", file=sys.stderr)

    uvicorn_config = uvicorn.Config(app, host=config.mcp_http_host, port=config.mcp_http_port, log_level=config.log_level.lower(), loop="asyncio", access_log=False)
    await uvicorn.Server(uvicorn_config).serve()


async def main():
    """Run the Dolibarr MCP server."""
    config = Config()
    async with test_api_connection(config) as ok:
        if not ok:
            print("⚠️ Starting without valid API", file=sys.stderr)
    print("🚀 Dolibarr MCP server ready", file=sys.stderr)
    if config.mcp_transport == "http":
        await _run_http_server(config)
    else:
        await _run_stdio_server(config)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped", file=sys.stderr)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
