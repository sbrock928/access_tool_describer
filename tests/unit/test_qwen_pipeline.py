from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from portfolio_analyzer.qwen.pipeline import (
    APPLICATION_PROMPT_VERSION,
    UNIT_PROMPT_VERSION,
    ApplicationAnalysisResult,
    InferenceCacheKey,
    InterpretationRunStatus,
    LogicalUnitKind,
    PortfolioRunStatus,
    analyze_application_two_stage,
    analyze_portfolio_candidates,
    build_definition_chunks,
    build_logical_units,
    content_fingerprint,
    inference_cache_key,
    model_fingerprint,
    prompt_fingerprint,
    schema_fingerprint,
)
from portfolio_analyzer.qwen.provider import PromptBudget
from portfolio_analyzer.v2.fingerprints import evidence_bundle_fingerprint
from portfolio_analyzer.v2.models import (
    AccessObjectEvidence,
    AccessObjectType,
    AccessTableEvidence,
    ApplicationEvidenceBundle,
    ApplicationInterpretation,
    ArtifactEvidence,
    ConnectionEvidence,
    ConnectionKind,
    ConnectionProvenance,
    DataOperation,
    DatasourceIdentity,
    DatasourceInteraction,
    DependencyEdge,
    DependencyNode,
    DependencyNodeKind,
    DependencyRelation,
    EvidenceRecord,
    ExtractionCoverage,
    ExtractionStatus,
    InteractionScope,
    ModelProvenance,
    OwnerClaim,
    PortfolioCandidate,
    PortfolioCandidateType,
    QueryEvidence,
    QueryKind,
    ResolutionStatus,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
ARTIFACT_DIGEST = "a" * 64
MODEL_DIGEST = "b" * 64


class _MemoryInferenceCache:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def get(self, key: InferenceCacheKey) -> dict[str, Any] | None:
        return self.values.get(key.cache_key)

    def put(self, key: InferenceCacheKey, value: dict[str, Any]) -> None:
        self.values[key.cache_key] = value


class _PipelineProvider:
    def __init__(
        self,
        *,
        fail_kind: str | None = None,
        repair_unknown_once: bool = False,
        application_evidence_id: str | None = None,
        portfolio_mode: str = "normal",
        context_tokens: int = 10_000_000,
        max_definition_chars_per_prompt: int | None = None,
        max_application_inputs_per_prompt: int | None = None,
        max_portfolio_candidates_per_prompt: int | None = None,
        leaked_text: str | None = None,
    ) -> None:
        self.fail_kind = fail_kind
        self.repair_unknown_once = repair_unknown_once
        self.application_evidence_id = application_evidence_id
        self.portfolio_mode = portfolio_mode
        self.context_tokens = context_tokens
        self.max_definition_chars_per_prompt = max_definition_chars_per_prompt
        self.max_application_inputs_per_prompt = max_application_inputs_per_prompt
        self.max_portfolio_candidates_per_prompt = max_portfolio_candidates_per_prompt
        self.leaked_text = leaked_text
        self.calls: list[dict[str, Any]] = []
        self.measurements: list[dict[str, Any]] = []
        self._attempts: dict[tuple[str, str], int] = {}

    def measure_prompt(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Mapping[str, Any],
        max_output_tokens: int = 1_024,
    ) -> PromptBudget:
        serialized_schema = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        prompt_tokens = len(system) + len(user) + len(schema_name) + len(serialized_schema)
        payload = json.loads(user)
        over_limit = False
        if payload.get("stage") in {"logical_unit", "logical_unit_batch"}:
            units = (
                payload.get("units", [])
                if payload.get("stage") == "logical_unit_batch"
                else [payload]
            )
            definition_sizes = [
                sum(len(item["text"]) for item in unit.get("definitions", []))
                for unit in units
            ]
            over_limit = bool(
                self.max_definition_chars_per_prompt is not None
                and any(
                    size > self.max_definition_chars_per_prompt
                    for size in definition_sizes
                )
            )
        if payload.get("stage", "").startswith("application_synthesis"):
            input_count = len(
                payload.get(
                    "logical_unit_interpretations",
                    payload.get("application_interpretations", []),
                )
            )
            over_limit = over_limit or (
                self.max_application_inputs_per_prompt is not None
                and input_count > self.max_application_inputs_per_prompt
            )
        if payload.get("stage") == "portfolio_candidate_interpretation":
            over_limit = over_limit or (
                self.max_portfolio_candidates_per_prompt is not None
                and len(payload.get("candidates", []))
                > self.max_portfolio_candidates_per_prompt
            )
        if over_limit:
            prompt_tokens = self.context_tokens
        self.measurements.append(
            {
                "user": user,
                "schema_name": schema_name,
                "prompt_tokens": prompt_tokens,
                "max_output_tokens": max_output_tokens,
            }
        )
        return PromptBudget(
            prompt_tokens=prompt_tokens,
            reserved_output_tokens=max_output_tokens,
            context_tokens=self.context_tokens,
        )

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Mapping[str, Any],
        max_output_tokens: int = 1_024,
    ) -> dict[str, Any]:
        raw = json.loads(user)
        repaired = "original_request" in raw
        payload = json.loads(raw["original_request"]) if repaired else raw
        self.calls.append(
            {
                "system": system,
                "payload": payload,
                "schema_name": schema_name,
                "schema": schema,
                "max_output_tokens": max_output_tokens,
                "repaired": repaired,
            }
        )
        stage = payload["stage"]
        if stage in {"logical_unit", "logical_unit_reduction"}:
            return self._logical_response(payload)
        if stage == "logical_unit_batch":
            return {
                "schema_version": "logical-unit-batch-output-v1",
                "interpretations": [
                    self._logical_response(unit) for unit in payload["units"]
                ],
            }
        if stage in {
            "application_synthesis",
            "application_synthesis_batch",
            "application_synthesis_reduction",
        }:
            return self._application_response(payload)
        if stage in {"portfolio_candidate_interpretation", "portfolio_reduction"}:
            return self._portfolio_response(payload)
        raise AssertionError(f"unexpected stage: {stage}")

    def _logical_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected = payload["expected_output"]
        unit = payload["logical_unit"]
        kind = unit["kind"]
        key = ("unit", expected["logical_unit_id"])
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt
        called_ids: list[str] = []
        should_invent = kind == self.fail_kind or (
            self.repair_unknown_once and attempt == 1
        )
        if should_invent:
            called_ids = ["object_unknown_or_cross_application"]
        allowed = payload["allowed_ids"]
        interaction_ids = allowed["datasource_interaction_ids"]
        return {
            **expected,
            "purpose": self.leaked_text
            or ("Insufficient evidence; abstain." if kind == "macro" else "Grounded purpose"),
            "business_entities": [],
            "workflow_actions": [],
            "datasource_interaction_ids": interaction_ids[:1],
            "called_object_ids": called_ids,
            "generated_outputs": [],
            "user_interactions": [],
            "important_business_terms": [],
            "uncertainties": ["Evidence is static."],
            "evidence_ids": [allowed["evidence_ids"][0]],
        }

    def _application_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected = payload["expected_output"]
        allowed = payload["allowed_ids"]
        evidence_id = self.application_evidence_id or allowed["evidence_ids"][0]
        return {
            **expected,
            "summary": self.leaked_text or "Grounded application summary.",
            "business_purpose": "Support a reviewed workflow.",
            "major_workflows": [],
            "capabilities": [],
            "modernization_concerns": [],
            "uncertainties": [],
            "findings": [],
            "evidence_ids": [evidence_id],
            "claim_ids": [],
        }

    def _portfolio_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected = payload["expected_output"]
        candidates = payload.get("candidates", payload.get("candidate_registry", []))
        if self.portfolio_mode == "abstain" or not candidates:
            findings: list[dict[str, Any]] = []
        else:
            candidate = candidates[0]
            application_ids = list(candidate["application_ids"])
            evidence_ids = list(candidate["evidence_ids"][:1])
            title = "Shared capability review"
            narrative = "Review the supplied candidate for reuse."
            if self.portfolio_mode == "invent_membership":
                application_ids.append("app-invented")
            if self.portfolio_mode == "invent_evidence":
                evidence_ids = ["evidence-invented"]
            if self.portfolio_mode == "retire":
                title = "Retire these applications"
                narrative = "Decommission the candidate members."
            findings = [
                {
                    "kind": "portfolio_opportunity",
                    "title": title,
                    "narrative": narrative,
                    "application_ids": application_ids,
                    "evidence_ids": evidence_ids,
                    "confidence": "medium",
                    "uncertainties": [],
                }
            ]
        return {
            **expected,
            "similarities": [],
            "findings": findings,
            "uncertainties": [],
        }


