# BOOTSTRAP — postawienie yt-mcp od zera (np. nowe Raspberry Pi)

Domena jest już zarejestrowana, a tunel Cloudflare już istnieje. Nic nie rejestrujesz
i nie tworzysz od nowa. Skrypt tylko instaluje usługi systemd.

## Na maszynie, na której już działa (wystarczy raz)

Tunel i domena nie są w repo. Skopiuj poświadczenia na nową maszynę:

```bash
scp -r ~/.cloudflared  uzytkownik@nowa-maszyna:~/
```

(opcjonalnie, żeby nie tracić streszczeń: `scp yt_mcp.db uzytkownik@nowa-maszyna:~/function_tooling/`)

## Na nowej maszynie

```bash
sudo apt update && sudo apt install -y git curl     # na Raspberry Pi OS Lite git bywa nieobecny
git clone <adres-repo> function_tooling && cd function_tooling
export CLAUDE_KEY=<klucz-API-Anthropic>              # tylko do pierwszego uruchomienia
bash ./bootstrap.sh
```

Gotowe. Skrypt sam zainstaluje `uv`, Pythona, zależności i `cloudflared`, utworzy
`/etc/yt-mcp.env` (z kluczem, chmod 600) i włączy dwie usługi: `yt-mcp` oraz
`yt-mcp-tunnel`. Wstają same po każdym restarcie. Na końcu wypisze URL do claude.ai
(`https://<host>/mcp`).

## Sprawdzenie

```bash
systemctl status yt-mcp yt-mcp-tunnel
journalctl -u yt-mcp -u yt-mcp-tunnel -f
```

## Wymagania, których skrypt nie załatwi

| Co | Dlaczego |
|---|---|
| Użytkownik z `sudo` | zapis do `/etc/systemd` |
| Dostęp do internetu | pobranie `uv`, Pythona, `cloudflared` |
| Skopiowane `~/.cloudflared/` | tunel jest powiązany z kontem Cloudflare; bez tego skrypt przerwie i wypisze, co zrobić |
| `CLAUDE_KEY` w powłoce | bez niego skrypt przerwie (tylko przy pierwszym uruchomieniu) |
| Domowe IP | YouTube często blokuje serwery w chmurze (patrz README, sekcja 10) |

## Zmiana klucza lub kodu później

```bash
sudo nano /etc/yt-mcp.env          # nowy CLAUDE_KEY
sudo systemctl restart yt-mcp      # po zmianie klucza lub kodu (po git pull)
```
