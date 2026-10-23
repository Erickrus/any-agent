"""
Text-to-speech -> WeChat voice pipeline.

Two output modes:
  1. "silk"  — full pipeline producing a playable in-chat voice bubble:
                 text -> gTTS mp3 -> pcm_s16le mono 24k (ffmpeg) -> SILK (pilk)
  2. "file"  — quick fallback: gTTS mp3 sent as a file attachment.

All temp files are written under the project-local ./tmp directory (never /tmp).

TTS uses Google Translate's public translate_tts endpoint directly (same
approach as reference/gtts.html) — no gTTS package needed, just aiohttp.

Optional deps (import lazily so the bot still runs without them):
  - pilk          (pip install pilk)      PCM <-> SILK codec
  - ffmpeg        (system binary)         mp3 -> pcm resample
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

logger = logging.getLogger("wx_bridge.voice")

# WeChat voice is decoded at 24 kHz mono s16le (see silk-transcode.ts SILK_SAMPLE_RATE).
SILK_SAMPLE_RATE = 24_000

# Google Translate TTS: single-chunk mp3, client=tw-ob, <=~200 chars per request.
_TTS_URL = "https://translate.google.com/translate_tts"
_TTS_MAX_CHUNK = 150
# translate_tts blocks requests without a browser-like UA.
_TTS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Project-local scratch dir. Resolved relative to the repo root (two levels up
# from this file: any_agent/voice.py -> repo/). Kept out of /tmp per project rule.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = os.path.join(_REPO_ROOT, "tmp")


class VoiceError(RuntimeError):
    """Raised when the TTS/encode pipeline cannot produce audio."""


@dataclass
class VoiceResult:
    """A produced audio file ready to send."""
    path: str
    # "silk" -> send as a type:3 voice_item bubble; "file" -> send as attachment.
    kind: str
    # Duration in milliseconds (best-effort; 0 if unknown). Used for playtime.
    duration_ms: int = 0


def _ensure_tmp() -> str:
    os.makedirs(TMP_DIR, exist_ok=True)
    return TMP_DIR


def _stamp(prefix: str, ext: str) -> str:
    return os.path.join(_ensure_tmp(), f"{prefix}-{int(time.time()*1000)}{ext}")


def _cleanup(*paths: str) -> None:
    for p in paths:
        if not p:
            continue
        try:
            os.remove(p)
        except OSError:
            pass


# --- pipeline stages (blocking; run via asyncio.to_thread) ------------------

def _split_text(text: str, max_len: int = _TTS_MAX_CHUNK) -> list[str]:
    """
    Split text into <=max_len chunks on sentence boundaries, matching the
    splitText() logic in reference/gtts.html so long replies stay fluent.
    """
    sentences = re.findall(r"[^,.;!?，。；！？\r\n]+[,.;!?，。；！？\r\n]*", text) or [text]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) > max_len:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            while len(sentence) > max_len:
                chunks.append(sentence[:max_len].strip())
                sentence = sentence[max_len:]
        current += sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks


# Local proxies to Google are often flaky (connection drops on the first try,
# succeeds on a retry), so each chunk request is retried with a short backoff.
_TTS_RETRIES = 4
_TTS_BACKOFF_S = 0.8


async def _fetch_chunk(session, url: str, proxy: str | None, idx: int) -> bytes:
    """GET one TTS chunk, retrying transient connection/HTTP failures."""
    last_err: Exception | None = None
    for attempt in range(1, _TTS_RETRIES + 1):
        try:
            async with session.get(
                url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status != 200:
                    raise VoiceError(f"translate_tts chunk {idx} HTTP {resp.status}")
                data = await resp.read()
                if not data:
                    raise VoiceError(f"translate_tts chunk {idx} empty body")
                return data
        except Exception as e:
            last_err = e
            if attempt < _TTS_RETRIES:
                logger.warning(
                    f"TTS chunk {idx} attempt {attempt} failed ({e}); retrying"
                )
                await asyncio.sleep(_TTS_BACKOFF_S * attempt)
    raise VoiceError(f"chunk {idx} failed after {_TTS_RETRIES} attempts: {last_err}")


async def _tts_to_mp3(text: str, lang: str, proxy: str | None = None) -> str:
    """
    Synthesize speech to an mp3 file by calling Google Translate's translate_tts
    endpoint per chunk and concatenating the mp3 bytes. Returns the mp3 path.

    proxy: HTTP proxy URL for reaching Google (e.g. "http://127.0.0.1:7890").
    """
    chunks = _split_text(text)
    if not chunks:
        raise VoiceError("nothing to synthesize")

    mp3_path = _stamp("tts", ".mp3")
    headers = {"User-Agent": _TTS_UA, "Referer": "https://translate.google.com/"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            with open(mp3_path, "wb") as out:
                for idx, chunk in enumerate(chunks):
                    url = (
                        f"{_TTS_URL}?ie=UTF-8&client=tw-ob"
                        f"&tl={quote(lang)}&q={quote(chunk)}"
                    )
                    data = await _fetch_chunk(session, url, proxy, idx)
                    out.write(data)
    except VoiceError:
        _cleanup(mp3_path)
        raise
    except Exception as e:
        _cleanup(mp3_path)
        raise VoiceError(f"TTS request failed: {e}") from e

    if not os.path.getsize(mp3_path):
        _cleanup(mp3_path)
        raise VoiceError("TTS produced an empty file")
    return mp3_path


def _mp3_to_pcm(mp3_path: str) -> str:
    """Transcode mp3 -> raw pcm_s16le mono @ SILK_SAMPLE_RATE using ffmpeg."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        raise VoiceError("ffmpeg not found on PATH")

    pcm_path = _stamp("tts", ".pcm")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", mp3_path,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ac", "1", "-ar", str(SILK_SAMPLE_RATE),
        pcm_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(pcm_path):
        _cleanup(pcm_path)
        raise VoiceError(f"ffmpeg failed: {proc.stderr.strip()[:200]}")
    return pcm_path