def _provenance() -> ModelProvenance:
    return ModelProvenance(
        model_manifest_sha256=MODEL_DIGEST,
        prompt_version="qwen-two-stage-v2",
        output_schema_version="v2-interpretations",
        inference_library_version="5.17.0",
    )


def _object(
    artifact_id: str,
    object_type: AccessObjectType,
    name: str,
    definition: str | None,
    evidence_ids: tuple[str, ...] = (),
) -> AccessObjectEvidence:
    return AccessObjectEvidence(
        artifact_id=artifact_id,
        object_type=object_type,
        name=name,
        sanitized_definition=definition,
        evidence_ids=evidence_ids,
    )


def _bundle(
    application_id: str = "app-1",
    *,
    rich: bool = True,
    lifecycle_claim: bool = True,
    aggregate_evidence: bool = False,
) -> ApplicationEvidenceBundle:
    artifact = ArtifactEvidence(
        application_id=application_id,
        source_locator=f"//source/{application_id}/tool.accdb",
        staged_relative_path=f"applications/{application_id}/tool.accdb",
        filename="tool.accdb",
        access_format="accdb",
        sha256=ARTIFACT_DIGEST,
        size_bytes=42,
        extractor_version="dao-v2",
        extraction_status=ExtractionStatus.COMPLETE,
        extracted_at=NOW,
    )
    specs: list[tuple[AccessObjectType, str, str | None]] = [
        (
            AccessObjectType.QUERY,
            "qryDeals",
            "SELECT DealId, InvestorId\nFROM dbo.Deal\nWHERE Active = 1;" * (4 if rich else 1),
        ),
    ]
    if rich:
        specs.extend(
            [
                (
                    AccessObjectType.PROCEDURE,
                    "modWorkflow.RunReport",
                    "Public Sub RunReport()\nDoCmd.OpenReport \"rptDeals\"\nEnd Sub\n" * 3,
                ),
                (
                    AccessObjectType.MODULE,
                    "modHelpers",
                    "Public Function NameDeal()\nEnd Function\n",
                ),
                (AccessObjectType.MODULE, "modAudit", "Public Sub LogRun()\nEnd Sub\n"),
                (AccessObjectType.FORM, "frmDeals", "Begin Form\nRecordSource = qryDeals\nEnd\n"),
                (
                    AccessObjectType.REPORT,
                    "rptDeals",
                    "Begin Report\nRecordSource = qryDeals\nEnd\n",
                ),
                (AccessObjectType.MACRO, "mcrStartup", "Action=OpenForm\nArgument=frmDeals\n"),
                (AccessObjectType.TABLE, "DealCache", None),
            ]
        )
    initial = [
        _object(artifact.artifact_id, object_type, name, definition)
        for object_type, name, definition in specs
    ]
    evidence = [
        EvidenceRecord(
            application_id=application_id,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            object_id=item.object_id,
            fact_type=f"{item.object_type.value}_definition",
            location=item.name,
            observation=f"Observed {item.object_type.value} {item.name}",
        )
        for item in initial
    ]
    objects = tuple(
        _object(
            artifact.artifact_id,
            item.object_type,
            item.name,
            item.sanitized_definition,
            (fact.evidence_id,),
        )
        for item, fact in zip(initial, evidence, strict=True)
    )
    by_name = {item.name: item for item in objects}
    evidence_by_name = {
        item.name: fact for item, fact in zip(objects, evidence, strict=True)
    }
    query_object = by_name["qryDeals"]
    query_fact = evidence_by_name["qryDeals"]
    datasource = DatasourceIdentity(
        platform="Microsoft SQL Server",
        driver="ODBC Driver 17 for SQL Server",
        server="SQL01",
        database="Warehouse",
    )
    connection = ConnectionEvidence(
        application_id=application_id,
        artifact_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        object_id=query_object.object_id,
        connection_kind=ConnectionKind.PASS_THROUGH_QUERY,
        sanitized_summary="SQL Server SQL01 Warehouse",
        direct_provenance=ConnectionProvenance.QUERYDEF_CONNECT,
        resolution_status=ResolutionStatus.RESOLVED,
        datasource_id=datasource.datasource_id,
        evidence_ids=(query_fact.evidence_id,),
    )
    query = QueryEvidence(
        object_id=query_object.object_id,
        query_kind=QueryKind.PASS_THROUGH,
        dao_type=112,
        sanitized_sql=query_object.sanitized_definition,
        returns_records=True,
        connection_kind=ConnectionKind.PASS_THROUGH_QUERY,
        connection_id=connection.connection_id,
        evidence_ids=(query_fact.evidence_id,),
    )
    interactions = [
        DatasourceInteraction(
            source_object_id=query_object.object_id,
            operation=DataOperation.READ,
            scope=InteractionScope.EXTERNAL,
            connection_id=connection.connection_id,
            datasource_id=datasource.datasource_id,
            schema_name="dbo",
            object_name="Deal",
            evidence_ids=(query_fact.evidence_id,),
        )
    ]
    if rich:
        form = by_name["frmDeals"]
        interactions.append(
            DatasourceInteraction(
                source_object_id=form.object_id,
                operation=DataOperation.READ,
                scope=InteractionScope.LOCAL,
                local_target_object_id=query_object.object_id,
                evidence_ids=(evidence_by_name["frmDeals"].evidence_id,),
            )
        )
    nodes = [
        DependencyNode(
            application_id=application_id,
            kind=DependencyNodeKind.ACCESS_OBJECT,
            label=item.name,
            artifact_id=artifact.artifact_id,
            object_id=item.object_id,
        )
        for item in objects
    ]
    database_node = DependencyNode(
        application_id=application_id,
        kind=DependencyNodeKind.DATABASE_OBJECT,
        label="SQL01/Warehouse/dbo/Deal",
        datasource_id=datasource.datasource_id,
    )
    nodes.append(database_node)
    node_by_object = {item.object_id: item for item in nodes if item.object_id is not None}
    edges = [
        DependencyEdge(
            source_node_id=node_by_object[query_object.object_id].node_id,
            target_node_id=database_node.node_id,
            relationship=DependencyRelation.READS,
            operation=DataOperation.READ,
            evidence_ids=(query_fact.evidence_id,),
        )
    ]
    if rich:
        for name in (
            "modWorkflow.RunReport",
            "modHelpers",
            "modAudit",
            "frmDeals",
            "rptDeals",
            "mcrStartup",
        ):
            source = by_name[name]
            edges.append(
                DependencyEdge(
                    source_node_id=node_by_object[source.object_id].node_id,
                    target_node_id=node_by_object[query_object.object_id].node_id,
                    relationship=DependencyRelation.CALLS,
                    evidence_ids=(evidence_by_name[name].evidence_id,),
                )
            )
    claims: tuple[OwnerClaim, ...] = ()
    if lifecycle_claim:
        claims = (
            OwnerClaim(
                application_id=application_id,
                field="Lifecycle status",
                value="Retirement approved",
                source="Portfolio owner",
            ),
        )
    all_evidence = list(evidence)
    if aggregate_evidence:
        all_evidence.append(
            EvidenceRecord(
                application_id=application_id,
                artifact_id=artifact.artifact_id,
                artifact_sha256=artifact.sha256,
                fact_type="aggregate_only",
                observation="Aggregate inference without a terminal object",
            )
        )
    tables: tuple[AccessTableEvidence, ...] = ()
    if rich:
        table = by_name["DealCache"]
        tables = (
            AccessTableEvidence(
                object_id=table.object_id,
                is_linked=False,
                evidence_ids=(evidence_by_name["DealCache"].evidence_id,),
            ),
        )
    return ApplicationEvidenceBundle(
        application_id=application_id,
        application_name=f"Application {application_id}",
        inventory_record_ids=(f"inventory-{application_id}",),
        generated_at=NOW,
        artifacts=(artifact,),
        objects=objects,
        connections=(connection,),
        tables=tables,
        queries=(query,),
        datasources=(datasource,),
        interactions=tuple(interactions),
        dependency_nodes=tuple(nodes),
        dependency_edges=tuple(edges),
        evidence=tuple(all_evidence),
        owner_claims=claims,
        coverage=ExtractionCoverage(
            primary_artifact_count=1,
            complete_artifact_count=1,
            partial_artifact_count=0,
            failed_artifact_count=0,
            discovered_object_count=len(objects),
            extracted_object_count=len(objects),
            warning_count=0,
            unresolved_reference_count=0,
        ),
    )


