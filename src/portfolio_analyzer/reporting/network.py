"""Application/resource network with scoped identities and traceable aggregated edges."""

from __future__ import annotations

import hashlib
import json
import ntpath
from collections import defaultdict
from typing import Any

from portfolio_analyzer.models import (
    Datasource,
    Dependency,
    InventoryRecord,
    SemanticPortfolioState,
    StagedArtifact,
)


def dependency_network(
    inventory: list[InventoryRecord],
    dependencies: list[Dependency],
    datasources: list[Datasource],
    state: SemanticPortfolioState | None = None,
    artifacts: list[StagedArtifact] | None = None,
) -> dict[str, Any]:
    names = {i.tool_inventory_id: i.tool_name for i in inventory}
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    artifact_paths = {
        (a.tool_inventory_id, a.sha256): str(a.local_staged_path)
        for a in artifacts or []
        if a.local_staged_path
    }
    bindings: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    staged_paths = {
        ntpath.normpath(str(a.local_staged_path).replace("/", "\\")).casefold()
        for a in artifacts or []
        if a.local_staged_path
    }
    for app, name in names.items():
        nodes["app:" + app] = {
            "id": "app:" + app,
            "label": name,
            "kind": "application",
            "app_id": app,
        }

    def add(
        app: str,
        kind: str,
        identity: list[str],
        label: str,
        operation: str,
        records: list[dict[str, str]],
        relationship: str,
    ) -> str | None:
        if app not in names:
            return None
        resource = (
            "resource:" + hashlib.sha256(json.dumps([kind, *identity]).encode()).hexdigest()[:20]
        )
        nodes.setdefault(resource, {"id": resource, "label": label, "kind": kind})
        edge = edges.setdefault(
            ("app:" + app, resource),
            {
                "source": "app:" + app,
                "target": resource,
                "app_id": app,
                "operations": set(),
                "relationships": set(),
                "evidence": {},
            },
        )
        edge["operations"].add(operation)
        edge["relationships"].add(relationship)
        for record in records:
            edge["evidence"][json.dumps(record, sort_keys=True)] = record
        return resource

    for d in datasources:
        parts = [d.platform, d.server or "", d.database or "", d.schema_name or ""]
        external = (
            d.platform.casefold() not in {"unknown", "access (local)"}
            and bool(d.database)
            and bool(d.server or ntpath.isabs(d.database or ""))
        )
        kind = (
            "external_data"
            if external
            else "local_data"
            if d.platform.casefold() == "access (local)"
            else "unresolved"
        )
        scope = sorted({e.artifact_path for e in d.evidence}) or ["artifact unresolved"]
        identity = (
            [p.casefold() for p in parts]
            if external
            else [
                d.tool_inventory_id,
                *scope,
                d.schema_name or "",
                (d.object_name or "Unknown").casefold(),
            ]
        )
        label = (
            " / ".join(p for p in parts if p) if external else d.object_name or "Unresolved data"
        )
        records = [
            {
                "object": e.object_name,
                "object_type": e.object_type,
                "artifact": e.artifact_path,
                "location": e.location or "",
                "evidence_id": e.evidence_id,
                "target": d.object_name or "",
                "observation": e.inference or e.text,
            }
            for e in d.evidence
        ]
        resource_id = add(
            d.tool_inventory_id, kind, identity, label, d.operation, records, "data access"
        )
        aliases = {d.object_name or ""}
        if d.schema_name and d.object_name:
            aliases.add(f"{d.schema_name}.{d.object_name}")
        aliases.update(e.object_name for e in d.evidence if e.object_type == "linked_table")
        if resource_id:
            for artifact in scope:
                for alias in aliases - {""}:
                    bindings[(d.tool_inventory_id, artifact.casefold(), alias.casefold())].add(
                        resource_id
                    )

    for dependency in dependencies:
        path = dependency.target.replace("/", "\\")
        normalized = ntpath.normpath(path).casefold()
        if dependency.dependency_type == "access_vba_reference":
            kind, identity = "runtime", [normalized]
        elif normalized in staged_paths:
            kind, identity = "analysis_artifact", [dependency.tool_inventory_id, normalized]
        elif dependency.target.lower().startswith(("https://", "http://")):
            kind, identity = "endpoint", [dependency.target]  # URL paths may be case-sensitive.
        elif path.startswith("\\\\"):
            kind, identity = "shared_file", [normalized]
        elif dependency.dependency_type == "filesystem":
            kind, identity = "local_file", [dependency.tool_inventory_id, normalized]
        else:
            kind, identity = "unresolved", [dependency.tool_inventory_id, dependency.target]
        records = [
            {
                "object": e.object_name,
                "object_type": e.object_type,
                "artifact": e.artifact_path,
                "location": e.location or "",
                "evidence_id": e.evidence_id,
                "target": dependency.target,
                "observation": e.inference or e.text,
            }
            for e in dependency.evidence
        ]
        if not records:
            records = [
                {
                    "object": dependency.source,
                    "object_type": "",
                    "artifact": "",
                    "location": "",
                    "evidence_id": "",
                    "target": dependency.target,
                    "observation": dependency.dependency_type,
                }
            ]
        add(
            dependency.tool_inventory_id,
            kind,
            identity,
            dependency.target,
            dependency.operation,
            records,
            dependency.dependency_type,
        )

    if state:
        for ir in state.application_irs:
            for fact in ir.behavior_facts:
                # Unresolved static references remain local; they cannot create shared hubs.
                if fact.action not in {
                    "read",
                    "write",
                    "entry",
                    "report",
                    "run_query",
                    "open_form",
                }:
                    continue
                for target in fact.targets:
                    artifact = artifact_paths.get(
                        (ir.tool_inventory_id, fact.artifact_hash), fact.artifact_hash
                    )
                    records = [
                        {
                            "object": fact.object_name,
                            "object_type": fact.object_type,
                            "artifact": artifact,
                            "location": "",
                            "evidence_id": ref,
                            "target": target,
                            "observation": fact.description,
                        }
                        for ref in fact.evidence_ids
                    ]
                    matches = bindings.get(
                        (ir.tool_inventory_id, artifact.casefold(), target.casefold()), set()
                    )
                    if len(matches) == 1:
                        edge = edges[("app:" + ir.tool_inventory_id, next(iter(matches)))]
                        edge["operations"].add(fact.action.upper())
                        edge["relationships"].add("static object reference")
                        for record in records:
                            edge["evidence"][json.dumps(record, sort_keys=True)] = record
                    else:
                        add(
                            ir.tool_inventory_id,
                            "local_data" if fact.datasource_scope == "local" else "unresolved",
                            [ir.tool_inventory_id, artifact, target.casefold()],
                            target,
                            fact.action.upper(),
                            records,
                            "static object reference",
                        )
    consumers: dict[str, set[str]] = defaultdict(set)
    output_edges = []
    for _key, edge in sorted(edges.items()):
        consumers[edge["target"]].add(edge["app_id"])
        output_edges.append(
            {
                **edge,
                "operations": sorted(edge["operations"]),
                "relationships": sorted(edge["relationships"]),
                "evidence": [edge["evidence"][k] for k in sorted(edge["evidence"])],
            }
        )
    for node in nodes.values():
        node["consumer_ids"] = sorted(consumers.get(node["id"], set()))
        node["consumer_count"] = len(node["consumer_ids"])
    candidates = [
        {
            "resource_id": n["id"],
            "label": n["label"],
            "application_ids": n["consumer_ids"],
            "reason": f"{n['consumer_count']} applications reference this confirmed resource.",
            "next_step": "Compare operations, ownership and transaction boundaries. "
            "A shared contract, "
            "view or adapter may suffice; consider a service when independent ownership "
            "and lifecycle requirements justify it.",
        }
        for n in nodes.values()
        if n["consumer_count"] >= 2 and n["kind"] in {"external_data", "shared_file", "endpoint"}
    ]
    return {
        "nodes": sorted(nodes.values(), key=lambda n: (n["kind"], n["label"], n["id"])),
        "edges": output_edges,
        "candidates": sorted(candidates, key=lambda c: (-len(c["application_ids"]), c["label"])),
        "raw_dependency_count": len(dependencies),
        "datasource_count": len(datasources),
    }
