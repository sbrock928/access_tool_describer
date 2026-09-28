from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

import pytest

from portfolio_analyzer.access.worker import run_extraction_with_timeout
from portfolio_analyzer.config import AnalyzerSettings
from portfolio_analyzer.models import InventoryRecord, VerifiedStagedArtifact
from portfolio_analyzer.staging.copying import ArtifactStager
from portfolio_analyzer.staging.hashing import sha256_file


@pytest.mark.windows_access
def test_synthetic_access_startup_and_network_controls(tmp_path: Path) -> None:
    """Exercise the externally monitored synthetic AutoExec/network security fixture."""

    if platform.system() != "Windows":
        pytest.skip("Windows Access integration test")
    fixture_value = os.environ.get("ACCESS_ANALYZER_SECURITY_FIXTURE")
    marker_value = os.environ.get("ACCESS_ANALYZER_STARTUP_MARKER")
    audit_value = os.environ.get("ACCESS_ANALYZER_NETWORK_AUDIT_LOG")
    prompt_value = os.environ.get("ACCESS_ANALYZER_CREDENTIAL_PROMPT_MARKER")
    if not fixture_value or not marker_value or not audit_value or not prompt_value:
        pytest.skip(
            "synthetic Access security fixture, audit, and prompt-monitor paths are not configured"
        )
    fixture = Path(fixture_value).resolve()
    marker = Path(marker_value).resolve()
    network_audit = Path(audit_value).resolve()
    credential_prompt_marker = Path(prompt_value).resolve()
    if not fixture.is_file():
        pytest.skip("synthetic Access security fixture is unavailable")
    if os.environ.get("ACCESS_ANALYZER_NETWORK_ISOLATED") != "1":
        pytest.skip("outbound-network isolation is not attested")
    if os.environ.get("ACCESS_ANALYZER_MACROS_DISABLED") != "1":
        pytest.skip("non-low macro policy is not attested")

    settings = AnalyzerSettings(workspace=tmp_path / "workspace")
    settings.ensure_workspace()
    artifact = ArtifactStager(settings).stage_primary(
        InventoryRecord(
            tool_inventory_id="windows-security-fixture",
            tool_name="Windows Security Fixture",
            inventory_filename=fixture.name,
            filepath=fixture,
        )
    )
    assert artifact.local_staged_path is not None
    before_hash = sha256_file(artifact.local_staged_path)
    marker.unlink(missing_ok=True)
    credential_prompt_marker.unlink(missing_ok=True)
    network_audit.write_text("", encoding="utf-8")
    before_processes = _access_processes()

    extracted = run_extraction_with_timeout(
        VerifiedStagedArtifact.from_staged(artifact),
        settings.extracted_dir / "security-fixture",
        settings,
        timeout_seconds=120,
        on_progress=lambda _message: None,
    )

    assert extracted.objects
    assert extracted.derived_copy_sha256 is not None, "gated SaveAsText lane did not run"
    assert sha256_file(artifact.local_staged_path) == before_hash
    assert not marker.exists(), "AutoExec/startup marker executed"
    assert not credential_prompt_marker.exists(), "credential prompt was observed"
    assert not network_audit.read_text(encoding="utf-8").strip(), "outbound activity observed"
    assert _access_processes() == before_processes, "orphan Microsoft Access process remains"
    assert not list(settings.extracted_dir.rglob("*.txt")), "raw SaveAsText export remains"
    _assert_shift_released()


def _access_processes() -> tuple[str, ...]:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq MSACCESS.EXE", "/FO", "CSV", "/NH"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return tuple(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and "No tasks are running" not in line
    )


def _assert_shift_released() -> None:
    try:
        import win32api  # type: ignore[import-untyped]
        import win32con  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - Windows integration dependency
        raise AssertionError("pywin32 is required for the Windows security fixture") from exc
    assert not bool(win32api.GetAsyncKeyState(win32con.VK_SHIFT) & 0x8000)