def _profile(bundle: ApplicationEvidenceBundle) -> ApplicationInterpretation:
    evidence_id = bundle.evidence[0].evidence_id
    claim_ids = (bundle.owner_claims[0].claim_id,) if bundle.owner_claims else ()
    return ApplicationInterpretation(
        application_id=bundle.application_id,
        source_bundle_sha256=evidence_bundle_fingerprint(bundle),
        logical_unit_interpretation_ids=(f"unit-{bundle.application_id}",),
        summary="Grounded summary",
        business_purpose="Grounded purpose",
        evidence_ids=(evidence_id,),
        claim_ids=claim_ids,
        provenance=_provenance(),
    )


def _table_only_bundle() -> ApplicationEvidenceBundle:
    original = _bundle("app-tables")
    table_object = next(
        item for item in original.objects if item.object_type == AccessObjectType.TABLE
    )
    table = next(item for item in original.tables if item.object_id == table_object.object_id)
    evidence = tuple(
        item for item in original.evidence if item.object_id == table_object.object_id
    )
    nodes = tuple(
        item for item in original.dependency_nodes if item.object_id == table_object.object_id
    )
    coverage = original.coverage.model_copy(
        update={"discovered_object_count": 1, "extracted_object_count": 1}
    )
    return original.model_copy(
        update={
            "objects": (table_object,),
            "connections": (),
            "tables": (table,),
            "queries": (),
            "datasources": (),
            "interactions": (),
            "dependency_nodes": nodes,
            "dependency_edges": (),
            "evidence": evidence,
            "coverage": coverage,
        }
    )


