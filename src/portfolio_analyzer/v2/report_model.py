"""Build the single normalized model consumed by every V2 report renderer."""

from __future__ import annotations

from datetime import datetime

from portfolio_analyzer.v2.candidates import (
    FROZEN_CANDIDATE_POLICY,
    SEMANTIC_SIMILARITY_DISABLED_WARNING,
)
from portfolio_analyzer.v2.fingerprints import (
    analysis_fingerprint,
    evidence_bundle_fingerprint,
    interpretation_fingerprint,
)
from portfolio_analyzer.v2.models import (
    PARTIAL_REPORT_WATERMARK,
    ApplicationEvidenceBundle,
    ApplicationProfile,
    DataAccess,
    DataOperation,
    ExtractionStatus,
    InteractionScope,
    LinkedTableView,
    PassThroughQueryView,
    PortfolioCandidate,
    PortfolioInterpretation,
    PortfolioReportModel,
    QueryKind,
    ReportApplicationEntry,
    ReportCoverageEntry,
    ReportDataAccessEntry,
    ReportDependencyEdgeEntry,
    ReportInventoryExclusion,
    ReportLineageTarget,
    ReportObjectEntry,
    ReportOmission,
    ReportOmissionStage,
    ReportQueryEntry,
    ReportReviewRecord,
    ReportStatus,
    ReportTableEntry,
    ReportUnresolvedEntry,
    ResolutionStatus,
)
from portfolio_analyzer.v2.workflow import InventoryExclusion


class IncompleteReportError(ValueError):
    """Raised when the report source set is incomplete without an explicit override."""


