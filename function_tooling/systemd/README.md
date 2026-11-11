# Usługi systemd

Szablony instalowane przez [../bootstrap.sh](../bootstrap.sh). Nie kopiuj ich ręcznie:
`@ZMIENNE@` podstawia skrypt (użytkownik, ścieżki), a w repo nie ma żadnych danych maszyny.

| Plik | Co robi | Zależności |
|---|---|---|
| `yt-mcp.service` | Serwer MCP (`main.py`, port 8001). Czyta `CLAUDE_KEY`, `PUBLIC_HOST`, `PORT`, `YT_MCP_DB` z `/etc/yt-mcp.env`. | po sieci |
| `yt-mcp-tunnel.service` | `cloudflared` wystawia port 8001 pod stałą domeną (host i tunel z `~/.cloudflared/config.yml`). | `Requires` + `After` = `yt-mcp.service` |

Obie: `Restart=always` (padnie, to wstanie) i `WantedBy=multi-user.target` (start po restarcie maszyny).

| Zmienna | Skąd |
|---|---|
| `@USER@` | użytkownik, który uruchomił `sudo ./bootstrap.sh` |
| `@APP_DIR@` | katalog repo |
| `@ENV_FILE@` | `/etc/yt-mcp.env` |
| `@CLOUDFLARED@`, `@CF_CONFIG@` | `~/.local/bin/cloudflared`, `~/.cloudflared/config.yml` |

Po zmianie szablonu uruchom `./bootstrap.sh` ponownie (jest idempotentny).
