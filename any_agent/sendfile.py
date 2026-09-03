"""
Parser for the `sendfile://` media protocol.

Agents emit marker lines to request that a local file be delivered to the WeChat user:

    sendfile:///Users/me/charts/chart.png
    sendfile:///tmp/report.pdf?caption=Here%20is%20your%20report

The path (and optional caption) are URL-encoded so paths with spaces / special chars
survive intact. parse_sendfile() extracts the markers, url-decodes them, and returns the
response text with the marker lines removed.
"""

import re
from urllib.parse import unquote

# Match a whole line that is a sendfile marker. Path = non-space up to optional ?caption=.
SENDFILE_RE = re.compile(
    r'^[ \t]*sendfile://(?P<path>\S+?)(?:\?caption=(?P<caption>\S*))?[ \t]*$',
    re.MULTILINE,
)


def parse_sendfile(text: str) -> tuple[str, list[tuple[str, str]]]:
    """
    Extract sendfile:// markers from agent text.

    Returns (clean_text, media) where media is a list of (abs_path, caption) tuples.
    - path and caption are URL-decoded (%20 -> space).
    - Only absolute paths (starting with '/') are accepted; others are ignored but still
      stripped from the text.
    - Marker lines are removed from clean_text.
    """
    if not text or "sendfile://" not in text:
        return text, []

    media: list[tuple[str, str]] = []

    for m in SENDFILE_RE.finditer(text):
        raw_path = m.group("path") or ""
        raw_caption = m.group("caption") or ""
        path = unquote(raw_path)
        caption = unquote(raw_caption)
        if path.startswith("/"):
            media.append((path, caption))

    # Remove marker lines from the text.
    clean = SENDFILE_RE.sub("", text)
    # Collapse the blank lines left behind (3+ newlines -> 2, trim edges).
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()

    return clean, media
