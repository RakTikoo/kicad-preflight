"""Optional integration with KiCad's official `kicad-cli` tool.

When kicad-cli is on PATH, Preflight runs the official design-rule check
(DRC) on the PCB and electrical-rule check (ERC) on the schematic, and
folds their violations into the report. When it is not installed, the
official checks are recorded as SKIPPED in the report's Coverage section —
never silently pretended, and the built-in text checks still run.

Trust boundaries (hardened after the v0 audit):
  * a non-zero kicad-cli exit is a FAILED run — its output file is not
    trusted, however plausible it looks;
  * violations are collected iteratively (no recursion limit), a
    matched violation node is not descended into (containers are not
    double-counted as their own children), and duplicates are removed
    on (severity, type, description, item identity), not whole-object
    equality — real ERC reports nest per sheet, so sheet/uuid fields
    legitimately differ between copies of the same violation;
  * severities are mapped error -> BLOCK, warning -> CAUTION;
    'exclusion' entries (user-suppressed) and unrecognised severities
    are counted in Coverage, never silently turned into findings;
  * violations per job are capped (MAX_VIOLATIONS) with the overflow
    stated in the Coverage status line.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .finding import BLOCK, CAUTION, NOTE, Finding

TIMEOUT_SECONDS = 180
MAX_VIOLATIONS = 500


def find_cli():
    return shutil.which("kicad-cli")


def _collect_violations(root):
    """Iteratively collect violation dicts (a dict with 'severity' plus
    'type' or 'description'); do not descend into a matched node."""
    out = []
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "severity" in node and ("description" in node
                                       or "type" in node):
                out.append(node)
                continue
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def _dedup_key(v):
    items = v.get("items") or v.get("violations") or []
    identities = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                identities.append(str(item.get("uuid")
                                      or item.get("description") or ""))
    return (str(v.get("severity", "")), str(v.get("type", "")),
            str(v.get("description", "")), tuple(sorted(identities)))


def _run_one(cli, args, source_path, out_name):
    """Run one kicad-cli check. Returns (violations, status_line, ignored)
    where violations is None when the run did not produce trustworthy
    output."""
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / out_name
        cmd = [cli] + args + ["--format", "json", "--output", str(out_path),
                              str(source_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"skipped — kicad-cli run failed ({exc})", 0
        if proc.returncode != 0:
            return None, (f"skipped — kicad-cli exited "
                          f"{proc.returncode}; its output was not "
                          "trusted"), 0
        if not out_path.is_file():
            return None, "skipped — kicad-cli produced no report", 0
        try:
            data = json.loads(out_path.read_text(encoding="utf-8",
                                                 errors="replace"))
        except ValueError:
            return None, "skipped — kicad-cli report was not valid JSON", 0
        violations = _collect_violations(data)
        seen, unique = set(), []
        for v in violations:
            key = _dedup_key(v)
            if key not in seen:
                seen.add(key)
                unique.append(v)
        return unique, f"ran — {len(unique)} violation(s)", 0


def run_official_checks(project):
    """Returns (findings, coverage) where coverage describes what ran."""
    coverage = {"available": False, "erc": "skipped — kicad-cli not found",
                "drc": "skipped — kicad-cli not found", "failed": False}
    cli = find_cli()
    if cli is None:
        return [], coverage
    coverage["available"] = True
    coverage["path"] = cli
    findings = []

    jobs = []
    if project.sch_path is not None:
        jobs.append(("erc", ["sch", "erc"], project.sch_path,
                     "KICAD_ERC_VIOLATION"))
    if project.pcb_path is not None:
        jobs.append(("drc", ["pcb", "drc"], project.pcb_path,
                     "KICAD_DRC_VIOLATION"))
    for key, args, source, rule in jobs:
        violations, status, _ = _run_one(cli, args, source, f"{key}.json")
        if violations is None:
            coverage["failed"] = True
            coverage[key] = status
            continue
        ignored = 0
        shown = violations[:MAX_VIOLATIONS]
        if len(violations) > MAX_VIOLATIONS:
            status += (f" (showing first {MAX_VIOLATIONS}; "
                       f"{len(violations) - MAX_VIOLATIONS} more not "
                       "listed)")
        for v in shown:
            severity_raw = str(v.get("severity", "")).lower()
            if severity_raw == "error":
                severity = BLOCK
            elif severity_raw == "warning":
                severity = CAUTION
            else:
                # 'exclusion' (user-suppressed) or unrecognised: counted,
                # never silently mapped to a finding.
                ignored += 1
                continue
            message = v.get("description") or v.get("type") or "violation"
            vtype = v.get("type", "")
            findings.append(Finding(
                rule, severity,
                f"Official {'ERC' if key == 'erc' else 'DRC'} "
                f"({vtype}): {message}" if vtype else
                f"Official {'ERC' if key == 'erc' else 'DRC'}: {message}",
                where=source.name,
                evidence=f"severity={severity_raw or 'unknown'}"))
        if ignored:
            status += (f"; {ignored} exclusion/unrecognised-severity "
                       "entr(ies) not counted as findings")
        coverage[key] = status
    return findings, coverage
