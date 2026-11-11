"""Optional integration with KiCad's official `kicad-cli` tool.

When kicad-cli is on PATH, Preflight runs the official design-rule
check (DRC) on the PCB and electrical-rule check (ERC) on the
schematic, and folds their violations into the report. When it is
not installed, the official checks are recorded as SKIPPED in the
report's Coverage section — never silently pretended, and the
built-in text checks still run.

v0.6 (WS1, hardened against the real KiCad 8.0.9 reports — full
evidence in kicad-preflight-review/v06-ws1/real-json-catalogue.md):

  * Reports are read by their KNOWN sections, not by a generic
    recursive hunt. A KiCad 8 DRC report is a flat object with
    `violations`, `unconnected_items` and `schematic_parity` lists;
    an ERC report nests violations under `sheets[].violations`.
    Every observed entry has exactly the fields type / severity /
    description / items[{description, pos{x, y}, uuid}], plus an
    optional `excluded: true`. Sections the old generic collector
    handled only by accident are now handled on purpose:
      - `unconnected_items` are real opens and surface as
        KICAD_DRC_VIOLATION findings (type `unconnected_items`);
      - `schematic_parity` runs as its OWN DRC job (see below) and
        surfaces under the distinct rule id KICAD_PARITY_CONFLICT.
    A report with none of the known sections falls back to the old
    bounded generic collector (older/other emitters, test fakes).
  * DRC runs are requested with `--severity-all`, because KiCad's
    default report (error+warning) hides user-excluded violations
    entirely; with all severities requested, an excluded violation
    arrives with its real severity plus `"excluded": true`. Those
    entries are COUNTED in Coverage, never turned into findings —
    the user already dismissed that exact instance in KiCad.
    (ERC gets the same flag: KiCad 8 ERC excluded entries behave
    identically in the report.)
  * `--schematic-parity` is a SEPARATE job, never folded into the
    main DRC run: on boards whose schematic KiCad cannot load, the
    parity run fails (observed rc 255 on the netlist samples) while
    plain DRC succeeds — folding it in would lose every DRC
    finding. A parity failure marks only the parity Coverage line,
    never the main jobs. The parity run's own `violations` /
    `unconnected_items` sections are discarded (they duplicate the
    main run); only its `schematic_parity` section is read.
  * Project settings (.kicad_pro, see kicadsettings.py) are
    honoured: a violation whose type the project set to "ignore",
    or which a stored per-instance exclusion matches exactly, does
    not become a finding and is counted as a waiver in Coverage; a
    stated severity override replaces the reported severity in the
    mapping. On real KiCad 8.0.9 reports the reported severity
    already reflects the project file (verified by editing .kicad_pro
    copies and re-running), so the override is normally a no-op —
    it exists so a report that disagrees with the project file is
    resolved in favour of the project file, and so the 'ignore'
    guarantee holds even for emitters that report ignored types.

Trust boundaries (from the v0 audit, kept):
  * a non-zero kicad-cli exit is a FAILED run — its output file is
    not trusted, however plausible it looks (KiCad 8.0.9 ERC
    segfaults on the hand-written led-board samples: rc -11/139,
    no file — handled by exactly this rule);
  * collection is iterative and bounded (node budget, per-section
    caps), matched violation nodes are not descended into, and
    duplicates are removed per section on (severity, type,
    description, item identity) — real ERC reports nest per sheet,
    so sheet/uuid fields legitimately differ between copies of the
    same violation;
  * severities are mapped error -> BLOCK, warning -> CAUTION;
    `excluded` entries, `severity: "exclusion"` entries (a
    report-filter value some versions emit; never observed from
    8.0.9, which uses the boolean flag) and unrecognised severities
    are counted in Coverage, never silently turned into findings;
  * violations per section are capped (MAX_VIOLATIONS) with the
    overflow stated in the Coverage status line.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import kicadsettings
from .finding import BLOCK, CAUTION, Finding

TIMEOUT_SECONDS = 180
MAX_VIOLATIONS = 500
MAX_COLLECT_NODES = 200_000     # fallback collector budget
MAX_ITEMS_PER_VIOLATION = 50    # identity/dedup work per violation

RULE_ERC = "KICAD_ERC_VIOLATION"
RULE_DRC = "KICAD_DRC_VIOLATION"
RULE_PARITY = "KICAD_PARITY_CONFLICT"


def find_cli():
    return shutil.which("kicad-cli")


# ---------------------------------------------------------------------------
# Report reading
# ---------------------------------------------------------------------------


def _collect_violations(root):
    """Legacy generic collector, kept for reports with no known
    sections (and for the test fakes that shaped the v0 adapter).

    Iteratively collect violation dicts (a dict with 'severity' plus
    'type' or 'description'); do not descend into a matched node.
    Bounded: stops after MAX_COLLECT_NODES visited nodes so a
    deeply/hugely nested report cannot hang the run."""
    out, _truncated = _collect_violations_bounded(root)
    return out


def _collect_violations_bounded(root):
    out = []
    stack = [root]
    visited = 0
    truncated = False
    while stack:
        if visited >= MAX_COLLECT_NODES:
            truncated = True
            break
        node = stack.pop()
        visited += 1
        if isinstance(node, dict):
            if "severity" in node and ("description" in node
                                       or "type" in node):
                out.append(node)
                continue
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out, truncated


class _MalformedReport(Exception):
    """A report whose known sections have the wrong types — the
    output cannot be trusted as 'zero violations' (RT-04)."""


def _violation_dicts(node):
    """The dict entries of a report section list; [] for anything
    else. Non-dict entries are not violations (poison tolerance)."""
    if not isinstance(node, list):
        return []
    return [entry for entry in node if isinstance(entry, dict)]


def _sections_from_report(data):
    """Split a parsed report into its known sections.

    Returns (sections, used_fallback) where sections maps
    "violations" / "unconnected_items" / "schematic_parity" /
    "erc_sheets" to lists of violation dicts. Returns (None, True)
    when the report has no recognisable section at all — the caller
    then falls back to the generic collector. A non-dict report is
    (None, True) as well (the generic collector yields nothing for
    it, which the caller treats as an empty report).

    Raises _MalformedReport when a KNOWN section key is present but
    its value has the wrong type (v0.6 review RT-04): a dict/string/
    null where a list belongs is a corrupt report, and reading it
    as "ran — 0 violations" would fabricate a clean result."""
    if not isinstance(data, dict):
        return None, True
    sections = {}
    known = False
    for key in ("violations", "unconnected_items", "schematic_parity"):
        if key in data:
            if not isinstance(data[key], list):
                raise _MalformedReport(key)
            sections[key] = _violation_dicts(data[key])
            known = True
    if "sheets" in data:
        raw_sheets = data.get("sheets")
        if not isinstance(raw_sheets, list):
            raise _MalformedReport("sheets")
        sheets = []
        for sheet in raw_sheets:
            if isinstance(sheet, dict):
                if "violations" in sheet and not isinstance(
                        sheet.get("violations"), list):
                    raise _MalformedReport("sheets[].violations")
                path = sheet.get("path")
                sheets.append((
                    path if isinstance(path, str) else "",
                    _violation_dicts(sheet.get("violations"))))
        sections["erc_sheets"] = sheets
        known = True
    if not known:
        return None, True
    return sections, False


def _dedup_key(v):
    items = v.get("items") or v.get("violations") or []
    identities = []
    if isinstance(items, list):
        for item in items[:MAX_ITEMS_PER_VIOLATION]:
            if isinstance(item, dict):
                identities.append(str(item.get("uuid")
                                      or item.get("description") or ""))
    # Position is part of identity (v0.6 review RT-12): the same
    # violation at three places is three findings, and an explicitly
    # excluded entry must never merge with its non-excluded twin
    # before the excluded check runs.
    _uuids, positions, _has = _item_identities(v)
    pos_key = tuple((round(x, 3), round(y, 3)) for x, y in positions[:2])
    return (str(v.get("severity", "")), str(v.get("type", "")),
            str(v.get("description", "")), tuple(sorted(identities)),
            bool(v.get("excluded") is True), pos_key)


def _dedup(violations):
    seen, unique = set(), []
    for v in violations:
        key = _dedup_key(v)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def _item_identities(v):
    """(uuids, positions_mm, has_items) of a violation's items, for
    exclusion matching. Poison-tolerant: non-dict items / non-numeric
    pos are skipped, never raised on. has_items records whether the
    violation carried item dicts at all — with items present but no
    parseable position, position checking is authoritative (the
    exclusion guard fails closed, RT-06)."""
    uuids, positions = [], []
    has_items = False
    items = v.get("items")
    if isinstance(items, list):
        for item in items[:MAX_ITEMS_PER_VIOLATION]:
            if not isinstance(item, dict):
                continue
            has_items = True
            uuid = item.get("uuid")
            if isinstance(uuid, str) and uuid:
                uuids.append(uuid)
            pos = item.get("pos")
            if isinstance(pos, dict):
                x, y = pos.get("x"), pos.get("y")
                if isinstance(x, (int, float)) \
                        and isinstance(y, (int, float)) \
                        and not isinstance(x, bool) \
                        and not isinstance(y, bool):
                    positions.append((float(x), float(y)))
    return uuids, positions, has_items


# ---------------------------------------------------------------------------
# One kicad-cli run
# ---------------------------------------------------------------------------


MAX_REPORT_BYTES = 64 * 1024 * 1024   # a report past this is not KiCad's


def _run_one(cli, args, source_path, out_name):
    """Run one kicad-cli check. Returns (data, status_line) where
    data is None when the run did not produce trustworthy output."""
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / out_name
        # Absolute path (v0.6 audit L1): a schematic/board whose
        # filename starts with "--" must reach kicad-cli as a path,
        # never as an option token.
        cmd = [cli] + args + ["--format", "json", "--output", str(out_path),
                              str(Path(source_path).resolve())]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"skipped — kicad-cli run failed ({exc})"
        if proc.returncode != 0:
            return None, (f"skipped — kicad-cli exited "
                          f"{proc.returncode}; its output was not "
                          "trusted")
        if not out_path.is_file():
            return None, "skipped — kicad-cli produced no report"
        try:
            if out_path.stat().st_size > MAX_REPORT_BYTES:
                return None, ("skipped — kicad-cli report was "
                              "unreasonably large; not trusted")
            data = json.loads(out_path.read_text(encoding="utf-8",
                                                 errors="replace"))
        except ValueError:
            return None, "skipped — kicad-cli report was not valid JSON"
        except RecursionError:
            # Pathologically nested JSON (RT-03): untrusted, like
            # any other unparseable report — never a crash.
            return None, ("skipped — kicad-cli report was nested too "
                          "deeply to parse; not trusted")
        return data, "ran"


# ---------------------------------------------------------------------------
# Mapping violations -> findings
# ---------------------------------------------------------------------------


class _Counts:
    """Per-run tallies that must reach Coverage, never vanish."""

    def __init__(self):
        self.excluded = 0            # "excluded": true / severity exclusion
        self.unrecognised = 0        # severities we cannot map
        self.overflow = 0            # entries past MAX_VIOLATIONS


def _map_violation(v, *, kind, rule, label, source_name, settings,
                   counts):
    """One report entry -> a Finding, or None (counted in `counts`
    or as a settings waiver on `settings`).

    Order matters: an explicitly excluded instance is dismissed
    before severity mapping (its severity is real but the user
    already judged this instance); a project 'ignore' / stored
    exclusion waives next; a project severity override then replaces
    the reported severity for the error/warning mapping."""
    vtype = v.get("type") if isinstance(v.get("type"), str) else ""
    severity_raw = v.get("severity")
    # Whitespace-normalised (RT-05): "error " is an error.
    severity_raw = severity_raw.strip().lower() \
        if isinstance(severity_raw, str) else ""
    description = v.get("description")
    message_text = description if isinstance(description, str) \
        and description else None
    if message_text is None:
        # Fall back to the first item's description — parity entries
        # in particular carry their meaning there (review F2).
        items = v.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(
                        item.get("description"), str) \
                        and item.get("description"):
                    message_text = item["description"]
                    break
    if message_text is None:
        message_text = vtype or "violation"

    if v.get("excluded") is True or severity_raw == "exclusion":
        counts.excluded += 1
        return None

    uuids, positions, has_items = _item_identities(v)
    if settings is not None and settings.present:
        if settings.waived(kind, vtype, item_uuids=uuids,
                           item_positions_mm=positions,
                           positions_authoritative=has_items):
            return None
        # The SAME deviation-only guard as the native path (RT-02):
        # a factory-default severity stated in .kicad_pro must not
        # rewrite the severity KiCad itself reported.
        override = kicadsettings.effective_override(
            settings, kind, vtype)
        if override == "ignore":
            # waived() already counted this path; reaching here with
            # an ignore override means waived() was bypassed by a
            # non-present settings quirk — still never a finding.
            return None
        if override in ("error", "warning"):
            severity_raw = override

    if severity_raw == "error":
        severity = BLOCK
    elif severity_raw == "warning":
        severity = CAUTION
    else:
        counts.unrecognised += 1
        return None
    return Finding(
        rule, severity,
        f"Official {label} ({vtype}): {message_text}" if vtype else
        f"Official {label}: {message_text}",
        where=source_name,
        evidence=f"severity={severity_raw or 'unknown'}")


def _findings_for_section(entries, *, kind, rule, label, source_name,
                          settings, counts):
    """Dedup, cap and map one report section."""
    unique = _dedup(entries)
    shown = unique[:MAX_VIOLATIONS]
    counts.overflow += len(unique) - len(shown)
    findings = []
    for v in shown:
        finding = _map_violation(
            v, kind=kind, rule=rule, label=label,
            source_name=source_name, settings=settings, counts=counts)
        if finding is not None:
            findings.append(finding)
    return findings, len(unique)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_official_checks(project, settings=None):
    """Returns (findings, coverage) where coverage describes what ran.

    ``settings`` is an optional kicadsettings.ProjectSettings; when
    not supplied it is loaded from the project's own .kicad_pro so
    existing callers get the v0.6 behaviour unchanged. The engine
    may pass one shared settings object so native-check waivers and
    adapter waivers accumulate in the same counters (see
    kicad-preflight-review/v06-ws1/INTEGRATION.md)."""
    coverage = {"available": False, "erc": "skipped — kicad-cli not found",
                "drc": "skipped — kicad-cli not found",
                "drc_parity": "skipped — kicad-cli not found",
                "failed": False, "parity_failed": False}
    if settings is None:
        settings = kicadsettings.load_for_project(project)
    coverage["settings"] = settings.coverage_stats()
    cli = find_cli()
    if cli is None:
        return [], coverage
    coverage["available"] = True
    coverage["path"] = cli
    findings = []
    counts = _Counts()

    def finish_job(key, data, builder):
        """Shared tail: build findings for one successful run and
        compose its Coverage status line."""
        try:
            job_findings, total = builder(data)
        except _MalformedReport:
            # A section with the wrong type is not "0 violations" —
            # the whole run's output is untrusted (RT-04).
            coverage[key] = ("skipped — kicad-cli report was "
                             "malformed (a section had the wrong "
                             "type); its output was not trusted")
            if key == "drc_parity":
                coverage["parity_failed"] = True
            else:
                coverage["failed"] = True
            return None
        findings.extend(job_findings)
        status = f"ran — {len(job_findings)} violation(s)"
        if total != len(job_findings):
            status += (f" ({total} report entr(ies) before "
                       "excluded/waived/unrecognised entries were "
                       "set aside)")
        if counts.overflow:
            status += (f" (showing first {MAX_VIOLATIONS} per section; "
                       f"{counts.overflow} more not listed)")
            counts.overflow = 0
        coverage[key] = status
        return total

    # --- ERC ---------------------------------------------------------------
    if project.sch_path is not None:
        data, status = _run_one(
            cli, ["sch", "erc", "--severity-all"], project.sch_path,
            "erc.json")
        if data is None:
            coverage["failed"] = True
            coverage["erc"] = status
        else:
            def build_erc(data, _src=project.sch_path):
                sections, fallback = _sections_from_report(data)
                out, total = [], 0
                if fallback:
                    entries, _trunc = _collect_violations_bounded(data)
                    got, n = _findings_for_section(
                        entries, kind="erc", rule=RULE_ERC, label="ERC",
                        source_name=_src.name, settings=settings,
                        counts=counts)
                    return got, n
                for sheet_path, entries in sections.get("erc_sheets", []):
                    where = _src.name if sheet_path in ("", "/") \
                        else f"{_src.name} · {sheet_path}"
                    got, n = _findings_for_section(
                        entries, kind="erc", rule=RULE_ERC, label="ERC",
                        source_name=where, settings=settings,
                        counts=counts)
                    out.extend(got)
                    total += n
                return out, total
            finish_job("erc", data, build_erc)

    # --- DRC (violations + unconnected items) ------------------------------
    if project.pcb_path is not None:
        data, status = _run_one(
            cli, ["pcb", "drc", "--severity-all"], project.pcb_path,
            "drc.json")
        if data is None:
            coverage["failed"] = True
            coverage["drc"] = status
        else:
            def build_drc(data, _src=project.pcb_path):
                sections, fallback = _sections_from_report(data)
                out, total = [], 0
                if fallback:
                    entries, _trunc = _collect_violations_bounded(data)
                    got, n = _findings_for_section(
                        entries, kind="drc", rule=RULE_DRC, label="DRC",
                        source_name=_src.name, settings=settings,
                        counts=counts)
                    return got, n
                for section in ("violations", "unconnected_items"):
                    got, n = _findings_for_section(
                        sections.get(section, []), kind="drc",
                        rule=RULE_DRC, label="DRC",
                        source_name=_src.name, settings=settings,
                        counts=counts)
                    out.extend(got)
                    total += n
                return out, total
            finish_job("drc", data, build_drc)

        # --- DRC schematic-parity (separate job, separate rule id) -----
        data, status = _run_one(
            cli, ["pcb", "drc", "--severity-all", "--schematic-parity"],
            project.pcb_path, "drc-parity.json")
        if data is None:
            coverage["parity_failed"] = True
            coverage["drc_parity"] = status
        else:
            def build_parity(data, _src=project.pcb_path):
                sections, fallback = _sections_from_report(data)
                if fallback:
                    # A report with no sections carries no parity
                    # evidence; the generic collector cannot tell
                    # parity entries from DRC violations, so nothing
                    # is surfaced rather than mislabelled.
                    return [], 0
                return _findings_for_section(
                    sections.get("schematic_parity", []), kind="drc",
                    rule=RULE_PARITY, label="DRC schematic-parity",
                    source_name=_src.name, settings=settings,
                    counts=counts)
            finish_job("drc_parity", data, build_parity)

    # Excluded / unrecognised tallies are per-run (not per-job), so
    # they live in structured keys rather than being appended to one
    # job's status line and misattributed.
    coverage["excluded_entries"] = counts.excluded
    coverage["unrecognised_severities"] = counts.unrecognised
    if counts.excluded or counts.unrecognised:
        coverage["excluded_note"] = (
            f"{counts.excluded} excluded and "
            f"{counts.unrecognised} unrecognised-severity entr(ies) "
            "across all kicad-cli jobs were not counted as findings")
    # Refresh the settings snapshot so adapter-applied waivers (which
    # mutate the shared object) are visible in Coverage.
    coverage["settings"] = settings.coverage_stats()
    return findings, coverage