def build_portfolio_report_model(
    bundles: tuple[ApplicationEvidenceBundle, ...],
    profiles: tuple[ApplicationProfile, ...],
    portfolio: PortfolioInterpretation | None,
    candidates: tuple[PortfolioCandidate, ...],
    *,
    generated_at: datetime,
    reviews: tuple[ReportReviewRecord, ...] = (),
    omissions: tuple[ReportOmission, ...] = (),
    inventory_exclusions: tuple[InventoryExclusion, ...] = (),
    allow_partial: bool = False,
    excluded_application_ids: tuple[str, ...] = (),
) -> PortfolioReportModel:
    """Validate lineage and assemble one renderer-independent portfolio report model."""

    if not bundles:
        raise ValueError("a portfolio report requires at least one evidence bundle")
    bundle_by_application = _unique_bundles(bundles)
    profile_by_application = _unique_profiles(profiles)
    unknown_profiles = set(profile_by_application) - set(bundle_by_application)
    if unknown_profiles:
        raise ValueError(
            "profiles reference unknown applications: " + ", ".join(sorted(unknown_profiles))
        )
    included_ids = set(bundle_by_application)
    excluded_ids = set(excluded_application_ids)
    if included_ids & excluded_ids:
        raise ValueError("excluded applications cannot also be included in the report")

    all_evidence_ids: set[str] = set()
    entries: list[ReportApplicationEntry] = []
    partial_reasons: list[str] = []
    coverage_qualifications: list[str] = []
    if FROZEN_CANDIDATE_POLICY.semantic_overlap_min_score is None:
        coverage_qualifications.append(SEMANTIC_SIMILARITY_DISABLED_WARNING)
    structured_omissions = list(omissions)
    accepted_profiles: list[ApplicationProfile] = []
    for application_id, bundle in sorted(bundle_by_application.items()):
        evidence_ids = {item.evidence_id for item in bundle.evidence}
        all_evidence_ids.update(evidence_ids)
        profile = profile_by_application.get(application_id)
        bundle_sha256 = evidence_bundle_fingerprint(bundle)
        if profile is not None:
            if profile.source_bundle_sha256 != bundle_sha256:
                partial_reasons.append(f"stale interpretation: {application_id}")
                structured_omissions.append(
                    ReportOmission(
                        application_id=application_id,
                        stage=ReportOmissionStage.ANALYZE,
                        reason="Application interpretation is stale for current evidence",
                    )
                )
                profile = None
            else:
                unknown_evidence = set(profile.evidence_ids) - evidence_ids
                if unknown_evidence:
                    raise ValueError(
                        f"application profile cites unknown evidence: {application_id}"
                    )
                accepted_profiles.append(profile)
        extraction_status = _bundle_status(bundle)
        notes = [warning for artifact in bundle.artifacts for warning in artifact.warnings]
        if profile is None:
            if not any(
                reason == f"stale interpretation: {application_id}"
                for reason in partial_reasons
            ):
                partial_reasons.append(f"missing interpretation: {application_id}")
                structured_omissions.append(
                    ReportOmission(
                        application_id=application_id,
                        stage=ReportOmissionStage.ANALYZE,
                        reason="Application interpretation unavailable",
                    )
                )
            notes.append("Application interpretation unavailable")
        if extraction_status == ExtractionStatus.PARTIAL:
            qualification = (
                f"qualified extraction coverage: {application_id}; unsupported absence "
                "claims are suppressed"
            )
            coverage_qualifications.append(qualification)
            notes.append(qualification)
        elif extraction_status == ExtractionStatus.FAILED:
            partial_reasons.append(f"failed extraction: {application_id}")
            structured_omissions.append(
                ReportOmission(
                    application_id=application_id,
                    stage=ReportOmissionStage.EXTRACT,
                    reason="Application extraction failed",
                )
            )
        entries.append(
            ReportApplicationEntry(
                application_id=application_id,
                application_name=bundle.application_name,
                evidence_bundle_sha256=bundle_sha256,
                interpretation_sha256=(
                    interpretation_fingerprint(profile) if profile is not None else None
                ),
                status=extraction_status,
                summary=profile.summary if profile is not None else None,
                business_purpose=(
                    profile.business_purpose if profile is not None else None
                ),
                major_workflows=profile.major_workflows if profile is not None else (),
                capabilities=profile.capabilities if profile is not None else (),
                artifact_count=len(bundle.artifacts),
                object_count=len(bundle.objects),
                table_count=len(bundle.tables),
                query_count=len(bundle.queries),
                datasource_count=len(bundle.datasources),
                connection_count=len(bundle.connections),
                interaction_count=len(bundle.interactions),
                evidence_count=len(bundle.evidence),
                unresolved_reference_count=len(bundle.unresolved_references),
                coverage_notes=tuple(notes),
            )
        )

    if excluded_ids:
        partial_reasons.append("applications were omitted from report input")
        explained_application_ids = {
            item.application_id
            for item in structured_omissions
            if item.application_id is not None and item.logical_unit_id is None
        }
        structured_omissions.extend(
            ReportOmission(
                application_id=application_id,
                stage=ReportOmissionStage.ANALYZE,
                reason="Application analysis was omitted from report input",
            )
            for application_id in excluded_ids - explained_application_ids
        )
    accepted_profiles_tuple = tuple(accepted_profiles)
    profile_hashes = {
        interpretation_fingerprint(profile) for profile in accepted_profiles_tuple
    }
    effective_portfolio = portfolio
    if effective_portfolio is None:
        partial_reasons.append("portfolio interpretation unavailable")
        structured_omissions.append(
            ReportOmission(
                stage=ReportOmissionStage.ANALYZE,
                reason="Portfolio interpretation unavailable",
            )
        )
    elif set(effective_portfolio.application_interpretation_sha256s) != profile_hashes:
        partial_reasons.append("portfolio interpretation is stale")
        structured_omissions.append(
            ReportOmission(
                stage=ReportOmissionStage.ANALYZE,
                reason="Portfolio interpretation is stale for application interpretations",
            )
        )
        effective_portfolio = None

    for candidate in candidates:
        unknown_applications = set(candidate.application_ids) - included_ids
        if unknown_applications:
            raise ValueError("portfolio candidate references an unknown application")
        if not set(candidate.evidence_ids).issubset(all_evidence_ids):
            raise ValueError("portfolio candidate cites unknown evidence")
    if effective_portfolio is not None:
        for finding in effective_portfolio.findings:
            if not set(finding.application_ids).issubset(included_ids):
                raise ValueError("portfolio finding references an unknown application")
            if not set(finding.evidence_ids).issubset(all_evidence_ids):
                raise ValueError("portfolio finding cites unknown evidence")

    structured_omissions = list(
        {item.omission_id: item for item in structured_omissions}.values()
    )
    if structured_omissions and not partial_reasons:
        partial_reasons.append("structured report omissions are present")
    if partial_reasons and not allow_partial:
        raise IncompleteReportError(
            "report inputs are incomplete; pass allow_partial=True to continue: "
            + "; ".join(sorted(partial_reasons))
        )
    status = ReportStatus.PARTIAL if partial_reasons else ReportStatus.COMPLETE
    return PortfolioReportModel(
        status=status,
        generated_at=generated_at,
        analysis_fingerprint=analysis_fingerprint(
            bundles, accepted_profiles_tuple, effective_portfolio
        ),
        applications=tuple(entries),
        observed_facts=tuple(
            fact for bundle in bundles for fact in bundle.evidence
        ),
        owner_claims=tuple(
            claim for bundle in bundles for claim in bundle.owner_claims
        ),
        application_profiles=accepted_profiles_tuple,
        review_records=reviews,
        unresolved_references=tuple(
            ReportUnresolvedEntry(
                application_id=bundle.application_id,
                unresolved=unresolved,
            )
            for bundle in bundles
            for unresolved in bundle.unresolved_references
        ),
        coverage=tuple(
            ReportCoverageEntry(
                application_id=bundle.application_id,
                coverage=bundle.coverage,
            )
            for bundle in bundles
        ),
        pass_through_queries=tuple(
            view for bundle in bundles for view in _pass_through_views(bundle)
        ),
        linked_tables=tuple(
            view for bundle in bundles for view in _linked_table_views(bundle)
        ),
        object_registry=tuple(
            ReportObjectEntry(
                application_id=bundle.application_id,
                access_object=access_object,
            )
            for bundle in bundles
            for access_object in bundle.objects
        ),
        table_registry=tuple(
            ReportTableEntry(
                application_id=bundle.application_id,
                table=table,
            )
            for bundle in bundles
            for table in bundle.tables
        ),
        query_registry=tuple(
            ReportQueryEntry(
                application_id=bundle.application_id,
                query=query,
            )
            for bundle in bundles
            for query in bundle.queries
        ),
        connection_registry=tuple(
            connection for bundle in bundles for connection in bundle.connections
        ),
        datasource_registry=tuple(
            {
                datasource.datasource_id: datasource
                for bundle in bundles
                for datasource in bundle.datasources
            }.values()
        ),
        data_access=tuple(
            ReportDataAccessEntry(
                application_id=bundle.application_id,
                data_access=access,
            )
            for bundle in bundles
            for access in bundle.interactions
        ),
        dependency_nodes=tuple(
            node for bundle in bundles for node in bundle.dependency_nodes
        ),
        dependency_edges=tuple(
            ReportDependencyEdgeEntry(
                application_id=bundle.application_id,
                dependency_edge=edge,
            )
            for bundle in bundles
            for edge in bundle.dependency_edges
        ),
        inventory_exclusions=tuple(
            ReportInventoryExclusion(
                application_id=item.application_id,
                application_name=item.application_name,
                filename=item.filename,
                source_locator=item.source_locator,
                status=item.status,
                reason=item.reason,
            )
            for item in inventory_exclusions
        ),
        omissions=tuple(structured_omissions),
        candidates=candidates,
        portfolio_findings=(
            effective_portfolio.findings if effective_portfolio is not None else ()
        ),
        portfolio_analysis_sha256=(
            interpretation_fingerprint(effective_portfolio)
            if effective_portfolio is not None
            else None
        ),
        excluded_application_ids=tuple(excluded_ids),
        warnings=tuple((*partial_reasons, *coverage_qualifications)),
        partial_watermark=(
            PARTIAL_REPORT_WATERMARK
            if status == ReportStatus.PARTIAL
            else None
        ),
        absence_claims_suppressed=(
            status == ReportStatus.PARTIAL or bool(coverage_qualifications)
        ),
    )


