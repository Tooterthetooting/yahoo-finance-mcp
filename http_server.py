"""Run the Yahoo Finance MCP server over Streamable HTTP so it can be hosted online.

The original server.py only speaks "stdio" (for Claude Desktop on your own computer).
claude.ai custom connectors need a public HTTPS URL, so this file starts the same
tools as a small web server instead. It does not change server.py.

Settings (set them as environment variables on your hosting service):
  PORT          Port to listen on. Most hosts set this for you. Default 8000.
  SECRET_PATH   A long random string. The connector URL becomes
                https://<your-app>/<SECRET_PATH>/mcp so strangers can't guess it.
                Leave empty to serve at https://<your-app>/mcp (not recommended).
"""

import os

import uvicorn
from starlette.responses import PlainTextResponse

import valuation_tools
from server import yfinance_server

# Adds get_valuation_snapshot and makes get_stock_info fall back to it when
# Yahoo throttles the full info request (common on cloud hosts like Render).
valuation_tools.register()

port = int(os.environ.get("PORT", "8000"))
secret = os.environ.get("SECRET_PATH", "").strip("/")
mcp_path = f"/{secret}/mcp" if secret else "/mcp"

# stateless_http: each request stands alone, so restarts or sleeping hosts
# don't break sessions. host="0.0.0.0" lets the hosting service reach the app.
mcp_app = yfinance_server.streamable_http_app(
    streamable_http_path=mcp_path,
    stateless_http=True,
    host="0.0.0.0",
)


async def health(_request):
    return PlainTextResponse("ok")


# Add a /health page for the hosting service's health check.
mcp_app.add_route("/health", health)
app = mcp_app

if __name__ == "__main__":
    print(f"Yahoo Finance MCP listening on port {port}, path {'/<secret>/mcp' if secret else '/mcp'}")
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
