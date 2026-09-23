"""Discover overlapping groups from repeated evidence, without a role/solution taxonomy."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from portfolio_analyzer.models import (
    AnalysisCoverage,
    Confidence,
    Datasource,
    Evidence,
    PortfolioTheme,
    SemanticApplicationIR,
    SemanticApplicationProfile,
    SemanticSource,
    StagedArtifact,
    ThemeLocation,
)


@dataclass
class _Feature:
    family: str
    label: str
    locations: list[ThemeLocation] = field(default_factory=list)
    operations: set[str] = field(default_factory=set)


def discover_themes(
    profiles: Iterable[SemanticApplicationProfile] = (),
    sources: Iterable[SemanticSource] = (),
    application_irs: Iterable[SemanticApplicationIR] = (),
    evidence: Iterable[Evidence] = (),
    datasources: Iterable[Datasource] = (),
    coverage: Iterable[AnalysisCoverage] = (),
    artifacts: Iterable[StagedArtifact] = (),
) -> list[PortfolioTheme]:
    """Group exact supporting application sets; never merge by transitive overlap.

    Names and roles alone are not grouping features. Local table names are scoped to
    their artifact; only confirmed external identities can establish shared data.
    Matching code is a reuse candidate, not proof of equivalent business semantics.
    """
    profiles = list(profiles)
    sources = list(sources)
    application_irs = list(application_irs)
    evidence = list(evidence)
    datasources = list(datasources)
    source_by_id = {s.source_id: s for s in sources}
    paths = {
        (a.tool_inventory_id, a.sha256): str(a.original_source_path) for a in artifacts if a.sha256
    }
    incomplete = {
        p.tool_inventory_id
        for p in profiles
        if p.status != "complete"
        or p.semantic_coverage is None
        or not p.semantic_coverage.complete_code_coverage
    } | {
        c.tool_inventory_id
        for c in coverage
        if c.analysis_status != "complete"
        or c.extraction_status != "complete"
        or c.extraction_warning_count > 0
    }
    features: dict[str, _Feature] = {}

    def add(
        key: str,
        family: str,
        label: str,
        locations: Iterable[ThemeLocation],
        operations: Iterable[str] = (),
    ) -> None:
        feature = features.setdefault(key, _Feature(family, label))
        # Stable presentation regardless of the order in which apps were analyzed.
        feature.label = min(feature.label, label)
        feature.locations.extend(locations)
        feature.operations.update(operations)

    def source_location(source: SemanticSource, observation: str) -> ThemeLocation:
        return ThemeLocation(
            tool_inventory_id=source.tool_inventory_id,
            object_type=source.object_type,
            object_name=source.object_name,
            artifact=paths.get(
                (source.tool_inventory_id, source.artifact_hash), source.artifact_hash
            ),
            location=source.location or "",
            observation=observation,
            evidence_id=source.source_id,
        )

    # Whole code objects, reassembled only when every segment is available. Preserve
    # identifiers and literals; normalizing them away would claim false equivalence.
    code_objects: dict[tuple[str, str, str, str], list[SemanticSource]] = defaultdict(list)
    for source in sources:
        if source.model_eligible and source.object_type in {"query", "module", "form", "report"}:
            code_objects[
                (
                    source.tool_inventory_id,
                    source.artifact_hash,
                    source.object_type,
                    source.object_name,
                )
            ].append(source)
    for (_app, _artifact, kind, name), segments in sorted(code_objects.items()):
        segments.sort(key=lambda s: s.segment_index)
        if {s.segment_count for s in segments} != {len(segments)} or {
            s.segment_index for s in segments
        } != set(range(1, len(segments) + 1)):
            continue
        text = "\n".join(s.excerpt for s in segments).strip()
        if len(text) < 40:
            continue
        # Tokenization ignores formatting between tokens but retains literal values.
        tokens = re.findall(r'"(?:""|[^"\n])*"|\'(?:\'\'|[^\'\n])*\'|\w+|[^\w\s]', text)
        signature = json.dumps(tokens, ensure_ascii=True)
        digest = hashlib.sha256((kind + signature).encode()).hexdigest()
        add(
            "code:" + digest,
            "matching implementation",
            f"{kind}: {name}",
            [
                source_location(s, f"Matching normalized {kind} definition: {name}")
                for s in segments
            ],
        )

    # Any new extraction signal participates without adding an entry to a catalogue.
    for item in evidence:
        if item.inference and item.inference.strip():
            label = " ".join(item.inference.split())
            add(
                "signal:" + label.casefold(),
                "repeated observation",
                label,
                [evidence_location(item)],
            )

    # Concrete external resources provide stronger evidence than local object names.
    identities: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for datasource in datasources:
        identity = _datasource_identity(datasource)
        if identity is None:
            continue
        key, label = identity
        add(
            key,
            "shared external data",
            label,
            [
                evidence_location(e)
                for e in datasource.evidence
                if e.tool_inventory_id == datasource.tool_inventory_id
            ],
            [datasource.operation],
        )
        if datasource.object_name:
            identities[(datasource.tool_inventory_id, datasource.object_name.casefold())].append(
                (key, label)
            )
    for ir in application_irs:
        for fact in ir.behavior_facts:
            if fact.datasource_scope != "external":
                continue
            for target in fact.targets:
                matches = set(identities.get((ir.tool_inventory_id, target.casefold()), []))
                # Ambiguous aliases in multiple artifacts cannot identify a shared resource.
                if len(matches) != 1:
                    continue
                key, label = next(iter(matches))
                add(
                    key,
                    "shared external data",
                    label,
                    [
                        source_location(source_by_id[ref], fact.description)
                        for ref in fact.evidence_ids
                        if ref in source_by_id
                        and source_by_id[ref].tool_inventory_id == ir.tool_inventory_id
                    ],
                    [fact.action],
                )

    # Features with exactly the same membership describe one group. This keeps A/B
    # and B/C separate, permits overlap, and does not force a fixed number of groups.
    by_members: dict[tuple[str, ...], list[tuple[str, _Feature]]] = defaultdict(list)
    for key, feature in sorted(features.items()):
        members = tuple(sorted({loc.tool_inventory_id for loc in feature.locations}))
        if len(members) >= 2:
            by_members[members].append((key, feature))
    themes = []
    for members, shared in sorted(by_members.items()):
        themes.append(_theme(members, shared, incomplete))
    return sorted(themes, key=lambda t: (-len(t.affected_tool_ids), t.title, t.theme_id))


def _datasource_identity(source: Datasource) -> tuple[str, str] | None:
    if source.platform.casefold() in {"unknown", "access (local)"}:
        return None
    # A bare database filename or a server name alone is not a global identity.
    database = (source.database or "").strip()
    server = (source.server or "").strip()
    if not database or not (server or re.match(r"^(?:[A-Za-z]:[\\/]|[/\\]{1,2})", database)):
        return None
    parts = [source.platform, server, database, source.schema_name or ""]
    return "data:" + json.dumps([p.casefold() for p in parts]), " / ".join(p for p in parts if p)


def evidence_location(evidence: Evidence) -> ThemeLocation:
    return ThemeLocation(
        tool_inventory_id=evidence.tool_inventory_id,
        object_type=evidence.object_type,
        object_name=evidence.object_name,
        artifact=evidence.artifact_path,
        location=evidence.location or "",
        observation=evidence.inference or evidence.text,
        evidence_id=evidence.evidence_id,
    )


def _theme(
    members: tuple[str, ...],
    shared: list[tuple[str, _Feature]],
    incomplete: set[str],
) -> PortfolioTheme:
    rank = {"shared external data": 0, "matching implementation": 1, "repeated observation": 2}
    shared.sort(key=lambda item: (rank[item[1].family], item[1].label, item[0]))
    families = {feature.family for _, feature in shared}
    labels = list(dict.fromkeys(feature.label for _, feature in shared))
    headline = "; ".join(labels[:2]) + (
        f" (+{len(labels) - 2} patterns)" if len(labels) > 2 else ""
    )
    title = "Shared evidence: " + headline
    basis = [f"{feature.family}: {feature.label}" for _, feature in shared]
    locations = {
        (loc.tool_inventory_id, loc.evidence_id, loc.observation): loc
        for _, feature in shared
        for loc in feature.locations
    }
    objects = sorted({loc.object_name for loc in locations.values()})
    scope = ", ".join(objects[:5]) + (
        f" and {len(objects) - 5} more objects" if len(objects) > 5 else ""
    )
    options = []
    questions = []
    if "matching implementation" in families:
        options.append(
            f"Compare the matching definitions in {scope} for extraction into a shared "
            "query, module or library, preserving application-specific inputs and rules."
        )
        questions.append(
            "Do matching definitions behave the same with each application's data, "
            "settings, references and permissions?"
        )
    if "shared external data" in families:
        resources = "; ".join(f.label for _, f in shared if f.family == "shared external data")
        options.append(
            f"Coordinate data contracts and access for {resources}; evaluate shared "
            "views or a data-access module before introducing a network service."
        )
        operations = sorted({o for _, f in shared for o in f.operations if o != "UNKNOWN"})
        questions.append(
            f"Observed operations: {', '.join(operations) or 'unresolved'}. Which "
            "writes, refreshes and access rules need a common owner?"
        )
    if "repeated observation" in families:
        observations = "; ".join(f.label for _, f in shared if f.family == "repeated observation")
        options.append(
            f"Compare the implementations of {observations} in {scope}. Reuse a common "
            "adapter or convention where contracts match; retain distinct behavior "
            "where they differ."
        )
        questions.append(
            "Does the repeated signal represent the same contract and requirement, "
            "or only the same technology?"
        )
    options.append(
        "Retain separate applications and coordinate only the evidenced common parts "
        "if business ownership, security or release requirements differ."
    )
    provisional = bool(set(members) & incomplete)
    pattern = (
        f"{len(members)} applications share {len(shared)} observed pattern(s): "
        + "; ".join(basis)
        + ". Membership comes from these references, not role labels."
    )
    return PortfolioTheme(
        theme_id="theme-"
        + hashlib.sha256(
            json.dumps(
                [members, sorted(key for key, _ in shared)],
            ).encode()
        ).hexdigest()[:12],
        title=title,
        category="discovered evidence group",
        observed_pattern=pattern,
        proposed_solution=options[0],
        alternative_options=options[1:],
        grouping_basis=basis,
        affected_tool_ids=list(members),
        locations=sorted(
            locations.values(),
            key=lambda loc: (
                loc.tool_inventory_id,
                loc.object_type,
                loc.object_name,
                loc.evidence_id,
                loc.observation,
            ),
        ),
        next_steps=[
            f"Compare the cited objects: {scope}",
            "Validate one shared boundary against each affected application's outputs "
            "before selecting an implementation",
        ],
        validation_questions=questions,
        confidence=Confidence.LOW
        if provisional or families == {"repeated observation"}
        else Confidence.MEDIUM,
        coverage_note=(
            "Provisional: some affected applications have incomplete coverage. "
            if provisional
            else ""
        )
        + "Static overlap does not establish a business domain, runtime equivalence or a decision "
        "to consolidate. Options require owner validation.",
    )