def _bundle_status(bundle: ApplicationEvidenceBundle) -> ExtractionStatus:
    if bundle.coverage.failed_artifact_count == bundle.coverage.primary_artifact_count:
        return ExtractionStatus.FAILED
    if (
        bundle.coverage.partial_artifact_count
        or bundle.coverage.failed_artifact_count
    ):
        return ExtractionStatus.PARTIAL
    return ExtractionStatus.COMPLETE


def _pass_through_views(
    bundle: ApplicationEvidenceBundle,
) -> tuple[PassThroughQueryView, ...]:
    objects = {item.object_id: item for item in bundle.objects}
    connections = {item.connection_id: item for item in bundle.connections}
    datasources = {item.datasource_id: item for item in bundle.datasources}
    output: list[PassThroughQueryView] = []
    for query in bundle.queries:
        if query.query_kind not in {
            QueryKind.PASS_THROUGH,
            QueryKind.PASS_THROUGH_BULK,
        } or query.connection_id is None:
            continue
        connection = connections[query.connection_id]
        datasource = (
            datasources.get(connection.datasource_id)
            if connection.datasource_id is not None
            else None
        )
        accesses = tuple(
            item for item in bundle.interactions if item.source_object_id == query.object_id
        )
        evidence_ids = tuple(
            {
                *query.evidence_ids,
                *connection.evidence_ids,
                *(evidence_id for item in accesses for evidence_id in item.evidence_ids),
            }
        )
        if not evidence_ids:
            raise ValueError("pass-through query report view has no observed evidence")
        source_object = objects[query.object_id]
        targets = tuple(
            _lineage_target(
                bundle,
                source_object.artifact_id,
                query.object_id,
                access,
                fallback_evidence_ids=tuple(
                    {*query.evidence_ids, *connection.evidence_ids}
                ),
            )
            for access in accesses
        )
        output.append(
            PassThroughQueryView(
                application_id=bundle.application_id,
                artifact_id=source_object.artifact_id,
                query_object_id=query.object_id,
                query_name=source_object.name,
                query_kind=query.query_kind,
                connection_id=connection.connection_id,
                connection_kind=connection.connection_kind,
                direct_provenance=connection.direct_provenance,
                datasource_id=connection.datasource_id,
                resolution_status=connection.resolution_status,
                resolution_provenance=connection.resolution_provenance,
                resolution_warnings=connection.resolution_warnings,
                platform=datasource.platform if datasource is not None else None,
                driver=datasource.driver if datasource is not None else None,
                dsn=datasource.dsn if datasource is not None else None,
                server=datasource.server if datasource is not None else None,
                database=datasource.database if datasource is not None else None,
                operations=tuple(item.operation for item in accesses),
                catalogs=tuple(
                    item.catalog for item in accesses if item.catalog is not None
                ),
                schemas=tuple(
                    item.schema_name for item in accesses if item.schema_name is not None
                ),
                object_names=tuple(
                    item.object_name for item in accesses if item.object_name is not None
                ),
                targets=targets,
                evidence_ids=evidence_ids,
            )
        )
    return tuple(output)


