"""Human-readable, cross-platform workspace naming."""

from __future__ import annotations

import re
import unicodedata

_INVALID_WINDOWS_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_RESERVED_WORKSPACE_NAMES = {"shared_libraries", "extraction_state.json"}


def euc_directory_name(euc_name: str) -> str:
    """Return a readable component that is safe on Windows and deterministic elsewhere."""
    normalized = unicodedata.normalize("NFC", euc_name).strip()
    component = _INVALID_WINDOWS_CHARACTERS.sub("_", normalized).rstrip(". ")
    component = re.sub(r"_+", "_", component)[:120].rstrip(". ") or "unnamed"
    stem = component.split(".", 1)[0].casefold()
    if stem in _RESERVED_WINDOWS_NAMES or component.casefold() in _RESERVED_WORKSPACE_NAMES:
        component = f"{component}_euc"
    return component


def legacy_inventory_directory_name(inventory_id: str) -> str:
    """Reproduce the pre-EUC workspace component for a safe one-time migration."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", inventory_id).strip("._") or "unnamed"
