from portfolio_analyzer.models import CapabilityFinding, Confidence, Evidence
from portfolio_analyzer.portfolio.recommendations import build_recommendations


def test_outlook_repetition_creates_evidence_backed_service_candidate() -> None:
    findings = [
        CapabilityFinding(
            tool_inventory_id=tool_id,
            capability="Outlook automation",
            layer="technical",
            confidence=Confidence.HIGH,
            evidence=[
                Evidence(
                    tool_inventory_id=tool_id,
                    artifact_path="staged.accdb",
                    object_type="module",
                    object_name="modEmail",
                    text="CreateObject(Outlook.Application)",
                    inference="Outlook automation",
                )
            ],
        )
        for tool_id in ("101", "102", "103")
    ]

    recommendations = build_recommendations(findings, [])

    assert recommendations[0].category == "microservice/api"
    assert recommendations[0].affected_tool_ids == ["101", "102", "103"]


def test_single_occurrence_does_not_create_service_candidate() -> None:
    finding = CapabilityFinding(
        tool_inventory_id="101",
        capability="Outlook automation",
        layer="technical",
        confidence=Confidence.HIGH,
    )

    assert build_recommendations([finding], []) == []