def test_logical_units_partition_semantic_objects_once_and_keep_data_as_context() -> None:
    bundle = _bundle()

    plans = build_logical_units(bundle)

    assert len(plans) == 7
    assigned = {object_id for plan in plans for object_id in plan.primary_object_ids}
    expected = {
        item.object_id
        for item in bundle.objects
        if item.object_type
        in {
            AccessObjectType.QUERY,
            AccessObjectType.PROCEDURE,
            AccessObjectType.MODULE,
            AccessObjectType.FORM,
            AccessObjectType.REPORT,
            AccessObjectType.MACRO,
        }
    }
    assert assigned == expected
    module_ids = {
        item.object_id
        for item in bundle.objects
        if item.object_type == AccessObjectType.MODULE
    }
    module_plans = [
        item for item in plans if set(item.primary_object_ids) & module_ids
    ]
    assert len(module_plans) == 2
    assert all(item.kind == LogicalUnitKind.VBA_PROCEDURE for item in module_plans)
    table_id = next(
        item.object_id for item in bundle.objects if item.object_type == AccessObjectType.TABLE
    )
    assert table_id not in assigned
    query_plan = next(item for item in plans if item.kind == LogicalUnitKind.QUERY)
    assert all("sanitized_definition" not in item for item in query_plan.context["primary_objects"])
    assert all("sanitized_sql" not in item for item in query_plan.context["query_metadata"])
    assert query_plan.context["authoritative_facts"]
    assert query_plan.context["interactions"]
    assert query_plan.context["dependency_edges"]


