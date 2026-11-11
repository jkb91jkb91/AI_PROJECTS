"""Summarise one video transcript with Claude Haiku (server side, so it never touches the chat context)."""

import os
from typing import NamedTuple

import anthropic
from pydantic import BaseModel, Field

MODEL = "claude-haiku-5-5"

# Safety cap on transcript size sent to the model (~100k tokens). Haiku 5.5 gets pricier above
# 100k prompt tokens, and a transcript this long is a multi-hour video anyway.
MAX_TRANSCRIPT_CHARS = 300_000

SYSTEM = (
    "Tworzysz DOKŁADNE streszczenie transkrypcji filmu z YouTube po polsku, niezależnie od języka "
    "transkrypcji. Streszczenie ma służyć do późniejszego wyszukiwania i analizy, więc nie może gubić "
    "szczegółów. Zasady:\n"
    "- Idź wątek po wątku, w kolejności z filmu. Nie pomijaj wątków pobocznych ani jednozdaniowych wzmianek, "
    "jeśli niosą jakąś informację lub opinię.\n"
    "- Zachowuj konkrety: liczby, daty, nazwy własne, nazwiska, tytuły, przykłady, argumenty, przyczyny "
    "i wnioski. Pisz 'autor twierdzi, że...', a nie ogólniki w stylu 'autor omawia temat'.\n"
    "- Oddzielaj fakty podawane w filmie od opinii autora.\n"
    "- Pisz własnymi słowami, bez długich cytatów. Nie dodawaj niczego, czego nie ma w transkrypcji, "
    "i niczego nie zgaduj. Jeśli fragment jest niejasny (np. błędy automatycznych napisów), "
    "napisz to zamiast domyślać się treści.\n"
    "- Długość: dopasuj do liczby zdań podanej w wiadomości użytkownika (to wartość orientacyjna, "
    "dłuższe i bogatsze filmy mogą wymagać więcej).\n"
    "- 'topics': 8-15 haseł po polsku w mianowniku, obejmujących wszystkie poruszone tematy.\n"
    "- 'stances': każda wyraźna opinia lub teza autora jako temat + 1-2 zdania, bez limitu liczby."
)


def target_sentences(transcript_chars: int) -> int:
    """Rough target length of the summary: about 1 sentence per 1.5 minutes of video.
    Spoken Polish is ~1000 characters of subtitles per minute."""
    minutes = transcript_chars / 1000
    return max(8, min(60, round(minutes / 1.5)))


class Stance(BaseModel):
    topic: str
    position: str = Field(description="Co autor twierdzi w tym filmie, 1-2 zdania")


class VideoSummary(BaseModel):
    summary: str
    topics: list[str]
    stances: list[Stance]


class SummaryResult(NamedTuple):
    summary: VideoSummary
    input_tokens: int   # actually billed prompt tokens (from the API response)
    output_tokens: int  # actually billed output tokens, thinking included


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("CLAUDE_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("Brak klucza API: ustaw zmienną środowiskową CLAUDE_KEY (lub ANTHROPIC_API_KEY).")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def summarize(title: str | None, upload_date: str | None, transcript: str) -> SummaryResult:
    truncated = len(transcript) > MAX_TRANSCRIPT_CHARS
    if truncated:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS]

    header = (
        f"Tytuł: {title or '?'}\nData publikacji: {upload_date or '?'}\n"
        f"Docelowa długość streszczenia: ok. {target_sentences(len(transcript))} zdań.\n"
    )
    if truncated:
        header += "(Uwaga: transkrypcja została obcięta - to tylko początek filmu.)\n"

    response = _get_client().messages.parse(
        model=MODEL,
        max_tokens=8000,
        output_config={"effort": "medium"},
        system=SYSTEM,
        messages=[{"role": "user", "content": f"{header}\n<transkrypcja>\n{transcript}\n</transkrypcja>"}],
        output_format=VideoSummary,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Model odmówił streszczenia tego filmu (refusal).")
    if response.parsed_output is None:
        raise RuntimeError(f"Brak poprawnego wyniku od modelu (stop_reason={response.stop_reason}).")
    usage = response.usage
    return SummaryResult(response.parsed_output, usage.input_tokens, usage.output_tokens)
