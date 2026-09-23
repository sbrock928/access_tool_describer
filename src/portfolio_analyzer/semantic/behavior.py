"""Evidence-backed descriptions of static Access behavior; never execute source text."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from portfolio_analyzer.models import (
    BehaviorFact,
    Datasource,
    SemanticApplicationIR,
    SemanticSource,
)
from portfolio_analyzer.parsing.sql import classify_sql
from portfolio_analyzer.parsing.vba import _without_vba_comment, analyze_vba

_UI_KEYS = {"recordsource", "dataentry", "allowedits", "allowadditions", "allowdeletions"}
_WRITE_OPERATIONS = {"INSERT", "UPDATE", "DELETE", "MERGE", "MAKE_TABLE", "DDL"}
DatasourceScope = Literal["local", "external", "unresolved", "not_applicable"]
_INTEGRATIONS = {"import", "export", "email", "http", "excel", "shell", "file"}
_ACTIONS = {
    "openreport": ("report", "Opens a report"),
    "openform": ("open_form", "Opens a form"),
    "openquery": ("run_query", "Invokes a saved query"),
    "runsql": ("dynamic_sql", "Executes SQL; targets may be determined at runtime"),
    "sendobject": ("email", "Sends an Access object by email"),
    "outputto": ("export", "Exports an Access object"),
    "transfertext": ("transfer", "Transfers text data"),
    "transferspreadsheet": ("transfer", "Transfers spreadsheet data"),
    "transferdatabase": ("transfer", "Transfers database data"),
    "runmacro": ("batch", "Invokes a macro"),
}
_SIGNALS = {
    "HTTP integration": ("http", "Contains HTTP interaction code"),
    "Excel automation": ("excel", "Automates Excel"),
    "Outlook automation": ("email", "Automates Outlook"),
    "Shell invocation": ("shell", "Invokes an external process"),
    "File operation": ("file", "Performs filesystem operations"),
}


def ui_properties(definition: str, properties: dict[str, str]) -> dict[str, str]:
    """Read only root form/report properties, excluding control properties and VBA.

    SaveAsText splits long strings into adjacent quoted continuation lines. Missing
    edit flags remain unknown: Access defaults are not proof of an entry workflow.
    """
    result = {
        key.casefold(): value for key, value in properties.items() if key.casefold() in _UI_KEYS
    }
    depth = 0
    active: str | None = None
    for line in definition.splitlines():
        value = line.strip()
        if value.casefold().startswith("codebehindform"):
            break
        if re.fullmatch(r"Begin(?:\s+\w+)?", value, re.I):
            depth += 1
            active = None
            continue
        if value.casefold() == "end":
            depth -= 1
            active = None
            continue
        if depth > 1:
            continue
        match = re.fullmatch(r"(\w+)\s*=\s*(.*)", value)
        if match:
            active = match[1].casefold()
            if active in _UI_KEYS:
                result[active] = _unquote(match[2])
            else:
                active = None
        elif active and value.startswith('"'):
            result[active] += _unquote(value)
        else:
            active = None
    return result


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value


def _true(value: str) -> bool:
    return value.casefold() in {"-1", "1", "true", "yes"}


def build_behavior_facts(
    inventory: list[SemanticSource],
    inspected: list[SemanticSource],
    datasources: list[Datasource],
) -> list[BehaviorFact]:
    facts: dict[tuple[str, str, str, str, tuple[str, ...]], BehaviorFact] = {}
    objects: dict[tuple[str, str, str], SemanticSource] = {}
    for source in inventory:
        objects.setdefault((source.artifact_hash, source.object_type, source.object_name), source)
    local_names = {
        (s.artifact_hash, s.object_name.casefold())
        for s in inventory
        if s.object_type in {"table", "query"}
    }
    linked_names = {
        (s.artifact_hash, s.object_name.casefold())
        for s in inventory
        if s.object_type == "linked_table"
    }

    def add(
        source: SemanticSource,
        action: str,
        description: str,
        targets: list[str] | None = None,
        scope: DatasourceScope = "not_applicable",
    ) -> None:
        names = sorted(set(targets or []), key=str.casefold)
        key = (source.artifact_hash, source.object_type, source.object_name, action, tuple(names))
        if key in facts:
            facts[key].evidence_ids = sorted(set(facts[key].evidence_ids) | {source.source_id})
            return
        facts[key] = BehaviorFact(
            action=action,
            description=description,
            object_type=source.object_type,
            object_name=source.object_name,
            targets=names,
            datasource_scope=scope,
            evidence_ids=[source.source_id],
        )

    def scope_for(source: SemanticSource, target: str) -> DatasourceScope:
        key = target.casefold().strip("[]")
        if (source.artifact_hash, key) in linked_names:
            return "external"
        if (source.artifact_hash, key) in local_names:
            return "local"
        matches = [d for d in datasources if (d.object_name or "").casefold() == key]
        if matches and all(d.platform == "Access (local)" for d in matches):
            return "local"
        if matches and all(d.platform not in {"Unknown", "Access (local)"} for d in matches):
            return "external"
        return "unresolved"

    def sql_facts(source: SemanticSource, sql: str) -> None:
        parsed = classify_sql(sql)
        for ref in parsed.references:
            action = "read" if ref.operation == "READ" else "write"
            if ref.operation == "EXECUTE":
                action = "dynamic_sql"
            verb = {"read": "Reads", "dynamic_sql": "Invokes"}.get(
                action, f"Changes ({ref.operation.lower()})"
            )
            add(source, action, f"{verb} {ref.name}", [ref.name], scope_for(source, ref.name))
        if parsed.operation in _WRITE_OPERATIONS and not parsed.references:
            add(source, "write", f"Contains a {parsed.operation.lower()} query; target unresolved")
        elif parsed.operation == "READ" and not parsed.references:
            add(source, "read", "Evaluates a read-only query")

    for source in objects.values():
        kind = source.object_type.casefold()
        if kind == "linked_table":
            add(
                source,
                "connection",
                f"Links external table {source.object_name}",
                [source.object_name],
                "external",
            )
        props = source.ui_properties
        record_source = props.get("recordsource", "").strip()
        if kind == "report":
            add(source, "report", f"Defines report {source.object_name}", [source.object_name])
        if kind not in {"form", "report"} or not record_source:
            continue
        if re.match(r"^(SELECT|TRANSFORM|PARAMETERS)\b", record_source, re.I):
            sql_facts(source, record_source)
            targets = list(classify_sql(record_source).object_names)
        else:
            targets = [record_source.strip("[]")]
            add(
                source,
                "read",
                f"Binds {source.object_name} to {targets[0]}",
                targets,
                scope_for(source, targets[0]),
            )
        if kind == "form" and any(
            _true(props.get(key, ""))
            for key in ("dataentry", "allowedits", "allowadditions", "allowdeletions")
        ):
            add(
                source,
                "entry",
                f"Form {source.object_name} is configured for record entry or editing",
                targets,
            )

    # Reassemble complete selected objects so splits do not break statement matching.
    grouped: dict[tuple[str, str, str], list[SemanticSource]] = defaultdict(list)
    for source in inspected:
        grouped[(source.artifact_hash, source.object_type, source.object_name)].append(source)
    for segments in grouped.values():
        segments.sort(key=lambda s: s.segment_index)
        source = segments[0]
        content = "\n".join(s.excerpt for s in segments)
        if source.object_type == "query":
            sql_facts(source, content)
        elif source.object_type in {"module", "form", "report"}:
            code = "\n".join(
                _without_vba_comment(line) if not re.match(r"^\s*Rem\b", line, re.I) else ""
                for line in content.splitlines()
            )
            code = re.sub(r"\s+_\s*\n\s*", " ", code)
            # Mask strings for call detection, keeping positions for literal arguments.
            masked = re.sub(r'"(?:""|[^"\n])*"', lambda m: " " * len(m[0]), code)
            for finding in analyze_vba(masked):
                # CreateObject's ProgID is a literal; handle it separately below.
                if finding.kind in _SIGNALS:
                    action, description = _SIGNALS[finding.kind]
                    add(source, action, description)
            for match in re.finditer(
                r'\bCreateObject\s*\(\s*"(Excel\.Application|Outlook\.Application|'
                r'MSXML2?\.[^"\n]+|WinHttp\.[^"\n]+)"',
                code,
                re.I,
            ):
                if not masked[match.start() :].startswith(code[match.start() : match.start() + 12]):
                    continue
                progid = match[1].casefold()
                action, description = (
                    ("excel", "Automates Excel")
                    if progid.startswith("excel")
                    else ("email", "Automates Outlook")
                    if progid.startswith("outlook")
                    else ("http", "Contains HTTP interaction code")
                )
                add(source, action, description)
            for match in re.finditer(r"\bDoCmd\.(\w+)\b", masked, re.I):
                name = match[1].casefold()
                if name not in _ACTIONS:
                    continue
                tail = code[match.end() :].split("\n", 1)[0].strip().lstrip("(")
                action, description = _ACTIONS[name]
                targets = []
                literal = re.match(r'"((?:""|[^"\n])*)"', tail)
                if name == "runsql" and literal:
                    sql_facts(source, literal[1].replace('""', '"'))
                    continue
                elif name in {"openquery", "openreport", "openform", "runmacro"} and literal:
                    targets = [literal[1].replace('""', '"')]
                    description += f" {targets[0]}"
                if action == "transfer":
                    direction = tail.split(",", 1)[0].strip().casefold()
                    if direction in {
                        "acimport",
                        "acimportdelim",
                        "acimportfixed",
                        "acimporthtml",
                        "0",
                    }:
                        action, description = "import", description.replace("Transfers", "Imports")
                    elif direction in {
                        "acexport",
                        "acexportdelim",
                        "acexportfixed",
                        "acexporthtml",
                    } or (direction == "1" and name != "transfertext"):
                        action, description = "export", description.replace("Transfers", "Exports")
                    else:
                        description += "; direction unresolved"
                arguments = _arguments(tail)
                target_index = {
                    "outputto": 1,
                    "sendobject": 1,
                    "transfertext": 2,
                    "transferspreadsheet": 2,
                    "transferdatabase": 4,
                }.get(name)
                if target_index is not None and len(arguments) > target_index:
                    target = arguments[target_index]
                    if re.fullmatch(r'"(?:""|[^"\n])*"', target):
                        targets = [_unquote(target)]
                        description += f" ({targets[0]})"
                add(source, action, description, targets)
                if (
                    name in {"outputto", "sendobject"}
                    and arguments
                    and arguments[0].casefold() == "acoutputreport"
                ):
                    add(
                        source,
                        "report",
                        "Produces a report" + (f" {targets[0]}" if targets else ""),
                        targets,
                    )
            for match in re.finditer(r"\b(?:CurrentDb\s*(?:\(\))?|\w+)\.Execute\b", masked, re.I):
                tail = code[match.end() :].split("\n", 1)[0].strip().lstrip("(")
                literal = re.match(r'"((?:""|[^"\n])*)"', tail)
                if literal:
                    sql_facts(source, literal[1].replace('""', '"'))
                else:
                    add(
                        source,
                        "dynamic_sql",
                        "Executes a command; operation and targets unresolved",
                    )
            recordsets = {
                match[1]
                for match in re.finditer(
                    r"\b(?:Dim|Private|Public)\s+(\w+)\s+As\s+(?:DAO\.|ADODB\.)?Recordset\b",
                    masked,
                    re.I,
                )
                if match[1]
            }
            recordsets.update(
                match[1]
                for match in re.finditer(
                    r"\bSet\s+(\w+)\s*=\s*[^\n]*\bOpenRecordset\s*\(", masked, re.I
                )
            )
            recordset_pattern = "|".join(re.escape(name) for name in sorted(recordsets))
            receiver = r"(?:Me\.)?Recordset(?:Clone)?"
            if recordset_pattern:
                receiver += "|" + recordset_pattern
            if source.object_type == "form" and re.search(
                rf"\b(?:{receiver})\.(?:AddNew|Edit|Update)\b|\bMe\.Dirty\s*=\s*False\b",
                masked,
                re.I,
            ):
                add(source, "entry", f"Form {source.object_name} contains record editing code")
        elif source.object_type == "macro":
            for match in re.finditer(r"(?im)^\s*Action\s*=\s*\"?(\w+)", content):
                name = match[1].casefold()
                if name in _ACTIONS:
                    action, description = _ACTIONS[name]
                    add(source, action, description + " (macro action; arguments unresolved)")
                elif name not in {"", "comment"}:
                    add(source, "batch", f"Macro invokes {match[1]}")
        # All segments of an inspected object support its assembled facts.
        for key, fact in facts.items():
            if key[:3] == (source.artifact_hash, source.object_type, source.object_name):
                fact.evidence_ids = sorted(set(fact.evidence_ids) | {s.source_id for s in segments})

    # A form that explicitly invokes a writing query has a supported update workflow.
    artifact_by_source = {s.source_id: s.artifact_hash for s in inventory}
    writing_queries = {
        (artifact_by_source[fact.evidence_ids[0]], fact.object_name.casefold()): fact
        for fact in facts.values()
        if fact.object_type == "query" and fact.action == "write"
    }
    for fact in list(facts.values()):
        if fact.object_type != "form":
            continue
        artifact_hash = artifact_by_source[fact.evidence_ids[0]]
        writes = fact.action == "write" or (
            fact.action == "run_query"
            and any((artifact_hash, t.casefold()) in writing_queries for t in fact.targets)
        )
        if writes:
            source = next(s for s in inventory if s.source_id == fact.evidence_ids[0])
            add(source, "entry", f"Form {source.object_name} invokes record updates", fact.targets)
            entry = facts[
                (
                    source.artifact_hash,
                    source.object_type,
                    source.object_name,
                    "entry",
                    tuple(sorted(set(fact.targets), key=str.casefold)),
                )
            ]
            entry.evidence_ids = sorted(
                set(fact.evidence_ids)
                | {
                    ref
                    for target in fact.targets
                    if (artifact_hash, target.casefold()) in writing_queries
                    for ref in writing_queries[(artifact_hash, target.casefold())].evidence_ids
                }
            )
    return sorted(facts.values(), key=lambda f: (f.action, f.object_type, f.object_name, f.targets))


def _arguments(value: str) -> list[str]:
    """Split Access action arguments without splitting commas inside VBA literals."""
    return [part.strip() for part in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", value)]


def classify_behavior(ir: SemanticApplicationIR) -> tuple[str, str, list[str], list[str]]:
    facts = ir.behavior_facts
    entry = [f for f in facts if f.action == "entry"]
    reports = [f for f in facts if f.action == "report"]
    integration = [f for f in facts if f.action in _INTEGRATIONS | {"transfer"}]
    batch = [
        f
        for f in facts
        if f.action in {"write", "batch", "dynamic_sql", "run_query"} and f.object_type != "form"
    ]
    reads = [f for f in facts if f.action == "read"]
    supported: list[BehaviorFact]
    if entry and reports:
        role, reason, supported = (
            "mixed application",
            "Both interactive updates and reporting are supported",
            entry + reports,
        )
    elif entry:
        role, reason, supported = (
            "transactional workflow",
            "Interactive record entry or updates are supported",
            entry,
        )
    elif reports or (
        reads and not batch and all(f.action in {"export", "email", "excel"} for f in integration)
    ):
        role, reason, supported = (
            "reporting and analytics",
            "Report definitions or report-opening actions are present"
            if reports
            else "Read/query behavior is present; output integrations are secondary",
            reports or reads,
        )
    elif integration:
        role, reason, supported = (
            "integration utility",
            "Explicit transfer or external interaction code is present",
            integration,
        )
    elif batch:
        role, reason, supported = (
            "batch automation",
            "Noninteractive processing or action queries are present",
            batch,
        )
    elif reads:
        role, reason, supported = (
            "reporting and analytics",
            "Read/query behavior is present without supported update or transfer behavior",
            reads,
        )
    else:
        role, reason, supported = (
            "unknown",
            "Insufficient behavioral evidence; object names and connections alone "
            "do not establish a role",
            [],
        )
    examples = list(dict.fromkeys(f.description for f in supported))[:3]
    rationale = reason + (": " + "; ".join(examples) if examples else "") + "."
    refs = sorted({ref for f in supported for ref in f.evidence_ids})
    secondary = (
        sorted({f.description for f in integration}) if role != "integration utility" else []
    )
    return role, rationale, refs, secondary


def behavior_details(ir: SemanticApplicationIR) -> tuple[list[str], list[str], list[str]]:
    # Interleave behavior categories so repeated reads cannot bury entry or outputs.
    groups: dict[str, list[str]] = defaultdict(list)
    for fact in ir.behavior_facts:
        suffix = (
            f" ({fact.datasource_scope} datasource)"
            if fact.datasource_scope != "not_applicable"
            else ""
        )
        description = fact.description + suffix
        if description not in groups[fact.action]:
            groups[fact.action].append(description)
    order = [
        "entry",
        "report",
        "read",
        "write",
        "import",
        "export",
        "transfer",
        "email",
        "http",
        "excel",
        "batch",
        "run_query",
        "dynamic_sql",
        "open_form",
        "shell",
        "file",
        "connection",
    ]
    observed: list[str] = []
    for index in range(max((len(v) for v in groups.values()), default=0)):
        for action in order:
            if index < len(groups[action]):
                observed.append(groups[action][index])
    inputs = sorted(
        {t for f in ir.behavior_facts if f.action in {"read", "export"} for t in f.targets}
    )
    outputs = sorted(
        {
            t
            for f in ir.behavior_facts
            if f.action in {"write", "report", "import"}
            for t in f.targets
        }
        | {f.description for f in ir.behavior_facts if f.action in {"export", "email"}}
    )
    return observed, inputs, outputs