def test_vba_modules_split_at_procedure_boundaries_with_declarations_as_context() -> None:
    original = _bundle()
    module = next(
        item for item in original.objects if item.object_type == AccessObjectType.MODULE
    )
    definition = (
        'Option Compare Database\nPrivate Const PREFIX As String = "D"\n'
        "Public Sub First()\nDebug.Print PREFIX\nEnd Sub\n"
        "Private Function Second() As String\nSecond = PREFIX\nEnd Function\n"
    )
    objects = tuple(
        item.model_copy(update={"sanitized_definition": definition})
        if item.object_id == module.object_id
        else item
        for item in original.objects
    )
    bundle = original.model_copy(update={"objects": objects})

    module_plans = [
        item
        for item in build_logical_units(bundle)
        if module.object_id in item.primary_object_ids
    ]

    assert len(module_plans) == 2
    assert all(item.kind == LogicalUnitKind.VBA_PROCEDURE for item in module_plans)
    assert len({item.logical_unit_id for item in module_plans}) == 2
    procedure_texts = {
        source.text
        for plan in module_plans
        for source in plan.definition_sources
        if ".vba_procedure:" in source.source
    }
    assert procedure_texts == {
        "Public Sub First()\nDebug.Print PREFIX\nEnd Sub\n",
        "Private Function Second() As String\nSecond = PREFIX\nEnd Function\n",
    }
    assert all(
        any(source.source.endswith("module_declarations") for source in plan.definition_sources)
        for plan in module_plans
    )


def test_definition_chunks_are_bounded_lossless_and_reduce_every_source() -> None:
    plan = next(
        item for item in build_logical_units(_bundle()) if item.kind == LogicalUnitKind.QUERY
    )

    chunks = build_definition_chunks(plan, max_definition_chars=36)

    assert len(chunks) > 1
    for source in plan.definition_sources:
        reconstructed = "".join(
            fragment.text
            for chunk in chunks
            for fragment in chunk
            if (fragment.object_id, fragment.source) == (source.object_id, source.source)
        )
        assert reconstructed == source.text
    assert all(sum(len(item.text) for item in chunk) <= 36 for chunk in chunks)


def test_small_related_units_are_batched_under_the_measured_prompt_budget() -> None:
    bundle = _bundle()
    provider = _PipelineProvider()

    result = analyze_application_two_stage(
        bundle,
        provider,
        provenance=_provenance(),
        max_definition_chars=1_000,
        max_output_tokens=256,
    )

    assert result.status == InterpretationRunStatus.COMPLETE
    batch_calls = [
        item
        for item in provider.calls
        if item["payload"]["stage"] == "logical_unit_batch"
    ]
    assert batch_calls
    assert any(len(item["payload"]["units"]) > 1 for item in batch_calls)
    batched_ids = {
        unit["expected_output"]["logical_unit_id"]
        for item in batch_calls
        for unit in item["payload"]["units"]
    }
    assert batched_ids <= {item.logical_unit_id for item in result.unit_results}
    assert all(
        item.interpretation is not None
        for item in result.unit_results
        if item.logical_unit_id in batched_ids
    )


def test_two_stage_pipeline_consumes_all_units_graph_datasources_coverage_and_claims() -> None:
    bundle = _bundle()
    provider = _PipelineProvider(repair_unknown_once=True)

    result = analyze_application_two_stage(
        bundle,
        provider,
        provenance=_provenance(),
        max_definition_chars=60,
        max_output_tokens=256,
    )

    assert isinstance(result, ApplicationAnalysisResult)
    assert result.status == InterpretationRunStatus.COMPLETE
    assert result.interpretation is not None
    assert all(
        item.status
        in {InterpretationRunStatus.COMPLETE, InterpretationRunStatus.ABSTAINED}
        for item in result.unit_results
    )
    assert set(result.interpretation.logical_unit_interpretation_ids) == {
        item.interpretation.interpretation_id
        for item in result.unit_results
        if item.interpretation is not None
    }
    assert any(item.chunk_count > 1 for item in result.unit_results)
    assert any(call["repaired"] for call in provider.calls)
    app_payload = next(
        call["payload"]
        for call in provider.calls
        if call["payload"]["stage"] == "application_synthesis"
    )
    assert len(app_payload["logical_unit_interpretations"]) == len(result.unit_results)
    assert app_payload["dependency_graph"]["edges"]
    assert app_payload["datasources"]
    assert app_payload["coverage"]
    assert app_payload["owner_claims"]


