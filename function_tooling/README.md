# yt-mcp

Serwer MCP (Model Context Protocol) w Pythonie, który pozwala Claude'owi z claude.ai
**analizować całe kanały YouTube** (np. „co autor mówił o podatkach przez ostatnie 3 lata?”)
**bez wlewania transkrypcji do kontekstu rozmowy**.

Gotowe komendy do wklejenia w czacie: [CLAUDE_USECASES.md](CLAUDE_USECASES.md).

Spis treści:

1. [Problem, który to rozwiązuje](#1-problem-który-to-rozwiązuje)
2. [Schemat całości](#2-schemat-całości)
3. [Jak to działa krok po kroku](#3-jak-to-działa-krok-po-kroku)
4. [Pliki projektu i zależności między nimi](#4-pliki-projektu-i-zależności-między-nimi)
5. [Narzędzia MCP — którego kiedy użyć](#5-narzędzia-mcp--którego-kiedy-użyć)
6. [Baza SQLite](#6-baza-sqlite)
7. [Skąd bierze się język napisów](#7-skąd-bierze-się-język-napisów)
8. [Konfiguracja i koszty](#8-konfiguracja-i-koszty)
9. [Uruchomienie (serwer + tunel + claude.ai)](#9-uruchomienie-serwer--tunel--claudeai)
10. [Ograniczenia i rzeczy, o których trzeba wiedzieć](#10-ograniczenia-i-rzeczy-o-których-trzeba-wiedzieć)

---

## 1. Problem, który to rozwiązuje

Transkrypcja 30-minutowego filmu to ok. **8–12 tys. tokenów**. Gdyby serwer oddawał Claude'owi
napisy 20 filmów, do kontekstu wpadłoby ~200 tys. tokenów: okno się zapełnia, limity
subskrypcji znikają, a w nowym oknie czatu (Claude nic nie pamięta) wszystko trzeba pobierać od zera.

Rozwiązanie: **ciężką pracę robi serwer, a do Claude'a wraca tylko mały wynik.**

| | Bez bazy (stary sposób) | Z bazą i streszczeniami (ten projekt) |
|---|---|---|
| Tokeny w Claude na jedno pytanie | ~200 tys. (20 filmów) | ~5–10 tys. |
| Ile filmów da się objąć | ~20 | cały kanał (setki/tysiące) |
| Drugie pytanie o to samo | wszystko od nowa | od razu, z bazy |
| Nowe okno czatu | wszystko od nowa | dane zostają na serwerze |
| Zapytania do YouTube | przy każdym pytaniu | jednorazowo na film |

Analogia: bibliotekarz z 1000 książek. Raz robi **fiszkę** dla każdej książki (to robi tani model
Haiku). Gdy pytasz o temat, przynosi 10 pasujących fiszek, a nie wszystkie książki.
**SQLite to szafka z fiszkami.**

---

## 2. Schemat całości

```
 claude.ai (Ty + Claude)
      │
      │  1. Claude woła narzędzie MCP, np. search_summaries("podatki")
      ▼
 ┌──────────────────────────┐
 │ Tunel (cloudflared)      │   publiczny adres HTTPS  →  http://127.0.0.1:8001
 └────────────┬─────────────┘
              ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │  SERWER MCP  (main.py + tools.py, port 8001)                                           │
 │                                                                             │
 │   narzędzia (patrz sekcja 5)                                                │
 │   ┌───────────────────────┐         ┌──────────────────────────────────┐    │
 │   │ search_summaries      │────────►│                                  │    │
 │   │ get_video_summary     │ czyta   │   SQLite  (db.py → yt_mcp.db)    │    │
 │   │ list_indexed_channels │────────►│   • tabela videos (streszczenia) │    │
 │   │ index_status          │         │   • indeks FTS5 (wyszukiwanie)   │    │
 │   └───────────────────────┘         │                                  │    │
 │                                     └───────────────▲──────────────────┘    │
 │   ┌───────────────────────┐   kolejka  ┌────────────┴─────────────────┐     │
 │   │ index_channel         │──────────► │  WĄTEK W TLE (indexer)       │     │
 │   │ summarize_videos      │  zadań     │  jeden film po drugim:       │     │
 │   └───────────────────────┘            │  a) yt-dlp  → napisy         │     │
 │                                        │  b) Haiku   → streszczenie   │     │
 │                                        │  c) zapis do SQLite          │     │
 │                                        │  d) pauza 3–8 s (anty-blokada)│    │
 │                                        └───────┬───────────────┬──────┘     │
 └────────────────────────────────────────────────┼───────────────┼────────────┘
                                                  ▼               ▼
                                            ┌──────────┐   ┌───────────────────┐
                                            │ YouTube  │   │ API Anthropic     │
                                            │ (napisy) │   │ model Haiku 5.5   │
                                            └──────────┘   │ klucz: CLAUDE_KEY │
                                                           └───────────────────┘
```

Najważniejsze: **Claude nigdy nie widzi transkrypcji** (chyba że sam o nie poprosi pojedynczym
narzędziem `get_video_subtitles`). Widzi tylko krótkie streszczenia z bazy.

---

## 3. Jak to działa krok po kroku

### Faza A — indeksowanie (raz na film, w tle)

```
Ty:     "zaindeksuj kanał https://www.youtube.com/@Kanal, 50 ostatnich filmów"
Claude: index_channel(channel_url, limit=50)
          │
          ▼
   serwer pobiera listę 50 filmów (yt-dlp, tylko metadane)
   dla każdego filmu:
        ├─ jest już w bazie jako "done"?  → pomiń (zero kosztu)
        └─ nie → wpisz do bazy jako "pending" + dodaj do kolejki
          │
          ▼
   odpowiedź wraca OD RAZU:  {"queued": 50, "already_done": 0, ...}
   (Claude nie czeka na zakończenie)

   ── w tle, jeden film po drugim ─────────────────────────────────────────────
   1. yt-dlp pobiera napisy (patrz sekcja 7: wybór języka)
   2. tekst napisów → Haiku → { dokładne streszczenie, tematy, stanowiska autora }
   3. zapis do SQLite (status "done") + aktualizacja indeksu wyszukiwania
   4. pauza 3–8 s losowo (żeby YouTube nie zablokował IP)
   błąd (np. brak napisów)? → status "error" + opis, kolejny film jedzie dalej

Ty:     "jak idzie indeksowanie?"
Claude: index_status()  →  {"done": 31, "pending": 19, "error": 0, "queue_length": 19}
```

Jeden film to kilka–kilkanaście sekund (głównie pauza + wywołanie Haiku), więc 50 filmów to
kilka–kilkanaście minut.

### Faza B — zadawanie pytań (szybkie, w każdej rozmowie)

```
Ty:     "co autor mówił o podatkach przez ostatnie lata? pokaż jak zmieniał zdanie"
Claude: search_summaries(query="podatki", sort="date")
          │
          ▼
   serwer: zapytanie → indeks FTS5 w SQLite (milisekundy)
          │
          ▼
   zwraca ~10 krótkich rekordów (data, tytuł, link, streszczenie, stanowiska)
   ≈ 5–10 tys. tokenów, niezależnie od tego, czy w bazie jest 50 czy 5000 filmów
          │
          ▼
Claude: czyta streszczenia posortowane po dacie i układa odpowiedź.
        Potrzebuje szczegółów jednego filmu? → get_video_summary(video_id)
```

### Szybka ścieżka dla konkretnych filmów

`summarize_videos([url1, url2, ...])` — dodaje filmy do tej samej kolejki i **czeka do
`wait_seconds`** (domyślnie 60 s, max 120 s) na wyniki. Zwraca gotowe streszczenia, a to, co jeszcze
się liczy, zgłasza w polu `pending` (wtedy wystarczy zapytać ponownie lub użyć `index_status`).

---

## 4. Pliki projektu i zależności między nimi

```
 main.py ──► tools.py ──┬──► indexer.py ──┬──► youtube.py ──► YouTube (yt-dlp)
 (start serwera)        │   (wątek,       ├──► summarizer.py ──► API Anthropic (Haiku)
                        │    kolejka)     └──► db.py
                        ├──► youtube.py
                        └──► db.py            (SQLite: zapis, wyszukiwanie)
```

| Plik | Za co odpowiada |
|---|---|
| [main.py](main.py) | Punkt startu: konfiguracja serwera HTTP (port, `PUBLIC_HOST`) i uruchomienie. Nic więcej. |
| [tools.py](tools.py) | **Narzędzia MCP** (to, co wywołuje claude.ai). Opisy (docstringi) są instrukcją dla modelu. Tu zmieniasz zachowanie narzędzi. |
| [indexer.py](indexer.py) | Wątek w tle i kolejka: pobiera napisy, wywołuje Haiku, zapisuje wynik. Tu jest tempo pobierania (`THROTTLE_SECONDS`). |
| [youtube.py](youtube.py) | Dostęp do YouTube'a przez yt-dlp: lista filmów kanału, zakładki, identyfikatory, wybór i czyszczenie napisów. |
| [db.py](db.py) | Cała obsługa SQLite: schemat, zapis streszczeń, wyszukiwanie FTS5, liczniki postępu. Nic nie wie o YouTube ani o Claude. |
| [summarizer.py](summarizer.py) | Jedna funkcja `summarize(...)`: wysyła transkrypcję do Haiku i zwraca ustrukturyzowane streszczenie. Tu jest prompt i klucz API. |
| `yt_mcp.db` | Plik bazy (powstaje sam przy pierwszym użyciu, jest w `.gitignore`). |

Gdzie co zmieniać:
- inny prompt / długość streszczenia → `SYSTEM` i klasy w [summarizer.py](summarizer.py) (potem podbij `SUMMARY_VERSION` w [db.py](db.py), a stare filmy przestreszczą się przy następnym `index_channel`),
- tempo pobierania → `THROTTLE_SECONDS` w [indexer.py](indexer.py),
- opis i zachowanie narzędzia → jego docstring w [tools.py](tools.py),
- inny model → `MODEL` w [summarizer.py](summarizer.py).

---

## 5. Narzędzia MCP — którego kiedy użyć

Argument `tab` (`videos` / `streams` / `shorts`) wybiera rodzaj filmów: zwykłe, transmisje na żywo albo krótkie. `channel_url` zostaje czystym adresem kanału.

| Narzędzie | Do czego | Rozmiar odpowiedzi |
|---|---|---|
| `list_indexed_channels()` | **Zacznij od niego w nowej rozmowie** — pokazuje, jakie kanały są w bazie (liczba filmów, zakres dat). | mały |
| `sync_channel(channel_url, limit=20, tab="videos", wait_seconds=90, display_summaries_in_chat=true)` | **Narzędzie do powtarzalnych zadań i harmonogramu.** Jednym wywołaniem: sprawdza bazę, dociąga brakujące filmy spośród N najnowszych, czeka do `wait_seconds` i zwraca wynik z flagą `complete`. Idempotentne: przy `complete=false` wywołaj ponownie z tymi samymi argumentami. Zwraca też liczniki: `from_database` (z bazy, bez tokenów), `summarised_in_run` (streszczone teraz) i `haiku_tokens_in_run` (rzeczywiście rozliczone tokeny Haiku). | liczby, albo ~1 tys. tokenów na film |
| `index_channel(channel_url, limit=50, tab="videos")` | Zaindeksuj kanał w tle (`limit=0` = cały kanał). Pomija to, co już jest gotowe. | mały, wraca od razu |
| `index_status(channel?)` | Postęp: ile `done` / `pending` / `error`, ostatnie błędy oraz `haiku_tokens_total` (tokeny wydane łącznie na streszczenia). | mały |
| `search_summaries(query, channel?, limit=10, sort="relevance"\|"date")` | **Główne narzędzie do analizy tematu.** `sort="date"` do pytań o zmianę poglądów w czasie. | ~1 tys. tokenów na wynik (przy `limit=10` ok. 10 tys.) |
| `get_summary(channel_url, comment?)` | **Dane do jednego obszernego podsumowania całej bazy kanału.** Tylko baza, bez tokenów. Reguły podsumowania są w opisie narzędzia; `comment` = dodatkowe wymagania. | ~1 tys. tokenów / film w bazie |
| `recent_summaries(channel?, limit=20)` (`limit=0` = wszystkie; `channel` = nazwa lub URL kanału) | **Najnowsze streszczenia z bazy**, bez haseł i bez zapytań do YouTube'a. Do próśb typu „streść ostatnie 20 filmów”. | ~1 tys. tokenów / film |
| `get_video_summary(video)` | Pełne streszczenie jednego filmu (URL lub id). | mały |
| `summarize_videos(video_urls, wait_seconds=60)` | Streść konkretną listę filmów i od razu zwróć wyniki. | ~1 tys. tokenów / film |
| `list_channel_videos(channel_url, limit=20, tab="videos")` | Sama lista filmów (tytuł, data, czas), bez napisów. | mały |
| `get_video_subtitles(video_urls, lang="pl", max_chars=20000)` | **Surowy tekst** napisów. Drogi w tokenach — tylko gdy potrzebne dokładne sformułowania 1–2 filmów. Ma limit `max_chars` i flagę `truncated`. | do ~5 tys. tokenów / film |

---

## 6. Baza SQLite

SQLite to zwykły plik na dysku (`yt_mcp.db`, wbudowany w Pythona, bez osobnego serwera).
Przeżywa zamknięcie czatu, restart serwera i restart komputera.

### Tabela `videos` — jedna linia na film

| Kolumna | Znaczenie |
|---|---|
| `video_id` | 11-znakowy id z YouTube (klucz główny) |
| `url`, `channel`, `title`, `upload_date` | metadane (`upload_date` jako `YYYYMMDD`) |
| `language` | język napisów, z których powstało streszczenie |
| `status` | `pending` (w kolejce) → `done` (gotowe) lub `error` (np. brak napisów) |
| `summary` | **dokładne** streszczenie (ok. 1 zdanie na 1,5 min filmu, od 8 do 60 zdań), **zawsze po polsku**, niezależnie od języka filmu |
| `topics` | lista haseł tematycznych (JSON) |
| `stances` | lista „temat + stanowisko autora” (JSON) |
| `summary_version` | wersja promptu; po zmianie `SUMMARY_VERSION` stare filmy zostaną przestreszczone |
| `error` | treść błędu, jeśli `status = error` |
| `input_tokens`, `output_tokens` | tokeny Haiku rzeczywiście rozliczone za to streszczenie (z odpowiedzi API) |
| `summarised_at` | kiedy streszczenie powstało (UTC); na tej podstawie liczone jest „z bazy” vs „streszczone w tym przebiegu” |

### Tabela `channel_urls` — adres kanału → nazwa kanału

Przy każdym pobraniu listy filmów z YouTube'a (`sync_channel`, `index_channel`) serwer zapisuje parę
„adres kanału (bez zakładki, małymi literami) → nazwa kanału”. Dzięki temu narzędzia czytające tylko z bazy
(`recent_summaries`, `search_summaries`, `index_status`) przyjmują w `channel` albo nazwę, albo ten sam
adres URL co `sync_channel`. Adres, którego serwer jeszcze nie widział (np. kanał zindeksowany przed dodaniem tej tabeli), jest
rozpoznawany przy pierwszym użyciu jednym tanim zapytaniem do YouTube'a o jedną pozycję listy (bez napisów i
bez tokenów) i od tej pory zapamiętany.

### Wyszukiwanie (FTS5)

Obok tabeli jest indeks pełnotekstowy `videos_fts` (tytuł + streszczenie + tematy + stanowiska).
Zapytanie jest „wyrozumiałe”: każde słowo łączone przez OR, dopasowanie po początku słowa, ranking BM25.

Polskiej odmiany FTS5 nie zna, więc zastosowano **prosty trik**: słowa dłuższe niż 5 liter są
obcinane do pierwszych 5 liter i szukane jako przedrostek. Dzięki temu „podatkach”, „podatek”
i „podatku” trafiają w ten sam film (sprawdzone testem). To przybliżenie — przy rzadkich
odmianach lepiej podać kilka synonimów albo krótkie hasło. Streszczenia po polsku z tematami
w mianowniku (robi je Haiku) dodatkowo wyrównują słownictwo.

### Rozmiar

Dokładne streszczenie jednego filmu 30-minutowego to ok. 10 KB wraz z indeksem, czyli **1000 filmów ≈ 10 MB** (orientacyjnie).

### Obsługa bazy

```bash
sqlite3 yt_mcp.db "SELECT status, COUNT(*) FROM videos GROUP BY status"   # podgląd
cp yt_mcp.db backup.db                                                     # kopia zapasowa
rm yt_mcp.db*                                                              # reset (usuwa też pliki -wal/-shm)
```

Ścieżkę zmienisz zmienną `YT_MCP_DB`.

---

## 7. Skąd bierze się język napisów

YouTube oferuje dla filmu napisy ręczne (jeśli autor je dodał) oraz automatyczne — a do tego
**maszynowe tłumaczenia napisów automatycznych na dziesiątki języków**, które są gorsze niż oryginał.
Dlatego kolejność wyboru to:

```
1. napisy ręczne w ORYGINALNYM języku filmu
2. napisy automatyczne w oryginalnym języku (klucz "xx-orig")
3. pl, potem en (ręczne, potem automatyczne — mogą być tłumaczeniem maszynowym)
4. cokolwiek innego, co jest dostępne
   (pomijany jest "live_chat" — to czat z transmisji, nie napisy)
```

Film po niemiecku, japońsku czy hiszpańsku jest więc streszczany z **oryginału**, a streszczenie i tak
trafia do bazy po polsku. Brak jakichkolwiek napisów = status `error` („No subtitles available”).
Na takie filmy nie ma obejścia bez transkrypcji z dźwięku (np. Whisper), której tu nie ma.

---

## 8. Konfiguracja i koszty

### Zmienne środowiskowe

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `CLAUDE_KEY` | — (wymagana do streszczeń) | Klucz API Anthropic. Awaryjnie czytany jest też `ANTHROPIC_API_KEY`. Musi być widoczna **w terminalu, w którym uruchamiasz `main.py`**. |
| `PUBLIC_HOST` | — | Host tunelu, np. `abc.trycloudflare.com` (sekcja 9). |
| `PORT` | `8001` | Port serwera. |
| `YT_MCP_DB` | `./yt_mcp.db` | Ścieżka do pliku bazy. |

Brak klucza nie wyłącza serwera — wyszukiwanie w już zapisanych streszczeniach działa. Tylko
streszczanie nowych filmów zakończy się czytelnym błędem w polu `error`.

### Koszt streszczania (model Haiku 5.5)

Orientacyjnie (ceny z chwili pisania: $0.10 / 1M tokenów wejścia, $0.50 / 1M wyjścia):
film 30 min ≈ 10 tys. tokenów wejścia + ok. 1–3 tys. wyjścia (streszczenie plus rozumowanie modelu na
poziomie `medium`) ≈ **ok. 0,2–0,4 centa**; 500 filmów to rzędu **jednego–dwóch dolarów**. Płaci się raz na film —
kolejne pytania z bazy nic nie kosztują. To liczby szacunkowe: sprawdź realne zużycie w Anthropic Console
po pierwszym indeksowaniu.
Transkrypcje dłuższe niż 300 tys. znaków (wielogodzinne filmy) są obcinane do początku, a streszczenie
dostaje o tym informację.

---

## 9. Uruchomienie (serwer + tunel + claude.ai)

Serwer i tunel internetowy to **dwa osobne procesy**. Trzymaj je w dwóch różnych
terminalach — dzięki temu możesz restartować kod (`main.py`) bez psucia adresu URL,
który masz już wklejony w claude.ai.

### Wymagania (jednorazowo)

- Python + [`uv`](https://docs.astral.sh/uv/) — do zarządzania zależnościami (`uv sync`).
- `cloudflared` w `~/.local/bin/cloudflared` (bez potrzeby `sudo`):

  ```bash
  mkdir -p ~/.local/bin
  curl -sL -o ~/.local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x ~/.local/bin/cloudflared
  ```

- Zależności Pythona: `mcp[cli]`, `yt-dlp[default,curl-cffi]`, `anthropic` (wszystkie w `pyproject.toml`).
- Klucz API w zmiennej `CLAUDE_KEY`.

### Terminal 1 — tunel (uruchamiasz raz, zostawiasz włączony)

```bash
cloudflared tunnel --url http://127.0.0.1:8001
```

W logu pojawi się coś takiego:

```
Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):
https://jakis-losowy-adres.trycloudflare.com
```

**Skopiuj samą nazwę hosta** (bez `https://`), np. `jakis-losowy-adres.trycloudflare.com` —
będzie potrzebna w Terminalu 2. Zostaw to okno otwarte — jeśli zamkniesz ten proces,
adres przestaje działać i trzeba wygenerować nowy (i zaktualizować connector w claude.ai).

### Terminal 2 — serwer MCP (ten będziesz restartować przy każdej zmianie kodu)

```bash
PUBLIC_HOST=jakis-losowy-adres.trycloudflare.com uv run python main.py
```

Podmień `jakis-losowy-adres.trycloudflare.com` na host, który dostałeś z Terminala 1.

**Po co w ogóle `PUBLIC_HOST`, skoro `cloudflared` i tak wie na jaki port kierować ruch?**
To dwie zupełnie osobne sprawy. `cloudflared --url` odpowiada tylko za *routing* —
"dokąd fizycznie wysłać żądanie". `PUBLIC_HOST` to coś innego: nasz kod MCP ma wbudowaną
ochronę przed atakami DNS rebinding, która sama sprawdza nagłówek `Host` w każdym
przychodzącym żądaniu i domyślnie ufa tylko `localhost`/`127.0.0.1`. Kiedy żądanie leci
przez tunel, nagłówek `Host` zawiera publiczną nazwę z `trycloudflare.com` — więc trzeba
jawnie powiedzieć naszemu kodowi "zaufaj też temu konkretnemu hostowi, to nie atak, to
legalny tunel". Stąd `PUBLIC_HOST` — to autoryzacja wewnątrz Pythona, zero związku z tym,
jak `cloudflared` kieruje ruch. Bez niego tunel zwraca **421 Misdirected Request**.

### Rejestracja w claude.ai (raz, chyba że zmieni się adres tunelu)

1. claude.ai → Settings → Connectors → **Add custom connector**.
2. URL: `https://jakis-losowy-adres.trycloudflare.com/mcp` (z `/mcp` na końcu! bez tego jest 404).
3. Zatwierdź bez autentykacji.

W nowej rozmowie warto zacząć od prośby „sprawdź `list_indexed_channels`”, żeby Claude wiedział, co jest w bazie.

### Co zrobić po zmianie kodu?

**Tunel (Terminal 1) zostaw włączony — nic z nim nie rób.** On nie wie nic o Pythonie,
pilnuje tylko portu 8001. W Terminalu 2: `Ctrl+C`, a potem **ta sama komenda co wcześniej**
(z tym samym `PUBLIC_HOST`). Adres w claude.ai się nie zmienia. Baza SQLite zostaje (to plik).

### Kiedy trzeba zaktualizować URL w claude.ai?

Tylko jeśli **proces z Terminala 1 (cloudflared) umrze lub zostanie zrestartowany** — wtedy
Cloudflare wygeneruje nowy, losowy adres i trzeba:
1. Zaktualizować `PUBLIC_HOST` w komendzie z Terminala 2 na nowy host.
2. Podmienić URL w Settings → Connectors w claude.ai na nowy (z `/mcp` na końcu).

Chcesz stały adres (np. na Raspberry Pi, który po zaniku prądu ma wstać sam)? Quick tunnel tego
nie umie. Potrzebny jest nazwany tunel Cloudflare (wymaga własnej domeny w Cloudflare) albo np. Tailscale Funnel.

### Szybki test bez Claude (sanity check)

```bash
curl -s -i -X POST https://jakis-losowy-adres.trycloudflare.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
```

Oczekiwana odpowiedź: `HTTP/1.1 200` i w treści `"serverInfo":{"name":"yt-mcp",...}`.

---

## 10. Ograniczenia i rzeczy, o których trzeba wiedzieć

- **Bezpieczeństwo: publiczny adres bez logowania.** Każdy, kto zna URL tunelu, może wywołać
  `index_channel` / `summarize_videos` i **zużywać Twój kredyt API** (Haiku) oraz obciążać Twoje IP
  zapytaniami do YouTube. Nie publikuj adresu. Jeśli serwer ma działać na stałe, dodaj uwierzytelnianie
  (token w nagłówku / Cloudflare Access) albo wystaw tylko narzędzia odczytu, a indeksowanie
  uruchamiaj lokalnie.
- **Blokada IP przez YouTube.** Z domowego IP zwykle działa, z serwerów w chmurze (Hetzner, AWS…)
  YouTube często blokuje pobieranie. Stąd pauza 3–8 s między filmami; nie zwiększaj równoległości.
  Przy blokadzie film dostaje status `error` (widać w `index_status`), a indeksowanie idzie dalej.
- **Kolejka jest w pamięci.** Po restarcie serwera zadania „w toku” przepadają (wiersze `pending`
  zostają w bazie). Wystarczy ponownie wywołać `index_channel` — gotowe filmy zostaną pominięte,
  a reszta (w tym te z błędem) trafi do kolejki od nowa.
- **Brak napisów = brak streszczenia.** Filmy bez żadnych napisów kończą jako `error`.
- **Wyszukiwanie to słowa kluczowe, nie znaczenie.** Odmiana jest łapana prostym trikiem (sekcja 6);
  zapytanie opisowe w stylu „filmy, w których autor żałuje decyzji” zadziała słabiej niż konkretne hasła.
  Wyszukiwanie semantyczne (wektory) to możliwa przyszła rozbudowa.
- **Dokładność streszczeń ma granice.** Długość rośnie z długością filmu (ok. 1 zdanie na 1,5 min, maksymalnie
  60 zdań), więc w bardzo długich filmach na minutę przypada mniej szczegółów. Dostrajanie:
  edytuj `SYSTEM` i `target_sentences` w [summarizer.py](summarizer.py), zwiększ `SUMMARY_VERSION` w
  [db.py](db.py) i zrestartuj serwer. Stare filmy przestreszczą się przy następnym `sync_channel`, ale wymaga to
  ponownego pobrania napisów, więc zmieniaj to najpierw na kilku filmach.
- **Streszczenie nie zastąpi transkrypcji.** Do pytań „jak dokładnie to powiedział” służy
  `get_video_subtitles` (z limitem znaków).
- **Stan testów.** Kod bazy, wyszukiwania, wyboru języka napisów, kolejki i narzędzi MCP został
  sprawdzony lokalnie z podstawionymi YouTube i Haiku. **Prawdziwe wywołanie Haiku i pobranie napisów z YouTube
  trzeba jeszcze potwierdzić u siebie**, np.: `summarize_videos` na jednym filmie, a potem `get_video_summary`.
