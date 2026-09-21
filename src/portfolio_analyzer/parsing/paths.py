"""Static filesystem dependency discovery."""

from __future__ import annotations

import re

WINDOWS_PATH = re.compile(r"(?<!\w)(?:\\\\[^\s\"']+|[A-Za-z]:\\[^\s\"']+)")


def extract_windows_paths(text: str) -> list[str]:
    return list(
        dict.fromkeys(match.group(0).rstrip(".,;)") for match in WINDOWS_PATH.finditer(text))
    )
