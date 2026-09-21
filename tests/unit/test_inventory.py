from pathlib import Path

import pytest
from openpyxl import Workbook

from portfolio_analyzer.inventory.loader import InventoryValidationError, load_inventory


def test_load_inventory_preserves_description_and_values(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tool Inventory ID", "Tool Name", "Description", "Filepath", "Owner"])
    sheet.append(
        [
            "100",
            "Payment Tool",
            "Old stated description",
            r"\\server\share\payment.accdb",
            "Finance",
        ]
    )
    path = tmp_path / "inventory.xlsx"
    workbook.save(path)

    record = load_inventory(path)[0]

    assert record.tool_inventory_id == "100"
    assert record.stated_description == "Old stated description"
    assert record.original_values["Owner"] == "Finance"


def test_inventory_requires_expected_columns(tmp_path: Path) -> None:
    workbook = Workbook()
    workbook.active.append(["Tool Name"])
    path = tmp_path / "bad.xlsx"
    workbook.save(path)

    with pytest.raises(InventoryValidationError, match="missing required columns"):
        load_inventory(path)
