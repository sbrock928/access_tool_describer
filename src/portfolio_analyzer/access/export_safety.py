"""Fail-closed preconditions for the optional Access SaveAsText export lane."""

from __future__ import annotations

import os
import platform
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExportSafetyDecision:
    permitted: bool
    reason: str


class ExportSafetyGate(Protocol):
    def evaluate(self) -> ExportSafetyDecision:
        """Confirm that opening a disposable database in Access is permitted."""


class EnvironmentExportSafetyGate:
    """Require explicit attestations supplied by a hardened Windows worker image.

    These values are deployment preconditions, not user-facing convenience flags. A normal host
    therefore performs the DAO-only lane and records partial UI/code coverage.
    """

    def evaluate(self) -> ExportSafetyDecision:
        if platform.system() != "Windows":
            return ExportSafetyDecision(False, "SaveAsText requires a Windows worker")
        if os.environ.get("ACCESS_ANALYZER_NETWORK_ISOLATED") != "1":
            return ExportSafetyDecision(
                False, "outbound-network isolation was not attested by the worker"
            )
        if os.environ.get("ACCESS_ANALYZER_MACROS_DISABLED") != "1":
            return ExportSafetyDecision(
                False, "non-low Access macro policy was not attested by the worker"
            )
        if not _is_low_privilege_windows_identity():
            return ExportSafetyDecision(
                False, "the Access worker identity is privileged or could not be verified"
            )
        return ExportSafetyDecision(True, "hardened worker preconditions are attested")


def _is_low_privilege_windows_identity() -> bool:
    """Fail closed unless the current Windows token is not an administrator token."""

    with suppress(Exception):
        import ctypes

        return not bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    return False