def test_tokenizer_budget_splits_at_boundaries_without_omitting_definition_text() -> None:
    bundle = _bundle()
    provider = _PipelineProvider(max_definition_chars_per_prompt=70)

    result = analyze_application_two_stage(
        bundle,
        provider,
        provenance=_provenance(),
        max_definition_chars=10_000,
        max_output_tokens=256,
    )

    assert result.interpretation is not None
    query_calls = [
        item
        for item in provider.calls
        if item["payload"]["stage"] == "logical_unit"
        and item["payload"]["logical_unit"]["kind"] == "query"
    ]
    assert len(query_calls) > 1
    reconstructed = "".join(
        fragment["text"]
        for call in query_calls
        for fragment in call["payload"]["definitions"]
    )
    query_plan = next(
        item for item in build_logical_units(bundle) if item.kind == LogicalUnitKind.QUERY
    )
    assert reconstructed == "".join(item.text for item in query_plan.definition_sources)
    assert all(
        sum(len(item["text"]) for item in call["payload"]["definitions"]) <= 70
        for call in query_calls
    )
    assert all(
        len(call["payload"]["chunk_interpretations"]) <= 2
        for call in provider.calls
        if call["payload"]["stage"] == "logical_unit_reduction"
    )


def test_application_synthesis_logically_reduces_oversized_all_unit_payload() -> None:
    bundle = _bundle()
    provider = _PipelineProvider(max_application_inputs_per_prompt=2)

    result = analyze_application_two_stage(
        bundle,
        provider,
        provenance=_provenance(),
        max_definition_chars=1_000,
    )

    assert result.status == InterpretationRunStatus.COMPLETE
    assert result.interpretation is not None
    assert set(result.interpretation.logical_unit_interpretation_ids) == {
        item.interpretation.interpretation_id
        for item in result.unit_results
        if item.interpretation is not None
    }
    stages = [item["payload"]["stage"] for item in provider.calls]
    assert "application_synthesis_batch" in stages
    assert "application_synthesis_reduction" in stages
    assert len(result.cache_keys) > 1


def test_table_only_application_runs_stage_two_with_empty_logical_unit_registry() -> None:
    bundle = _table_only_bundle()
    provider = _PipelineProvider()

    result = analyze_application_two_stage(
        bundle,
        provider,
        provenance=_provenance(),
    )

    assert result.status == InterpretationRunStatus.COMPLETE
    assert result.unit_results == ()
    assert result.interpretation is not None
    assert result.interpretation.logical_unit_interpretation_ids == ()
    payload = provider.calls[0]["payload"]
    assert payload["stage"] == "application_synthesis"
    assert payload["tables"]
    assert payload["coverage"]


def test_failed_unit_is_explicit_and_application_uses_every_remaining_valid_unit() -> None:
    bundle = _bundle()
    provider = _PipelineProvider(fail_kind="macro")

    result = analyze_application_two_stage(
        bundle,
        provider,
        provenance=_provenance(),
        max_definition_chars=1_000,
    )

    assert result.status == InterpretationRunStatus.PARTIAL
    failed = [item for item in result.unit_results if item.status == InterpretationRunStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].failure is not None
    assert result.interpretation is not None
    valid_ids = {
        item.interpretation.interpretation_id
        for item in result.unit_results
        if item.interpretation is not None
    }
    assert set(result.interpretation.logical_unit_interpretation_ids) == valid_ids
    payload = result.to_payload()
    json.dumps(payload)
    failed_payloads = [item for item in payload["unit_results"] if item["status"] == "failed"]
    assert len(failed_payloads) == 1
    assert failed_payloads[0]["reason"]
    assert payload["cache_keys"]


def test_application_rejects_aggregate_only_terminal_citation_after_repair() -> None:
    bundle = _bundle(aggregate_evidence=True)
    aggregate_id = next(
        item.evidence_id for item in bundle.evidence if item.fact_type == "aggregate_only"
    )
    provider = _PipelineProvider(application_evidence_id=aggregate_id)

    result = analyze_application_two_stage(
        bundle,
        provider,
        provenance=_provenance(),
        max_definition_chars=1_000,
    )

    assert result.status == InterpretationRunStatus.FAILED
    assert result.interpretation is None
    assert result.failure is not None
    application_calls = [
        item for item in provider.calls if item["payload"]["stage"] == "application_synthesis"
    ]
    assert len(application_calls) == 2


def test_cache_fingerprints_are_pure_and_independently_invalidated() -> None:
    provenance = _provenance()
    content = content_fingerprint({"value": 1})
    schema = schema_fingerprint(ApplicationInterpretation)
    prompt = prompt_fingerprint(
        prompt_version=APPLICATION_PROMPT_VERSION,
        system_prompt="system",
    )
    model = model_fingerprint(provenance)
    first = inference_cache_key(
        content_sha256=content,
        schema_sha256=schema,
        prompt_sha256=prompt,
        model_sha256=model,
    )
    repeated = inference_cache_key(
        content_sha256=content,
        schema_sha256=schema,
        prompt_sha256=prompt,
        model_sha256=model,
    )
    changed = inference_cache_key(
        content_sha256=content_fingerprint({"value": 2}),
        schema_sha256=schema,
        prompt_sha256=prompt_fingerprint(
            prompt_version=UNIT_PROMPT_VERSION,
            system_prompt="system",
        ),
        model_sha256=model,
    )

    assert first == repeated
    assert first.cache_key != changed.cache_key


