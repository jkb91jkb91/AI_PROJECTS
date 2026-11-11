"""Entry point: starts the MCP server (streamable-http). Tools live in tools.py."""

import os

from mcp.server.transport_security import TransportSecuritySettings

from tools import mcp

PORT = int(os.environ.get("PORT", "8001"))


if __name__ == "__main__":
    allowed_hosts = [f"127.0.0.1:{PORT}", f"localhost:{PORT}"]
    public_host = os.environ.get("PUBLIC_HOST")
    if public_host:
        allowed_hosts.append(public_host)
    else:
        print(
            "\nUWAGA: zmienna PUBLIC_HOST nie jest ustawiona — serwer przyjmie "
            "żądania tylko z localhost. Jeśli używasz tunelu (cloudflared), "
            "uruchom ponownie z PUBLIC_HOST=<host-z-tunelu> (patrz README.md).\n"
        )

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
    )

    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=PORT,
        transport_security=transport_security,
    )
