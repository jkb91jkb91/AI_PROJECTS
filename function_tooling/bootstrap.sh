#!/usr/bin/env bash
# Jednorazowa instalacja yt-mcp jako usług systemd. Uruchom:  ./bootstrap.sh
# Po tym serwer i tunel wstają same po restarcie komputera / zaniku prądu.
#
# ┌─ WAŻNE: DOMENA I TUNEL SĄ JUŻ ZAŁOŻONE ─────────────────────────────────────┐
# │ Domena jest zarejestrowana, a nazwany tunel Cloudflare już istnieje.        │
# │ Host i tunel czytamy z ~/.cloudflared/config.yml — nic nie jest wpisane     │
# │ w tym skrypcie (domena ma zostać tajna). NIE rejestruj domeny ani nie       │
# │ twórz tunelu ponownie; skrypt tylko opakowuje to w systemd.                 │
# │ URL w claude.ai: https://<host-z-config.yml>/mcp (skrypt go wypisze).       │
# └─────────────────────────────────────────────────────────────────────────────┘
#
# Dwie usługi (druga zależy od pierwszej):
#   yt-mcp.service          serwer MCP (python main.py, port 8001)
#   yt-mcp-tunnel.service   cloudflared; Requires+After=yt-mcp.service
# Opis i szablony: systemd/ (README.md)
#
# Sekrety (CLAUDE_KEY) leżą w /etc/yt-mcp.env (chmod 600), nie w plikach usług.

set -euo pipefail

ENV_FILE="/etc/yt-mcp.env"

# Wymagamy roota do zapisu w /etc/systemd, ale usługi mają chodzić jako zwykły użytkownik.
if [[ $EUID -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

RUN_USER="${SUDO_USER:-$(logname)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
CLOUDFLARED="$RUN_HOME/.local/bin/cloudflared"
UV="$RUN_HOME/.local/bin/uv"

# --- Wymagania na świeżej maszynie (po sklonowaniu repo) ----------------------
# Instalujemy jako zwykły użytkownik, nie jako root, do ~/.local/bin.
as_user() { sudo -u "$RUN_USER" -H bash -c "$1"; }

if [[ ! -x "$UV" ]]; then
  echo "==> Instaluję uv (brak $UV)"
  as_user 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi
[[ -x "$UV" ]] || { echo "Instalacja uv nie powiodła się"; exit 1; }

if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "==> Pobieram cloudflared (brak $CLOUDFLARED)"
  case "$(uname -m)" in
    x86_64) CF_ARCH=amd64 ;; aarch64|arm64) CF_ARCH=arm64 ;; armv7l) CF_ARCH=arm ;;
    *) echo "Nieznana architektura $(uname -m)"; exit 1 ;;
  esac
  as_user "mkdir -p '$RUN_HOME/.local/bin' && curl -fsSL -o '$CLOUDFLARED' \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH \
    && chmod +x '$CLOUDFLARED'"
fi

# Tunelu i domeny NIE da się odtworzyć automatycznie — są poza repo (konto Cloudflare).
CF_CONFIG="$RUN_HOME/.cloudflared/config.yml"
if [[ ! -f "$CF_CONFIG" ]]; then
  cat <<MSG
Brak $CF_CONFIG — na tej maszynie nie ma jeszcze skonfigurowanego tunelu.
Domena jest już zarejestrowana, więc wystarczy (jako $RUN_USER, jednorazowo):
  1. $CLOUDFLARED tunnel login                      # logowanie do Cloudflare
  2. $CLOUDFLARED tunnel create <nazwa-tunelu>      # albo skopiuj ~/.cloudflared/ ze starej maszyny
  3. $CLOUDFLARED tunnel route dns <nazwa-tunelu> <host.twojadomena>
  4. utwórz $CF_CONFIG:
       tunnel: <ID-tunelu>
       credentials-file: $RUN_HOME/.cloudflared/<ID-tunelu>.json
       ingress:
         - hostname: <host.twojadomena>
           service: http://127.0.0.1:8001
         - service: http_status:404
Potem uruchom ./bootstrap.sh ponownie.
MSG
  exit 1
fi
# Host = pierwszy "hostname:" z ingress w configu tunelu (tam jest jedyne źródło prawdy).
PUBLIC_HOST="$(awk '/^[[:space:]]*-?[[:space:]]*hostname:/ {print $NF; exit}' "$CF_CONFIG")"
[[ -n "$PUBLIC_HOST" ]] || { echo "Nie znalazłem hostname w $CF_CONFIG"; exit 1; }

echo "==> Zależności Pythona (uv sync) jako $RUN_USER"
sudo -u "$RUN_USER" bash -c "cd '$APP_DIR' && '$UV' sync"

echo "==> Plik środowiska $ENV_FILE"
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -z "${CLAUDE_KEY:-}" ]]; then
    echo "Ustaw CLAUDE_KEY w tej powłoce i uruchom ponownie (sudo -E zachowa zmienną)."
    exit 1
  fi
  install -m 600 /dev/null "$ENV_FILE"
  {
    echo "CLAUDE_KEY=$CLAUDE_KEY"
    echo "PUBLIC_HOST=$PUBLIC_HOST"
    echo "PORT=8001"
    echo "YT_MCP_DB=$APP_DIR/yt_mcp.db"
  } > "$ENV_FILE"
else
  echo "    istnieje — zostawiam bez zmian"
fi

# Jednostki leżą w repo (systemd/*.service) jako szablony z @ZMIENNYMI@.
for unit in yt-mcp.service yt-mcp-tunnel.service; do
  echo "==> /etc/systemd/system/$unit"
  sed -e "s|@USER@|$RUN_USER|g" \
      -e "s|@APP_DIR@|$APP_DIR|g" \
      -e "s|@ENV_FILE@|$ENV_FILE|g" \
      -e "s|@CLOUDFLARED@|$CLOUDFLARED|g" \
      -e "s|@CF_CONFIG@|$CF_CONFIG|g" \
      "$APP_DIR/systemd/$unit" > "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable --now yt-mcp.service yt-mcp-tunnel.service

echo
echo "Gotowe. Status:   systemctl status yt-mcp yt-mcp-tunnel"
echo "Logi:             journalctl -u yt-mcp -u yt-mcp-tunnel -f"
echo "Po zmianie kodu:  sudo systemctl restart yt-mcp"
echo "Connector w claude.ai: https://$PUBLIC_HOST/mcp"