def test_validated_outputs_are_reused_per_inference_without_prompt_persistence() -> None:
    bundle = _bundle()
    cache = _MemoryInferenceCache()
    first_provider = _PipelineProvider()

    first = analyze_application_two_stage(
        bundle,
        first_provider,
        provenance=_provenance(),
        cache=cache,
    )
    second_provider = _PipelineProvider()
    second = analyze_application_two_stage(
        bundle,
        second_provider,
        provenance=_provenance(),
        cache=cache,
    )

    assert first.interpretation == second.interpretation
    assert first.unit_results == second.unit_results
    assert first_provider.calls
    assert second_provider.calls == []
    assert cache.values
    assert all("prompt" not in value for value in cache.values.values())


def test_output_token_policy_change_invalidates_each_inference_cache_key() -> None:
    bundle = _bundle(rich=False)
    cache = _MemoryInferenceCache()
    analyze_application_two_stage(
        bundle,
        _PipelineProvider(),
        provenance=_provenance(),
        cache=cache,
        max_output_tokens=256,
    )
    changed_policy_provider = _PipelineProvider()

    analyze_application_two_stage(
        bundle,
        changed_policy_provider,
        provenance=_provenance(),
        cache=cache,
        max_output_tokens=128,
    )

    assert changed_policy_provider.calls


def test_model_authored_secret_values_are_redacted_before_cache_and_persistence() -> None:
    cache = _MemoryInferenceCache()

    result = analyze_application_two_stage(
        _bundle(),
        _PipelineProvider(leaked_text="Password=model-output-secret"),
        provenance=_provenance(),
        cache=cache,
    )

    serialized = json.dumps(result.to_payload(), sort_keys=True)
    cached = json.dumps(cache.values, sort_keys=True)
    assert "model-output-secret" not in serialized
    assert "model-output-secret" not in cached
    assert "<redacted>" in serialized


def test_source_bundle_hash_uses_stable_v2_fingerprint() -> None:
    bundle = _bundle(rich=False)

    result = analyze_application_two_stage(
        bundle,
        _PipelineProvider(),
        provenance=_provenance(),
    )

    assert result.source_bundle_sha256 == evidence_bundle_fingerprint(bundle)
    assert result.interpretation is not None
    assert result.interpretation.source_bundle_sha256 == evidence_bundle_fingerprint(bundle)


def test_portfolio_stage_is_candidate_closed_and_bounded() -> None:
    first = _bundle("app-1", rich=False)
    second = _bundle("app-2", rich=False)
    profiles = (_profile(first), _profile(second))
    candidate = PortfolioCandidate(
        candidate_type=PortfolioCandidateType.SHARED_ENDPOINT,
        application_ids=("app-1", "app-2"),
        evidence_ids=(first.evidence[0].evidence_id, second.evidence[0].evidence_id),
        basis_ids=(first.datasources[0].datasource_id,),
        score=0.9,
        policy_version="candidate-policy-v1",
    )
    provider = _PipelineProvider()

    result = analyze_portfolio_candidates(
        (candidate,),
        profiles,
        (first, second),
        provider,
        provenance=_provenance(),
        max_candidates_per_batch=1,
    )

    assert result.status == PortfolioRunStatus.COMPLETE
    assert result.analysis is not None
    assert result.batch_count == 1
    assert result.analysis.findings[0].application_ids == ("app-1", "app-2")
    payload = provider.calls[0]["payload"]
    assert payload["candidates"] == [candidate.model_dump(mode="json")]
    assert payload["technical_basis"] == [first.datasources[0].model_dump(mode="json")]


def test_portfolio_rejects_invented_membership_after_one_repair() -> None:
    first = _bundle("app-1", rich=False)
    second = _bundle("app-2", rich=False)
    candidate = PortfolioCandidate(
        candidate_type=PortfolioCandidateType.SEMANTIC_OVERLAP,
        application_ids=("app-1", "app-2"),
        evidence_ids=(first.evidence[0].evidence_id, second.evidence[0].evidence_id),
        score=0.7,
        policy_version="candidate-policy-v1",
    )
    provider = _PipelineProvider(portfolio_mode="invent_membership")

    result = analyze_portfolio_candidates(
        (candidate,),
        (_profile(first), _profile(second)),
        (first, second),
        provider,
        provenance=_provenance(),
    )

    assert result.status == PortfolioRunStatus.INCOMPLETE
    assert result.analysis is None
    assert len(provider.calls) == 2