def _linked_table_views(
    bundle: ApplicationEvidenceBundle,
) -> tuple[LinkedTableView, ...]:
    objects = {item.object_id: item for item in bundle.objects}
    connections = {item.connection_id: item for item in bundle.connections}
    datasources = {item.datasource_id: item for item in bundle.datasources}
    output: list[LinkedTableView] = []
    for table in bundle.tables:
        if not table.is_linked or table.connection_id is None:
            continue
        connection = connections[table.connection_id]
        datasource = (
            datasources.get(connection.datasource_id)
            if connection.datasource_id is not None
            else None
        )
        accesses = tuple(
            item for item in bundle.interactions if item.source_object_id == table.object_id
        )
        evidence_ids = tuple(
            {
                *table.evidence_ids,
                *connection.evidence_ids,
                *(evidence_id for item in accesses for evidence_id in item.evidence_ids),
            }
        )
        if not evidence_ids:
            raise ValueError("linked-table report view has no observed evidence")
        source_object = objects[table.object_id]
        targets = tuple(
            _lineage_target(
                bundle,
                source_object.artifact_id,
                table.object_id,
                access,
                fallback_evidence_ids=tuple(
                    {*table.evidence_ids, *connection.evidence_ids}
                ),
            )
            for access in accesses
        )
        if not targets and table.source_table_name is not None:
            catalog, target_database, schema_name, object_name = _external_name_parts(
                table.source_table_name
            )
            targets = (
                ReportLineageTarget(
                    application_id=bundle.application_id,
                    artifact_id=source_object.artifact_id,
                    source_object_id=table.object_id,
                    operation=DataOperation.UNKNOWN,
                    scope=(
                        InteractionScope.EXTERNAL
                        if connection.resolution_status == ResolutionStatus.RESOLVED
                        and connection.datasource_id is not None
                        else InteractionScope.UNRESOLVED
                    ),
                    catalog=catalog,
                    database=(
                        target_database
                        or (datasource.database if datasource is not None else None)
                    ),
                    schema_name=schema_name,
                    object_name=object_name,
                    evidence_ids=evidence_ids,
                ),
            )
        output.append(
            LinkedTableView(
                application_id=bundle.application_id,
                artifact_id=source_object.artifact_id,
                table_object_id=table.object_id,
                table_name=source_object.name,
                object_kind=source_object.object_type,
                source_table_name=table.source_table_name,
                connection_id=connection.connection_id,
                connection_kind=connection.connection_kind,
                direct_provenance=connection.direct_provenance,
                datasource_id=connection.datasource_id,
                resolution_status=connection.resolution_status,
                resolution_provenance=connection.resolution_provenance,
                resolution_warnings=connection.resolution_warnings,
                platform=datasource.platform if datasource is not None else None,
                driver=datasource.driver if datasource is not None else None,
                dsn=datasource.dsn if datasource is not None else None,
                server=datasource.server if datasource is not None else None,
                database=datasource.database if datasource is not None else None,
                operations=tuple(item.operation for item in targets),
                catalogs=tuple(
                    item.catalog for item in targets if item.catalog is not None
                ),
                schemas=tuple(
                    item.schema_name for item in targets if item.schema_name is not None
                ),
                object_names=tuple(
                    item.object_name for item in targets if item.object_name is not None
                ),
                targets=targets,
                evidence_ids=evidence_ids,
            )
        )
    return tuple(output)


