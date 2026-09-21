"""Lightweight static VBA analysis, intentionally not a VBA interpreter."""

from __future__ import annotations

import re
from dataclasses import dataclass

PROCEDURE = re.compile(
    r"^\s*(?:public |private |friend |static )?(sub|function|property\s+(?:get|let|set))\s+(\w+)",
    re.I | re.M,
)


@dataclass(frozen=True)
class VbaFinding:
    kind: str
    location: str
    text: str


def extract_procedures(source: str) -> list[str]:
    return [match.group(2) for match in PROCEDURE.finditer(source)]


def analyze_vba(source: str) -> list[VbaFinding]:
    checks = {
        "Excel automation": r"\bExcel\.Application\b|CreateObject\s*\(\s*[\"']Excel\.Application",
        "Outlook automation": (
            r"\bOutlook\.Application\b|CreateObject\s*\(\s*[\"']Outlook\.Application"
        ),
        "Shell invocation": r"\bShell\s*\(|\bWScript\.Shell\b",
        "Access automation": r"\bAccess\.Application\b|DoCmd\.Open",
        "File operation": r"\b(?:Open|Kill|FileCopy|Name|MkDir|Dir)\b",
        "Configuration loading": r"\b(?:GetSetting|SaveSetting|OpenTextFile)\b",
        "Database access": r"\b(?:CurrentDb|OpenDatabase|ADODB\.|DAO\.)\b",
    }
    findings: list[VbaFinding] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        for kind, pattern in checks.items():
            if re.search(pattern, line, re.I):
                findings.append(
                    VbaFinding(kind=kind, location=f"line {line_number}", text=line.strip())
                )
    return findings
