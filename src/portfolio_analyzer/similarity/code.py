"""Normalized token similarity for code or SQL; not semantic proof."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


def normalized_tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.casefold()))


def jaccard_similarity(left: str, right: str) -> float:
    a, b = normalized_tokens(left), normalized_tokens(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def sequence_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, re.sub(r"\s+", " ", left), re.sub(r"\s+", " ", right)).ratio()