def _lineage_target(
    bundle: ApplicationEvidenceBundle,
    artifact_id: str,
    source_object_id: str,
    access: DataAccess,
    *,
    fallback_evidence_ids: tuple[str, ...],
) -> ReportLineageTarget:
    return ReportLineageTarget(
        application_id=bundle.application_id,
        artifact_id=artifact_id,
        source_object_id=source_object_id,
        interaction_id=access.interaction_id,
        operation=access.operation,
        scope=access.scope,
        local_target_object_id=access.local_target_object_id,
        catalog=access.catalog,
        database=access.database,
        schema_name=access.schema_name,
        object_name=access.object_name,
        evidence_ids=access.evidence_ids or fallback_evidence_ids,
    )


def _external_name_parts(
    value: str,
) -> tuple[str | None, str | None, str | None, str]:
    parts = [item.strip() for item in value.split(".") if item.strip()]
    if not parts:
        return None, None, None, "<unknown object>"
    if len(parts) == 1:
        return None, None, None, parts[0]
    if len(parts) == 2:
        return None, None, parts[0], parts[1]
    if len(parts) == 3:
        return None, parts[0], parts[1], parts[2]
    return ".".join(parts[:-3]), parts[-3], parts[-2], parts[-1]


def _unique_bundles(
    values: tuple[ApplicationEvidenceBundle, ...],
) -> dict[str, ApplicationEvidenceBundle]:
    output: dict[str, ApplicationEvidenceBundle] = {}
    for value in values:
        if value.application_id in output:
            raise ValueError(f"duplicate evidence bundle: {value.application_id}")
        output[value.application_id] = value
    return output


def _unique_profiles(
    values: tuple[ApplicationProfile, ...],
) -> dict[str, ApplicationProfile]:
    output: dict[str, ApplicationProfile] = {}
    for value in values:
        if value.application_id in output:
            raise ValueError(f"duplicate application profile: {value.application_id}")
        output[value.application_id] = value
    return output