def test_portfolio_rejects_unknown_evidence_after_one_repair() -> None:
    first = _bundle("app-1", rich=False)
    second = _bundle("app-2", rich=False)
    candidate = PortfolioCandidate(
        candidate_type=PortfolioCandidateType.SEMANTIC_OVERLAP,
        application_ids=("app-1", "app-2"),
        evidence_ids=(first.evidence[0].evidence_id, second.evidence[0].evidence_id),
        score=0.7,
        policy_version="candidate-policy-v1",
    )
    provider = _PipelineProvider(portfolio_mode="invent_evidence")

    result = analyze_portfolio_candidates(
        (candidate,),
        (_profile(first), _profile(second)),
        (first, second),
        provider,
        provenance=_provenance(),
    )

    assert result.status == PortfolioRunStatus.INCOMPLETE
    assert result.analysis is None
    assert len(provider.calls) == 2


def test_portfolio_rejects_unknown_technical_basis_before_inference() -> None:
    first = _bundle("app-1", rich=False)
    second = _bundle("app-2", rich=False)
    candidate = PortfolioCandidate(
        candidate_type=PortfolioCandidateType.SHARED_ENDPOINT,
        application_ids=("app-1", "app-2"),
        evidence_ids=(first.evidence[0].evidence_id, second.evidence[0].evidence_id),
        basis_ids=("datasource-invented",),
        score=0.9,
        policy_version="candidate-policy-v1",
    )
    provider = _PipelineProvider()

    result = analyze_portfolio_candidates(
        (candidate,),
        (_profile(first), _profile(second)),
        (first, second),
        provider,
        provenance=_provenance(),
    )

    assert result.status == PortfolioRunStatus.INCOMPLETE
    assert result.analysis is None
    assert provider.calls == []


def test_retirement_requires_lifecycle_claim_for_every_candidate_member() -> None:
    first = _bundle("app-1", rich=False, lifecycle_claim=True)
    second = _bundle("app-2", rich=False, lifecycle_claim=False)
    candidate = PortfolioCandidate(
        candidate_type=PortfolioCandidateType.SEMANTIC_OVERLAP,
        application_ids=("app-1", "app-2"),
        evidence_ids=(first.evidence[0].evidence_id, second.evidence[0].evidence_id),
        score=0.8,
        policy_version="candidate-policy-v1",
    )
    provider = _PipelineProvider(portfolio_mode="retire")

    result = analyze_portfolio_candidates(
        (candidate,),
        (_profile(first), _profile(second)),
        (first, second),
        provider,
        provenance=_provenance(),
    )

    assert result.status == PortfolioRunStatus.INCOMPLETE
    assert result.analysis is None


def test_portfolio_schema_valid_abstention_is_complete() -> None:
    first = _bundle("app-1", rich=False)
    second = _bundle("app-2", rich=False)
    candidate = PortfolioCandidate(
        candidate_type=PortfolioCandidateType.SEMANTIC_OVERLAP,
        application_ids=("app-1", "app-2"),
        evidence_ids=(first.evidence[0].evidence_id, second.evidence[0].evidence_id),
        score=0.5,
        policy_version="candidate-policy-v1",
    )

    result = analyze_portfolio_candidates(
        (candidate,),
        (_profile(first), _profile(second)),
        (first, second),
        _PipelineProvider(portfolio_mode="abstain"),
        provenance=_provenance(),
    )

    assert result.status == PortfolioRunStatus.COMPLETE
    assert result.analysis is not None
    assert result.analysis.findings == ()


def test_portfolio_batches_adapt_to_exact_tokenizer_measurement() -> None:
    first = _bundle("app-1", rich=False)
    second = _bundle("app-2", rich=False)
    evidence_ids = (first.evidence[0].evidence_id, second.evidence[0].evidence_id)
    candidates = tuple(
        PortfolioCandidate(
            candidate_type=candidate_type,
            application_ids=("app-1", "app-2"),
            evidence_ids=evidence_ids,
            score=score,
            policy_version="candidate-policy-v1",
        )
        for candidate_type, score in (
            (PortfolioCandidateType.SEMANTIC_OVERLAP, 0.5),
            (PortfolioCandidateType.EXACT_CODE, 0.7),
            (PortfolioCandidateType.SHARED_FILE, 0.9),
        )
    )
    provider = _PipelineProvider(max_portfolio_candidates_per_prompt=1)

    result = analyze_portfolio_candidates(
        candidates,
        (_profile(first), _profile(second)),
        (first, second),
        provider,
        provenance=_provenance(),
        max_candidates_per_batch=25,
    )

    assert result.status == PortfolioRunStatus.COMPLETE
    assert result.batch_count == 3
    candidate_calls = [
        item
        for item in provider.calls
        if item["payload"]["stage"] == "portfolio_candidate_interpretation"
    ]
    assert len(candidate_calls) == 3
    submitted_ids = {
        candidate["candidate_id"]
        for call in candidate_calls
        for candidate in call["payload"]["candidates"]
    }
    assert submitted_ids == {item.candidate_id for item in candidates}
