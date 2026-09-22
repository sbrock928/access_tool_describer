"""Lightweight static VBA analysis, intentionally not a VBA interpreter."""

from __future__ import annotations

import re
from dataclasses import dataclass

from portfolio_analyzer.models import Confidence

PROCEDURE = re.compile(
    r"^\s*(?:public |private |friend |static )?(sub|function|property\s+(?:get|let|set))\s+(\w+)",
    re.I | re.M,
)


@dataclass(frozen=True)
class VbaFinding:
    kind: str
    location: str
    text: str
    confidence: Confidence = Confidence.MEDIUM


def extract_procedures(source: str) -> list[str]:
    return [match.group(2) for match in PROCEDURE.finditer(source)]


def analyze_vba(source: str) -> list[VbaFinding]:
    checks = (
        (
            "Excel automation",
            r"\bExcel\.Application\b|CreateObject\s*\(\s*[\"']Excel\.Application",
            Confidence.HIGH,
        ),
        (
            "Outlook automation",
            r"\bOutlook\.Application\b|CreateObject\s*\(\s*[\"']Outlook\.Application",
            Confidence.HIGH,
        ),
        ("Shell invocation", r"\bShell\s*\(|\bWScript\.Shell\b", Confidence.HIGH),
        (
            "HTTP integration",
            r"\b(?:MSXML2?\.|WinHttp\.|XMLHTTP|ServerXMLHTTP)\b",
            Confidence.HIGH,
        ),
        (
            "Dynamic SQL execution",
            r"\b(?:DoCmd\.RunSQL|CurrentDb(?:\(\))?\.Execute|ADODB\.Command)\b",
            Confidence.HIGH,
        ),
        (
            "File operation",
            r"\b(?:FileCopy|Kill|MkDir|RmDir)\b|\bOpen\s+.+\s+For\s+|\bDir\s*\(",
            Confidence.MEDIUM,
        ),
        (
            "Configuration loading",
            r"\b(?:GetSetting|SaveSetting|OpenTextFile)\b",
            Confidence.HIGH,
        ),
        (
            "Database access",
            r"\b(?:CurrentDb|OpenDatabase|ADODB\.|DAO\.)\b",
            Confidence.MEDIUM,
        ),
        ("Suppressed VBA errors", r"\bOn\s+Error\s+Resume\s+Next\b", Confidence.HIGH),
    )
    findings: list[VbaFinding] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        analyzable = _without_vba_comment(line)
        for kind, pattern, confidence in checks:
            if re.search(pattern, analyzable, re.I):
                findings.append(
                    VbaFinding(
                        kind=kind,
                        location=f"line {line_number}",
                        text=line.strip(),
                        confidence=confidence,
                    )
                )
    return findings


def _without_vba_comment(line: str) -> str:
    """Remove apostrophe comments without treating apostrophes inside strings as comments."""
    in_string = False
    index = 0
    while index < len(line):
        character = line[index]
        if character == '"':
            if in_string and index + 1 < len(line) and line[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
        elif character == "'" and not in_string:
            return line[:index]
        index += 1
    return line
