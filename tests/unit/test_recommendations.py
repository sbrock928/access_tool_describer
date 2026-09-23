from portfolio_analyzer.models import CapabilityFinding, Confidence, Evidence
from portfolio_analyzer.portfolio.recommendations import build_recommendations


def test_repeated_signal_discovers_a_group_without_forcing_a_service() -> None:
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

    assert recommendations[0].category == "discovered_evidence"
    assert "Outlook automation" in recommendations[0].title
    assert recommendations[0].affected_tool_ids == ["101", "102", "103"]


def test_single_occurrence_does_not_create_service_candidate() -> None:
    finding = CapabilityFinding(
        tool_inventory_id="101",
        capability="Outlook automation",
        layer="technical",
        confidence=Confidence.HIGH,
    )

    assert build_recommendations([finding], []) == []


def test_existing_recommendations_and_risks_keep_application_object_locations() -> None:
    from portfolio_analyzer.models import AnalysisCoverage, Recommendation
    from portfolio_analyzer.portfolio.themes import build_portfolio_themes

    evidence = [Evidence(
        tool_inventory_id=tool_id, artifact_path=f"{tool_id}.accdb",
        object_type="module", object_name="SendMail", location="line 12",
        text='CreateObject("Outlook.Application")', inference="Outlook automation",
    ) for tool_id in ("a", "b", "c")]
    recommendation = Recommendation(
        category="microservice/api", title="Notification delivery service candidate",
        rationale="Repeated mail delivery", affected_tool_ids=["a", "b", "c"],
        confidence=Confidence.MEDIUM, evidence=evidence,
    )
    themes = build_portfolio_themes(None, [recommendation], evidence, [AnalysisCoverage(
        tool_inventory_id="b", tool_name="Mail B", extraction_status="failed",
        staging_status="staged", analysis_status="not_run",
    )])
    assert len(themes) == 1
    for theme in themes:
        assert theme.affected_tool_ids == ["a", "b", "c"]
        assert {loc.evidence_id for loc in theme.locations} == {e.evidence_id for e in evidence}
        assert all(loc.object_name == "SendMail" and loc.location == "line 12"
                   for loc in theme.locations)
        assert theme.confidence == Confidence.LOW
    assert "Outlook automation" in themes[0].proposed_solution
    assert themes[0].alternative_options
