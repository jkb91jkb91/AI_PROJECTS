"""YouTube access through yt-dlp: channel listings, video ids, subtitles (no database writes except
the channel url -> name pair learned while listing a channel)."""

import re
import time

import yt_dlp

import db

VTT_TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")
VTT_TAG_RE = re.compile(r"<[^>]+>")
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})")

CHANNEL_TABS = ("videos", "streams", "shorts")


def channel_tab_url(channel_url: str, tab: str | None = None) -> str:
    """Build the YouTube list URL: <channel url>/<tab>.

    `tab` is videos | streams | shorts. If it is None, a tab already present at the end of the URL is
    kept, and a bare channel URL means "videos". An explicit `tab` always wins over the URL's suffix.
    """
    url = channel_url.strip().rstrip("/")
    in_url = None
    for candidate in CHANNEL_TABS:
        if url.endswith(f"/{candidate}"):
            in_url = candidate
            url = url[: -(len(candidate) + 1)]
            break
    chosen = (tab or in_url or "videos").strip().strip("/").lower()
    if chosen not in CHANNEL_TABS:
        raise ValueError(f"tab must be one of {', '.join(CHANNEL_TABS)} (got {tab!r})")
    return f"{url}/{chosen}"


def channel_key(channel_url: str) -> str:
    """Stable identity of a channel address: no tab suffix, no trailing slash, lower case."""
    return channel_tab_url(channel_url, "videos").rsplit("/", 1)[0].lower()

def list_channel(channel_url: str, limit: int, tab: str | None = None) -> tuple[str | None, list[dict]]:
    """Return (channel name, flat video entries). limit <= 0 means the whole channel."""
    url = channel_tab_url(channel_url, tab)

    ydl_opts = {"extract_flat": True, "quiet": True, "skip_download": True}
    if limit > 0:
        ydl_opts["playlistend"] = limit

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    info = info or {}
    entries = [e for e in (info.get("entries") or []) if e]
    if limit > 0:
        entries = entries[:limit]
    channel_name = info.get("channel") or info.get("uploader") or info.get("title")
    db.remember_channel_url(channel_key(channel_url), channel_name)
    return channel_name, entries


def video_id(url: str) -> str | None:
    match = VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def subtitle_candidates(info: dict, preferred_lang: str) -> list[tuple[str, str]]:
    """List (language_code, subtitle_url) candidates in priority order.

    YouTube offers machine translations of auto-captions into many languages, and those are
    worse than the original. So the video's ORIGINAL language comes first (manual subs, then
    the original auto-captions); `preferred_lang` and English only come after that.
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    original = info.get("language")
    if not original:
        for key in auto:
            if key.endswith("-orig"):
                original = key[: -len("-orig")]
                break

    ordered: list[tuple[str, dict]] = []

    def add(source: dict, key: str) -> None:
        if key != "live_chat" and source.get(key):
            ordered.append((key, source[key]))

    if original:
        add(manual, original)
        add(auto, f"{original}-orig")
        add(auto, original)
    for lang in (preferred_lang, "en"):
        add(manual, lang)
        add(auto, lang)
    for source in (manual, auto):
        for key in source:
            add(source, key)

    candidates: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for lang_code, entries in ordered:
        url = pick_vtt_format(entries)
        if url and url not in seen_urls:
            seen_urls.add(url)
            candidates.append((lang_code, url))
    return candidates


def pick_vtt_format(entries: list[dict]) -> str | None:
    for entry in entries:
        if entry.get("ext") == "vtt":
            return entry.get("url")
    return entries[0].get("url") if entries else None


def clean_vtt(raw_vtt: str) -> str:
    """Strip VTT timestamps/metadata and collapse repeated caption lines into plain text."""
    lines = []
    previous = None
    for line in raw_vtt.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT":
            continue
        if VTT_TIMESTAMP_RE.search(line) or line.isdigit():
            continue
        if line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        line = VTT_TAG_RE.sub("", line)
        if line and line != previous:
            lines.append(line)
            previous = line
    return " ".join(lines)


def fetch_transcript(ydl: yt_dlp.YoutubeDL, info: dict, preferred_lang: str) -> tuple[str, str]:
    """Download and clean the best available subtitles. Returns (language_code, text).
    Raises RuntimeError when there are none or all downloads fail."""
    candidates = subtitle_candidates(info, preferred_lang)
    if not candidates:
        raise RuntimeError("No subtitles available")

    last_error = None
    for lang_code, subtitle_url in candidates:
        try:
            raw_vtt = ydl.urlopen(subtitle_url).read().decode("utf-8", errors="ignore")
            return lang_code, clean_vtt(raw_vtt)
        except Exception as exc:
            last_error = str(exc)
            time.sleep(2)
    raise RuntimeError(last_error or "Subtitle download failed")
