from pathlib import Path

import pytest
from openpyxl import Workbook

from portfolio_analyzer.inventory.loader import InventoryValidationError, load_inventory


def test_load_inventory_preserves_description_and_values(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["INVENTORY_ID", "EUCTNAME", "FILE_NAME", "FULLPATH", "DESCRIPTION", "Owner"])
    sheet.append(
        [
            "100",
            "Payment Tool",
            "payment.accdb",
            r"\\server\share\payment.accdb",
            "Old stated description",
            "Finance",
        ]
    )
    path = tmp_path / "inventory.xlsx"
    workbook.save(path)

    record = load_inventory(path)[0]

    assert record.tool_inventory_id == "100"
    assert record.inventory_filename == "payment.accdb"
    assert record.stated_description == "Old stated description"
    assert record.original_values["Owner"] == "Finance"


def test_inventory_requires_expected_columns(tmp_path: Path) -> None:
    workbook = Workbook()
    workbook.active.append(["EUCTNAME"])
    path = tmp_path / "bad.xlsx"
    workbook.save(path)

    with pytest.raises(InventoryValidationError, match="missing required columns"):
        load_inventory(path)
