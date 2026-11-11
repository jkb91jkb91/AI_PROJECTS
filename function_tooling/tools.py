"""The MCP tools (what claude.ai calls). The docstrings are the instructions the model sees."""

import json

import yt_dlp
from mcp.server.mcpserver import MCPServer

import db
import indexer
import youtube

mcp = MCPServer("yt-mcp")


def resolve_channel(channel: str | None) -> tuple[str | None, dict | None]:
    """Accept either a channel name fragment or a channel URL. Returns (name, error).

    A URL the database has not seen yet (e.g. a channel indexed before addresses were recorded) is
    learned on the spot with ONE cheap YouTube request for a single list entry - no subtitles, no
    summaries, no tokens. After that the URL is known and the lookup is purely local.
    """
    if channel and channel.strip().lower().startswith(("http://", "https://")):
        key = youtube.channel_key(channel)
        name = db.channel_for_url(key)
        if name is None:
            for tab in ("videos", "streams", "shorts"):
                try:
                    name, _ = youtube.list_channel(channel, 1, tab)  # records the url -> name pair
                except Exception:
                    name = None
                if name:
                    break
        if not name:
            return None, {"error": f"Could not determine which stored channel {channel} is "
                                   "(YouTube unreachable or wrong address). Pass the channel name instead."}
        return name, None
    return channel, None


# ----------------------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------------------

def fmt_date(yyyymmdd: str | None) -> str | None:
    if yyyymmdd and len(yyyymmdd) == 8:
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return yyyymmdd


def format_row(row) -> dict:
    return {
        "video_id": row["video_id"],
        "title": row["title"],
        "date": fmt_date(row["upload_date"]),
        "channel": row["channel"],
        "url": row["url"],
        "language": row["language"],
        "summary": row["summary"],
        "topics": json.loads(row["topics"] or "[]"),
        "stances": json.loads(row["stances"] or "[]"),
    }


@mcp.tool()
def list_channel_videos(channel_url: str, limit: int = 20, tab: str | None = None) -> list[dict]:
    """List the most recent videos published on a YouTube channel (metadata only, no transcripts).

    Args:
        channel_url: URL of the channel, e.g. https://www.youtube.com/@SomeChannel
        limit: Maximum number of videos to return (default 20).
        tab: Which kind of videos: "videos" (regular uploads, default), "streams" (live stream recordings)
            or "shorts". Leave channel_url as the plain channel address; this argument picks the list.
    """
    _, entries = youtube.list_channel(channel_url, limit, tab)
    videos = []
    for entry in entries:
        video_id = entry.get("id")
        videos.append(
            {
                "title": entry.get("title"),
                "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else entry.get("url"),
                "duration": entry.get("duration"),
                "upload_date": entry.get("upload_date"),
            }
        )
    return videos


@mcp.tool()
def index_channel(channel_url: str, limit: int = 50, tab: str | None = None) -> dict:
    """Start summarising the videos of a YouTube channel in the background (server side).

    Each video's subtitles are downloaded and summarised by a small model, and the summary is
    stored in a local database. Videos that are already summarised are skipped. Returns
    immediately - use index_status to follow progress, then search_summaries to query.

    Args:
        channel_url: URL of the channel, e.g. https://www.youtube.com/@SomeChannel
        limit: How many of the most recent videos to index (default 50; 0 = the whole channel).
        tab: "videos" (regular uploads, default), "streams" (live stream recordings) or "shorts".
    """
    channel_name, entries = youtube.list_channel(channel_url, limit, tab)
    counts = {"queued": 0, "already_done": 0, "in_queue": 0}
    for entry in entries:
        video_id = entry.get("id")
        if not video_id:
            continue
        url = f"https://www.youtube.com/watch?v={video_id}"
        counts[indexer.enqueue(video_id, url, channel_name, entry.get("title"))] += 1
    return {
        "channel": channel_name,
        "videos_found": len(entries),
        **counts,
        "note": "Summaries are produced in the background (a few seconds per video). "
        "Call index_status(channel) to check progress.",
    }