def _pcm_to_silk(pcm_path: str) -> tuple[str, int]:
    """Encode raw PCM -> SILK via pilk. Returns (silk_path, duration_ms).

    Duration is computed from the PCM byte count (mono s16le = 2 bytes/sample),
    not pilk's return value — pilk.encode() returns whole seconds, which rounds
    short clips to 0 and makes WeChat reject the voice as empty.
    """
    try:
        import pilk
    except ImportError as e:
        raise VoiceError("pilk not installed (pip install pilk)") from e

    silk_path = _stamp("tts", ".silk")
    try:
        pilk.encode(pcm_path, silk_path, pcm_rate=SILK_SAMPLE_RATE, tencent=True)
    except Exception as e:
        _cleanup(silk_path)
        raise VoiceError(f"pilk encode failed: {e}") from e

    pcm_bytes = os.path.getsize(pcm_path)
    duration_ms = int(pcm_bytes / (SILK_SAMPLE_RATE * 2) * 1000)
    return silk_path, duration_ms


# --- public API -------------------------------------------------------------

async def synthesize(
    text: str, lang: str = "en", prefer: str = "silk", proxy: str | None = None
) -> VoiceResult:
    """
    Turn text into a sendable audio file.

    prefer="silk": attempt the full SILK bubble pipeline; on any failure
                   (missing ffmpeg/pilk, encode error) fall back to the raw mp3
                   as a file attachment so the user still gets audio.
    prefer="file": skip SILK, always return the mp3 as an attachment.
    proxy:         HTTP proxy for reaching Google (e.g. "http://127.0.0.1:7890").

    Callers own VoiceResult.path and should delete it after sending.
    """
    text = (text or "").strip()
    if not text:
        raise VoiceError("empty text")

    mp3_path = await _tts_to_mp3(text, lang, proxy=proxy)

    if prefer == "file":
        return VoiceResult(path=mp3_path, kind="file")

    pcm_path = ""
    try:
        pcm_path = await asyncio.to_thread(_mp3_to_pcm, mp3_path)
        silk_path, duration_ms = await asyncio.to_thread(_pcm_to_silk, pcm_path)
    except VoiceError as e:
        logger.warning(f"SILK pipeline unavailable ({e}); sending mp3 as file")
        _cleanup(pcm_path)
        return VoiceResult(path=mp3_path, kind="file")

    _cleanup(mp3_path, pcm_path)
    return VoiceResult(path=silk_path, kind="silk", duration_ms=duration_ms)
