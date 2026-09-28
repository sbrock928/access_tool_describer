from datetime import UTC, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from portfolio_analyzer.v2.review import (
    REVIEW_HEADERS,
    REVIEW_SHEET,
    ReviewDecisionKind,
    carry_forward_decisions,
    import_review_overlay,
)

FINGERPRINT = "a" * 64
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _workbook(path: Path, rows: list[list[str]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = REVIEW_SHEET
    sheet.append(REVIEW_HEADERS)
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def test_review_import_requires_matching_analysis_proposal_and_evidence(tmp_path: Path) -> None:
    path = tmp_path / "review.xlsx"
    _workbook(
        path,
        [[FINGERPRINT, "finding-1", "ev-2, ev-1", "edit", "New label", "Ada", "OK"]],
    )

    overlay = import_review_overlay(
        path,
        analysis_fingerprint=FINGERPRINT,
        known_proposals={"finding-1": ("ev-1", "ev-2")},
        imported_at=NOW,
    )

    assert overlay.decisions[0].decision == ReviewDecisionKind.EDIT
    assert overlay.decisions[0].evidence_ids == ("ev-1", "ev-2")
    assert carry_forward_decisions(
        overlay, known_proposals={"finding-1": ("ev-2", "ev-1")}
    ) == overlay.decisions
    assert carry_forward_decisions(
        overlay, known_proposals={"finding-1": ("ev-1",)}
    ) == ()


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (["b" * 64, "finding-1", "ev-1", "accept", "", "Ada", ""], "stale"),
        ([FINGERPRINT, "unknown", "ev-1", "accept", "", "Ada", ""], "Unknown"),
        (
            [FINGERPRINT, "finding-1", "ev-other", "accept", "", "Ada", ""],
            "conflicting evidence",
        ),
        ([FINGERPRINT, "finding-1", "ev-1", "=A1", "", "Ada", ""], "formula-like"),
    ],
)
def test_review_import_fails_closed_for_stale_or_untrusted_rows(
    tmp_path: Path, row: list[str], message: str
) -> None:
    path = tmp_path / "review.xlsx"
    _workbook(path, [row])

    with pytest.raises(ValueError, match=message):
        import_review_overlay(
            path,
            analysis_fingerprint=FINGERPRINT,
            known_proposals={"finding-1": ("ev-1",)},
            imported_at=NOW,
        )


def test_review_import_rejects_duplicate_proposals(tmp_path: Path) -> None:
    path = tmp_path / "review.xlsx"
    row = [FINGERPRINT, "finding-1", "ev-1", "accept", "", "Ada", ""]
    _workbook(path, [row, row])

    with pytest.raises(ValueError, match="Duplicate"):
        import_review_overlay(
            path,
            analysis_fingerprint=FINGERPRINT,
            known_proposals={"finding-1": ("ev-1",)},
            imported_at=NOW,
        )


def test_review_import_ignores_valid_untouched_proposals(tmp_path: Path) -> None:
    path = tmp_path / "review.xlsx"
    _workbook(
        path,
        [
            [FINGERPRINT, "finding-1", "ev-1", "accept", "", "Ada", ""],
            [FINGERPRINT, "finding-2", "ev-2", "", "", "", ""],
        ],
    )

    overlay = import_review_overlay(
        path,
        analysis_fingerprint=FINGERPRINT,
        known_proposals={"finding-1": ("ev-1",), "finding-2": ("ev-2",)},
        imported_at=NOW,
    )

    assert tuple(item.proposal_id for item in overlay.decisions) == ("finding-1",)