@mcp.tool()
def sync_channel(
    channel_url: str,
    limit: int = 20,
    tab: str | None = None,
    wait_seconds: int = 90,
    display_summaries_in_chat: bool = True,
) -> dict:
    """Make sure the N newest videos of a channel are summarised, then return them. REPEATABLE.

    One call does everything: lists the N newest videos on YouTube, skips those already summarised
    in the database, queues the missing ones, waits up to wait_seconds for them and returns the result.
    It is idempotent - calling it again with the same arguments only processes what is still missing.

    RULE: if the result has "complete": false, call this tool again with exactly the same arguments
    until "complete" is true (each call makes progress, roughly 5-8 new videos per call).
    Videos without subtitles are reported under "failed" and are not retried.

    TOKEN ACCOUNTING (one "run" = all calls with the same arguments until complete is true; the numbers
    are cumulative over the run, so the last call - the one with complete=true - holds the totals):
      - from_database: videos that were already summarised BEFORE the run started (read from the
        database, no tokens spent)
      - summarised_in_run: videos summarised during this run (the summary model was used)
      - haiku_tokens_in_run: real billed tokens of the summary model for those videos
        ({"input": ..., "output": ...}; output includes the model's thinking)

    Args:
        channel_url: Plain channel address, e.g. https://www.youtube.com/@SomeChannel
        limit: How many of the newest videos to cover (default 20; 0 = the whole channel).
        tab: "videos" (regular uploads, default), "streams" (live stream recordings) or "shorts".
            Keep channel_url as the plain channel address and choose the list with this argument.
        wait_seconds: How long one call may wait for summaries (max 120).
        display_summaries_in_chat: true = return the summaries (about 1k tokens per video);
            false = return only the counts (use this when you only want to index).
    """
    run_key = f"{youtube.channel_tab_url(channel_url, tab)}|{limit}"
    run_started = db.open_run(run_key)

    channel_name, entries = youtube.list_channel(channel_url, limit, tab)
    ids: list[str] = []
    for entry in entries:
        video_id = entry.get("id")
        if video_id and video_id not in ids:
            indexer.enqueue(video_id, f"https://www.youtube.com/watch?v={video_id}", channel_name, entry.get("title"))
            ids.append(video_id)

    rows = indexer.wait_for(ids, wait_seconds)
    ready = [r for r in rows if r and r["status"] == "done"]
    pending = [r for r in rows if r and r["status"] == "pending"]
    failed = [
        {"video_id": r["video_id"], "title": r["title"], "error": r["error"]}
        for r in rows
        if r and r["status"] == "error"
    ]
    summarised_in_run = [r for r in ready if r["summarised_at"] and r["summarised_at"] >= run_started]
    complete = not pending
    if complete:
        db.close_run(run_key)

    result: dict = {
        "channel": channel_name,
        "requested": len(ids),
        "ready": len(ready),
        "from_database": len(ready) - len(summarised_in_run),
        "summarised_in_run": len(summarised_in_run),
        "haiku_tokens_in_run": {
            "input": sum(r["input_tokens"] or 0 for r in summarised_in_run),
            "output": sum(r["output_tokens"] or 0 for r in summarised_in_run),
        },
        "pending": len(pending),
        "failed": failed,
        "complete": complete,
        "run_started": run_started,
    }
    if display_summaries_in_chat:
        result["summaries"] = [format_row(r) for r in ready]  # newest first, as listed by YouTube
    return result


