# Dolibarr MCP Server

Dolibarr MCP delivers a Model Context Protocol (MCP) interface for the Dolibarr
ERP/CRM. The project mirrors the project structure of [`prestashop-mcp`](https://github.com/latinogino/prestashop-mcp):
an async API client, a production-ready STDIO server, and focused
documentation.

**Design Note:** While sharing the same architecture, this server implements **specialized search tools** (e.g., `search_products_by_ref`, `resolve_product_ref`) instead of a single unified `get_` tool. This design choice ensures efficient server-side filtering via Dolibarr's SQL API, preventing the agent from accidentally loading thousands of records and exceeding context limits.

Claude Desktop and other MCP-aware tools can use the server to
manage customers, products, invoices, orders, and contacts in a Dolibarr
instance.

Consult the bundled [documentation index](docs/README.md) for deep dives into
configuration, API coverage, and contributor workflows.

## ✨ Features

- **Full ERP coverage** – CRUD tools for users, customers, products, invoices,
  orders, contacts, projects, and raw API access.
- **Advanced Search** – Server-side filtering for products, customers, and projects to minimize token usage and costs.
- **Async/await HTTP client** – Efficient Dolibarr API wrapper with structured
  error handling.
- **Ready for MCP hosts** – STDIO transport compatible with Claude Desktop out
  of the box.
- **Multi-tenancy support** – HTTP transport extracts `Authorization` header to authenticate each user with their own Dolibarr instance.
- **Shared workflow with prestashop-mcp** – Identical developer ergonomics and
  documentation structure across both repositories.

## ✅ Prerequisites

- Python 3.8 or newer.
- Access to a Dolibarr installation with the REST API enabled and a personal API
  token.

## 📦 Installation

### Linux / macOS

```bash
git clone https://github.com/latinogino/dolibarr-mcp.git
cd dolibarr-mcp
python3 -m venv venv_dolibarr
source venv_dolibarr/bin/activate
pip install -e .
# Optional development extras
pip install -e '.[dev]'
```

While the virtual environment is active record the Python executable path with
`which python`. Claude Desktop must launch the MCP server using this interpreter.

### Windows (PowerShell)

```powershell
git clone https://github.com/latinogino/dolibarr-mcp.git
Set-Location dolibarr-mcp
py -3 -m venv venv_dolibarr
./venv_dolibarr/Scripts/Activate.ps1
pip install -e .
# Optional development extras (escape brackets in PowerShell)
pip install -e .`[dev`]
```

Run `Get-Command python` (or `Get-Command python.exe`) while the environment is
activated and note the absolute path. Claude Desktop should use this interpreter
inside the virtual environment, for example
`C:\\path\\to\\dolibarr-mcp\\venv_dolibarr\\Scripts\\python.exe`.

### Docker (optional)

```bash
docker compose up -d
# or
docker build -t dolibarr-mcp .
docker run -d \
  -e DOLIBARR_URL=https://your-dolibarr.example.com/api/index.php \
  -e DOLIBARR_API_KEY=YOUR_API_KEY \
  dolibarr-mcp
```

## ⚙️ Configuration

### Environment variables

The server reads configuration from the environment or a `.env` file. Both
`DOLIBARR_URL` and `DOLIBARR_SHOP_URL` are accepted for the base API address.

| Variable | Description |
| --- | --- |
| `DOLIBARR_URL` / `DOLIBARR_SHOP_URL` | Base Dolibarr API endpoint, e.g. `https://example.com/api/index.php`. Trailing slashes are handled automatically. |
| `DOLIBARR_API_KEY` | Personal Dolibarr API token. |
| `LOG_LEVEL` | Optional logging verbosity (`INFO`, `DEBUG`, `WARNING`, …). |
| `MCP_TRANSPORT` | Transport to use: `stdio` (default) or `http` for streamable HTTP. |
| `MCP_HTTP_HOST` | Host/interface to bind when using HTTP transport (default `0.0.0.0`). |
| `MCP_HTTP_PORT` | Port to bind when using HTTP transport (default `8080`). |
| `MCP_AUTH_ENABLED` | Enable API key authentication for HTTP transport (default `true`). Set to `false` when using multi-tenancy with user-provided tokens. |
| `CACHE_ENABLED` | Enable Redis/DragonflyDB caching (default `true`). Disable for stricter multi-tenancy isolation. |

Example `.env`:

```env
DOLIBARR_URL=https://your-dolibarr.example.com/api/index.php
DOLIBARR_API_KEY=YOUR_API_KEY
LOG_LEVEL=INFO
```

### Claude Desktop configuration

Add the following block to `claude_desktop_config.json`, replacing the paths and
credentials with your own values:

```json
{
  "mcpServers": {
    "dolibarr": {
      "command": "C:\\path\\to\\dolibarr-mcp\\venv_dolibarr\\Scripts\\python.exe",
      "args": ["-m", "dolibarr_mcp.dolibarr_mcp_server"],
      "cwd": "C:\\path\\to\\dolibarr-mcp",
      "env": {
        "DOLIBARR_SHOP_URL": "https://your-dolibarr.example.com",
        "DOLIBARR_API_KEY": "YOUR_API_KEY"
      }
    }
  }
}
```

Restart Claude Desktop after saving the configuration. The MCP server reads the
same environment variables when launched from Linux or macOS hosts.

## ▶️ Usage

### Start the MCP server

The server communicates over STDIO by default, so run it in the foreground from
the virtual environment:

```bash
python -m dolibarr_mcp.dolibarr_mcp_server
```

Logs are written to stderr to avoid interfering with the MCP protocol. Keep the
process running while Claude Desktop is active.

### HTTP streaming mode (for Open WebUI or remote MCP clients)

Enable the HTTP transport by setting `MCP_TRANSPORT=http` (and optionally
`MCP_HTTP_HOST` / `MCP_HTTP_PORT`). This keeps the server running without STDIO
and exposes the Streamable HTTP transport compatible with Open WebUI:

```bash
MCP_TRANSPORT=http MCP_HTTP_PORT=8080 python -m dolibarr_mcp.dolibarr_mcp_server
```

Then point Open WebUI’s MCP configuration at `http://<host>:8080/`. The MCP
protocol headers (including `mcp-protocol-version`) are handled automatically by
Open WebUI’s MCP client.

### Multi-tenancy (HTTP transport only)

When using the HTTP transport, each user can authenticate with their own Dolibarr instance by passing their API key in the `Authorization` header. This allows a single MCP server deployment to serve multiple Dolibarr users.

```bash
curl -X POST http://localhost:8080/ \
  -H "Authorization: Bearer your-dolibarr-api-key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", ...}'
```

Supported authorization formats:
- `Authorization: Bearer <api-key>`
- `Authorization: Token <api-key>`

If no `Authorization` header is provided, the server falls back to `DOLIBARR_API_KEY` from the environment for backward compatibility.

**Caching:** When Redis/DragonflyDB caching is enabled, cache keys include the auth token to ensure users don't see each other's cached data. To disable caching entirely in multi-tenant deployments, set `CACHE_ENABLED=false`.

### Test the Dolibarr credentials

Use the standalone connectivity check before wiring the server into an MCP host:

```bash
python -m dolibarr_mcp.test_connection --url https://your-dolibarr.example.com/api/index.php --api-key YOUR_API_KEY
```

When the environment variables are already set, omit the overrides and run
`python -m dolibarr_mcp.test_connection`.

## 🧪 Development

- Run the test-suite with `pytest` (see [`docs/development.md`](docs/development.md)
  for coverage options and Docker helpers).
- Editable installs rely on the `src/` layout and expose the `dolibarr-mcp`
  console entry point for backwards compatibility.
- The repository structure, tooling, and docs intentionally mirror
  [`prestashop-mcp`](https://github.com/latinogino/prestashop-mcp) to keep the
  companion projects aligned.

## 📄 License

Released under the [MIT License](LICENSE).
