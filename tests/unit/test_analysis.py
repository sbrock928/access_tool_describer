from pathlib import Path

from portfolio_analyzer.analysis.application import analyze_application
from portfolio_analyzer.capabilities.discovery import capability_taxonomy, discover_capabilities
from portfolio_analyzer.models import ExtractedApplication, ExtractedObject
from portfolio_analyzer.similarity.code import jaccard_similarity


def test_analysis_creates_evidence_dependencies_and_capabilities() -> None:
    app = ExtractedApplication(
        tool_inventory_id="8",
        staged_path=Path("/local/staged.accdb"),
        extractor_version="fixture",
        objects=[
            ExtractedObject(
                object_type="module",
                name="modReport",
                definition=(
                    'Sub Go(): Set x = CreateObject("Excel.Application"): '
                    'Open "\\\\server\\out.csv": End Sub'
                ),
            ),
            ExtractedObject(object_type="query", name="qDeal", definition="SELECT * FROM dbo.Deal"),
            ExtractedObject(
                object_type="reference",
                name="euc_al",
                properties={"full_path": r"\\server\shared\euc_al.accdb", "is_broken": "True"},
            ),
        ],
    )
    evidence, datasources, dependencies = analyze_application(app)
    capabilities = discover_capabilities(evidence)

    assert datasources[0].object_name == "Deal"
    assert datasources[0].operation == "READ"
    assert dependencies[0].target.startswith("\\\\server")
    assert any(item.dependency_type == "access_vba_reference" for item in dependencies)
    assert capability_taxonomy(capabilities)["Excel automation"] == 1


def test_similarity_is_explainable_token_signal() -> None:
    assert jaccard_similarity("SELECT Deal FROM Tranche", "SELECT Deal FROM Payment") > 0