@mcp.tool()
def summarize_videos(video_urls: list[str], wait_seconds: int = 60) -> dict:
    """Summarise specific YouTube videos server side and return the short summaries.

    Use this INSTEAD of get_video_subtitles when you want to understand the content of several
    videos - it returns about 150 words per video instead of the full transcript. Summaries are
    cached, so asking again for the same video is instant.

    Args:
        video_urls: YouTube video URLs.
        wait_seconds: How long to wait for results before returning what is ready (max 120).
            Videos still being processed are reported under "pending"; call index_status or
            this tool again later to get them.
    """
    ids: list[str] = []
    unrecognised = 0
    for url in video_urls:
        video_id = youtube.video_id(url)
        if not video_id:
            unrecognised += 1
        elif video_id not in ids:
            indexer.enqueue(video_id, f"https://www.youtube.com/watch?v={video_id}", None, None)
            ids.append(video_id)

    rows = indexer.wait_for(ids, wait_seconds)
    unfinished = [r for r in rows if r and r["status"] == "pending"]
    done = [format_row(r) for r in rows if r and r["status"] == "done"]
    failed = [{"video_id": r["video_id"], "error": r["error"]} for r in rows if r and r["status"] == "error"]
    result: dict = {"summaries": done, "pending": len(unfinished), "failed": failed}
    if unrecognised:
        result["skipped_unrecognised_urls"] = unrecognised
    return result


@mcp.tool()
def search_summaries(query: str, channel: str | None = None, limit: int = 10, sort: str = "relevance") -> list[dict]:
    """Search the stored video summaries (title, summary, topics and the author's stances).

    This is the main tool for analysing a topic across a whole channel: it returns only the most
    relevant short summaries, so it is cheap regardless of how many videos are indexed. The query
    is matched loosely (any word, word beginnings), so short keywords work best, e.g. "podatki".

    Args:
        query: Keywords to look for.
        channel: Optional channel name fragment or the channel URL to restrict the search to.
        limit: Maximum number of results (default 10).
        sort: "relevance" (default) or "date" (oldest first - use it to see how views changed over time).
    """
    channel, error = resolve_channel(channel)
    if error:
        return [error]
    rows = db.search(query, channel, max(1, min(limit, 50)), order_by_date=(sort == "date"))
    return [format_row(r) for r in rows]


@mcp.tool()
def recent_summaries(channel: str | None = None, limit: int = 20) -> list[dict]:
    """Return the newest already-summarised videos straight from the database (newest first).

    HOW TO USE THE RESULT: by default present EVERY returned video separately, in the order returned
    (newest first). For each video output, VERBATIM from the result and in full: the title, the date, the
    language, the COMPLETE "summary" text, the "topics" list and every entry of "stances" (topic + position).
    The stored summary is already the finished text (often several thousand characters) - copy it word for
    word. NEVER shorten, paraphrase, condense or leave out parts of it, and never replace it with your own
    2-3 sentence version. Do not ask whether the user wants "the full text": the full stored summary is
    what is expected. Do NOT merge the videos into one combined summary and do not add your own conclusions
    - unless the user explicitly asks for a combined/overall summary (for that the get_summary tool is
    made).

    It needs no keywords, makes no YouTube requests and costs nothing. It only covers videos that were
    indexed earlier - if the channel is missing or has too few videos, call sync_channel first, then
    index_status.

    Args:
        channel: Optional channel name fragment or the channel URL (a URL works once sync_channel or
            index_channel has been run for it); omit for all channels.
        limit: How many videos to return (default 20, max 50). Each summary is roughly 1k tokens.
            0 = ALL summarised videos of the channel (no cap; the answer grows ~1k tokens per video).
    """
    channel, error = resolve_channel(channel)
    if error:
        return [error]
    rows = db.recent(channel, None if limit <= 0 else max(1, min(limit, 50)))
    return [format_row(r) for r in rows]


