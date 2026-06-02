"""Minimal zero-dependency .env loader.

Reads KEY=VALUE pairs from a .env file into os.environ, so the scripts can be
configured from a file instead of (or in addition to) shell `export`s. Kept
deliberately dependency-free to match the rest of the project (stdlib only).

Behaviour, matching the common dotenv convention:

  * Variables already present in the real environment win — a value passed via
    `docker run -e KEY=...` or a shell `export` is never overwritten by .env.
  * Blank lines and lines starting with `#` are ignored.
  * Inline comments after an unquoted value are stripped (`KEY=25  # note`).
  * Values may be wrapped in single or double quotes to preserve spaces / `#`.
  * A leading `export ` on a line is tolerated.
"""

import os
from pathlib import Path


def _parse_value(raw):
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[1:end]
        return raw[1:]  # unterminated quote; take the rest
    # Unquoted: an inline comment starts at the first '#'.
    hash_at = raw.find("#")
    if hash_at != -1:
        raw = raw[:hash_at]
    return raw.strip()


def load_dotenv(path=None):
    """Load KEY=VALUE pairs from `path` (default: .env next to this file).

    Missing file is a no-op. Returns the dict of keys actually applied.
    """
    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    path = Path(path)

    applied = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return applied

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):]
        if "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue  # real environment takes precedence
        value = _parse_value(raw_value)
        os.environ[key] = value
        applied[key] = value
    return applied