@mcp.tool()
def get_summary(channel_url: str, comment: str = "") -> dict:
    """Data for ONE comprehensive summary of everything stored in the database for a channel.

    The user may call it shorthand-style, e.g. "get_summary(URL, COMMENT)": use this tool with
    channel_url=URL and comment=COMMENT.

    Reads ONLY the database: no YouTube downloads, no summarising model, no tokens spent (apart from
    a one-time cheap lookup the first time an unknown channel URL is used). It never fetches new videos -
    new ones are added by sync_channel.

    HOW TO USE THE RESULT: write ONE comprehensive, coherent summary of all returned videos with the
    sections: (1) the most important topics, (2) the author's main claims and stances, (3) how they
    changed over time (with video dates), (4) conclusions shared by several videos, (5) contradictions or
    unclear points. Base it only on the returned summaries. If "comment" is not empty, treat it as extra
    requirements for the content and form of the summary (what to focus on, format, length); it does not
    change the data source. End by stating how many videos were included ("videos_included").
    If the result contains "error" or "videos_included" is 0, report that and write "brak danych do
    podsumowania" - do not call other tools and do not ask whether to fetch more.

    Args:
        channel_url: Channel address (same as for sync_channel; a tab suffix is ignored) or a channel name
            from list_indexed_channels.
        comment: Optional extra requirements for the summary; may be empty.
    """
    name, error = resolve_channel(channel_url)
    if error:
        return error
    rows = db.recent(name, None)
    summaries = [format_row(r) for r in rows]
    return {
        "channel": name,
        "videos_included": len(summaries),
        "comment": comment,
        "summaries": summaries,  # newest first
    }


@mcp.tool()
def get_video_summary(video: str) -> dict:
    """Get the stored summary of one video, including the author's stances.

    Args:
        video: Video URL or the 11-character video id.
    """
    video_id = youtube.video_id(video) or video
    row = db.get_video(video_id)
    if row is None:
        return {"error": "Video is not indexed. Use summarize_videos or index_channel first."}
    if row["status"] != "done":
        return {"video_id": video_id, "status": row["status"], "error": row["error"]}
    return format_row(row)


@mcp.tool()
def index_status(channel: str | None = None) -> dict:
    """Show indexing progress: how many videos are summarised, pending or failed.

    Args:
        channel: Optional channel name fragment or channel URL; omit for all channels.
    """
    channel, error = resolve_channel(channel)
    if error:
        return error
    return {
        **db.status_counts(channel),
        "haiku_tokens_total": db.token_totals(channel),
        "queue_length": indexer.queue_length(),
        "recent_errors": db.recent_errors(channel),
    }


@mcp.tool()
def list_indexed_channels() -> list[dict]:
    """List the channels that are stored in the database (video counts and date range).
    Call this first in a new conversation to see what can be searched."""
    channels = db.list_channels()
    for c in channels:
        c["oldest"] = fmt_date(c["oldest"])
        c["newest"] = fmt_date(c["newest"])
    return channels


@mcp.tool()
def get_video_subtitles(video_urls: list[str], lang: str = "pl", max_chars: int = 20000) -> list[dict]:
    """Fetch the raw subtitle/transcript text of YouTube videos.

    WARNING: transcripts are long (about 8-12k tokens per 30-minute video). To understand several
    videos use summarize_videos or search_summaries instead. Use this only when you need the exact
    wording of one or two videos.

    The original language of each video is preferred (manual subtitles first, then the original
    auto-captions); `lang` and English are only used as fallbacks.

    Args:
        video_urls: YouTube video URLs, e.g. the "url" values returned by list_channel_videos.
        lang: Fallback language code (default "pl").
        max_chars: Maximum characters of text returned per video (default 20000, roughly 5k tokens).
    """
    results = []
    with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True}) as ydl:
        for video_url in video_urls:
            try:
                info = ydl.extract_info(video_url, download=False) or {}
                language, text = youtube.fetch_transcript(ydl, info, lang)
            except Exception as exc:
                results.append({"url": video_url, "language": None, "subtitles": None, "error": str(exc)})
                continue
            results.append(
                {
                    "url": video_url,
                    "language": language,
                    "subtitles": text[:max_chars],
                    "truncated": len(text) > max_chars,
                    "total_chars": len(text),
                    "error": None,
                }
            )
    return results
