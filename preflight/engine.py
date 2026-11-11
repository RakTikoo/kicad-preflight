"""Preflight engine: load a project, run every check layer, build a report.

Layers, in order:
  1. File/parse integrity (project.py)
  2. Schematic <-> PCB cross-checks + schematic part checks (checks.py)
  3. KiCad file-format version handling (checks.run_version_checks)
  4. Fabrication readiness: outline, zone fills, fab capability
     minimums, position-file agreement (fabchecks.py)
  5. Geometry-lite distance checks: different-net copper
     clearance, copper-to-edge, outline closure from exact parsed
     shapes (geometry.py) — a conservative subset, not KiCad DRC
  6. BOM <-> schematic checks (bomchecks.py via checks.run_bom_layer)
  7. Offline part-risk checks against the sample dataset (partrisk.py);
     with --online, live distributor data (online.py, opt-in,
     bring-your-own-key) supersedes the dataset per MPN
  8. Official kicad-cli DRC/ERC, when installed (kicadcli.py) —
     honouring the project's own .kicad_pro severity settings and
     per-instance exclusions (kicadsettings.py), which also waive
     or re-map matching native findings below
  9. Netlist: ERC-lite (unconnected pins, undriven power nets,
     output conflicts, dangling labels) and schematic<->PCB
     pin-level parity against the PCB's modelled pads
     (netlist.py) — a conservative subset, not KiCad ERC/parity

The report's Coverage section states exactly what each layer did, and
the verdict accounts for it: anything that exists but could not be
checked produces a BLOCK, a CAUTION, or a SCAN_INCOMPLETE finding —
a clean PASS never hides a skipped check.

Also here: run_bom_only(), the standalone BOM mode. It runs the
tool-agnostic BOM layer (and part risk) on a lone CSV — the same code
path a future fab-site browser extension would use at checkout, where
no design-tool project exists at all.

And run_gerber_package(), the fabrication-output mode: file-level
checks on a folder of Gerbers + drill files (gerber.py), for the
moment after export and before upload, when no project runs.
"""

from dataclasses import dataclass, field
from pathlib import Path

from . import (bomchecks, checks, fabchecks, geometry, kicadcli,
               kicadsettings, netlist, partrisk)
from .bom import parse_bom
from .finding import BLOCK, CAUTION, NOTE, Finding, verdict_for
from .gerber import run_gerber_checks
from .project import load_project, read_project_file
from .sexpr import SexprError

MAX_FINDINGS = 2000          # report budget; overflow is stated, not silent


@dataclass
class Report:
    project_name: str
    folder: str
    verdict: str
    findings: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "project": self.project_name,
            "folder": self.folder,
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
            "coverage": self.coverage,
        }


def _cap(findings):
    if len(findings) <= MAX_FINDINGS:
        return findings
    kept = findings[:MAX_FINDINGS]
    kept.append(Finding(
        "FINDINGS_TRUNCATED", CAUTION,
        f"{len(findings) - MAX_FINDINGS} further finding(s) were not "
        f"listed — the report budget is {MAX_FINDINGS}. The verdict "
        "already reflects them; fix the listed items and re-run.",
        where="report"))
    return kept


# --- .kicad_pro waivers on native findings (v0.6, WS1 section 2) ------
#
# A project's own KiCad settings (severity map + per-instance
# exclusions in .kicad_pro) are the user's stated answer to "how
# serious is this class, and which instances did I already dismiss".
# The kicad-cli adapter honours them for official findings; the same
# settings object is applied here to the native findings that
# correspond to a KiCad check, so a project cannot waive a violation
# in KiCad and still be flagged for it natively. Mapping: native
# rule -> (settings kind, KiCad type). Exclusions match natively
# only when a finding's text carries the item UUID (kicadsettings
# is deliberately strict about identity); ignore-types apply by
# class. Severity overrides are honoured for the ERC-lite family
# and, severity-half only, for parity (net_conflict): the project's
# error/warning answer replaces Preflight's default mapping there.
# Geometry severities are NOT remapped (they encode fab-minimum
# semantics, not KiCad's), only dropped when waived.
_NATIVE_WAIVERS = {
    "ERC_UNCONNECTED_PIN": ("erc", "pin_not_connected"),
    "ERC_POWER_NOT_DRIVEN": ("erc", "power_pin_not_driven"),
    "ERC_OUTPUT_CONFLICT": ("erc", "pin_to_pin"),
    "ERC_PIN_CONFLICT": ("erc", "pin_to_pin"),
    "ERC_DANGLING_LABEL": ("erc", "label_dangling"),
    "GEOMETRY_CLEARANCE_BELOW_MIN": ("drc", "clearance"),
    "GEOMETRY_COPPER_TO_EDGE": ("drc", "copper_edge_clearance"),
    "BOARD_OUTLINE_OPEN": ("drc", "invalid_outline"),
}
_NATIVE_SEVERITY_REMAP = {
    "ERC_UNCONNECTED_PIN", "ERC_POWER_NOT_DRIVEN",
    "ERC_OUTPUT_CONFLICT", "ERC_PIN_CONFLICT", "ERC_DANGLING_LABEL",
}
_PARITY_RULES = {"PARITY_NET_MEMBERSHIP", "PARITY_NET_SCH_ONLY",
                 "PARITY_NET_PCB_ONLY", "PARITY_NET_RENAMED"}
_NATIVE_EXTRA_ALIASES = {
    # KiCad stores dangling global labels under their own type.
    "ERC_DANGLING_LABEL": ("global_label_dangling",),
}
def _project_override(settings, kind, type_):
    """The project's effective severity override for a KiCad type,
    or None. Delegates to kicadsettings.effective_override — the
    deviation-only rule (KiCad writes the full factory-default map
    into every .kicad_pro, so a stated severity equal to the
    factory default is not a project decision and must not remap
    anything) lives there so the native checks and the kicad-cli
    adapter apply it identically (v0.6 review RT-02)."""
    return kicadsettings.effective_override(settings, kind, type_)


def _apply_project_settings(findings, settings):
    """Drop native findings the project waives in .kicad_pro and
    apply its severity overrides where the mapping above allows.
    Waivers are counted by the settings object and disclosed in
    Coverage (coverage["project_settings"])."""
    if settings is None:
        return findings
    kept = []
    for f in findings:
        if f.rule in _PARITY_RULES:
            # Severity half only (WS1 caveat): per-instance
            # exclusions name KiCad violations, not native parity
            # results, so they are never reinterpreted here.
            override = _project_override(settings, "drc", "net_conflict")
            if override == "ignore":
                settings.record_waiver("drc", "net_conflict", "ignore")
                continue
            if override == "warning" and f.severity == BLOCK:
                f.severity = CAUTION
            elif override == "error" and f.severity != BLOCK:
                f.severity = BLOCK
            kept.append(f)
            continue
        entry = _NATIVE_WAIVERS.get(f.rule)
        if entry is None:
            kept.append(f)
            continue
        kind, ktype = entry
        # KiCad type strings only (v0.6 review RT-11): Preflight's
        # own rule ids are NOT a waiver vocabulary KiCad writes, so
        # they are never accepted as alias candidates. The one extra
        # alias is another KiCad type (global_label_dangling).
        aliases = _NATIVE_EXTRA_ALIASES.get(f.rule, ())
        if settings.waived(kind, ktype,
                           where_key=f"{f.where} {f.evidence}",
                           aliases=aliases):
            continue
        if f.rule in _NATIVE_SEVERITY_REMAP:
            override = _project_override(settings, kind, ktype)
            if override == "error" and f.severity != BLOCK:
                f.severity = BLOCK
            elif override == "warning" and f.severity == BLOCK:
                f.severity = CAUTION
        kept.append(f)
    return kept


# --- online supersede (v0.6, WS2 section 2) ----------------------------

_PART_RISK_RULES = {"PART_OBSOLETE", "PART_OUT_OF_STOCK",
                    "PART_NOT_RECOMMENDED", "PART_LOW_STOCK",
                    "PART_LONG_LEAD_TIME", "PART_SINGLE_SOURCE",
                    "PART_NOT_IN_SAMPLE_DATA",
                    "PART_DATASET_ENTRY_INVALID"}


def _mpn_of(finding):
    """The MPN a part-risk finding names: the text after the final
    '·' in its `where` (both partrisk and online findings use the
    '<file> line N · MPN' format)."""
    return finding.where.rsplit("·", 1)[-1].strip()


def _merge_online(offline_findings, online_findings, online_cov):
    """Live data replaces the sample-dataset result per MPN — but
    only as far as the live answer actually speaks (v0.6 review
    RT-01): for every MPN the provider answered WITH usable
    lifecycle/stock/lead data ('succeeded_mpns'), the whole offline
    part-risk family for that MPN is dropped and the online
    findings (possibly none — a healthy live part clears an
    offline flag) stand in their place. For an MPN the provider
    found but stated NO usable risk field for ('known_mpns'), only
    PART_NOT_IN_SAMPLE_DATA is superseded — the provider does know
    the part — while dataset risk findings stand, because silence
    about a field is not evidence about it. Not-found MPNs and
    total provider failure supersede nothing."""
    succeeded = set(online_cov.get("succeeded_mpns") or [])
    known = set(online_cov.get("known_mpns") or []) - succeeded
    if not succeeded and not known:
        return list(offline_findings) + list(online_findings)
    kept = []
    for f in offline_findings:
        if f.rule in _PART_RISK_RULES and _mpn_of(f) in succeeded:
            continue
        if f.rule == "PART_NOT_IN_SAMPLE_DATA" and _mpn_of(f) in known:
            continue
        kept.append(f)
    return kept + list(online_findings)


def run(target, bom_override=None, dataset_path=None,
        online=False, online_provider=None):
    """Run all checks on `target` and return a Report.

    Raises partrisk.DatasetError for a bad user-supplied dataset —
    the CLI turns that into a usage error (exit 3). With
    online=True, the opt-in live part-data layer runs (online.py,
    keys from environment variables only); the default False never
    imports it, so an offline run makes zero network calls and its
    output is unchanged."""
    project = load_project(target, bom_override=bom_override)
    settings = kicadsettings.load_for_project(project)
    findings = []

    for path, error in project.parse_errors:
        findings.append(Finding(
            "PROJECT_FILE_UNPARSEABLE", BLOCK,
            f"{path.name} exists but could not be parsed as a KiCad "
            f"S-expression file ({error}). Preflight cannot check a "
            "file it cannot read, and checks depending on it were "
            "skipped rather than guessed.",
            where=path.name))

    findings.extend(checks.run_cross_checks(project))
    findings.extend(checks.run_version_checks(project))
    findings.extend(fabchecks.run_fab_checks(project))
    geo_findings, geo_stats = geometry.run_geometry_checks(project)
    findings.extend(_apply_project_settings(geo_findings, settings))
    bom_findings, bom_stats = checks.run_bom_layer(project)
    findings.extend(bom_findings)
    findings.extend(fabchecks.run_cpl_checks(project))

    meta, parts = partrisk.load_dataset(dataset_path)
    risk_findings, risk_stats = partrisk.run_part_risk_checks(
        project, meta, parts)
    online_cov = {"status": "off"}
    if online:
        from . import online as online_mod  # lazy: off == never imported
        o_findings, o_cov = online_mod.online_checks(
            project.bom_rows, provider=online_provider)
        online_cov = o_cov["online"]
        risk_findings = _merge_online(risk_findings, o_findings,
                                      online_cov)
    findings.extend(risk_findings)

    cli_findings, cli_coverage = kicadcli.run_official_checks(
        project, settings=settings)
    findings.extend(cli_findings)

    net_findings, net_stats = netlist.run_netlist_checks(project)
    findings.extend(_apply_project_settings(net_findings, settings))
    if cli_coverage.get("failed"):
        findings.append(Finding(
            "KICAD_CLI_FAILED", CAUTION,
            "kicad-cli is installed but a DRC/ERC run failed, so the "
            "official checks did not contribute to this report. The "
            "built-in checks above still ran; see Coverage for the "
            "failure detail.",
            where="kicad-cli"))

    # --- SCAN_INCOMPLETE: things that exist but were not checked -------
    incomplete = []
    if project.unloaded_sch:
        incomplete.append(
            "schematic file(s) not loaded: "
            + ", ".join(p.name for p in project.unloaded_sch)
            + " (a folder should hold one project; extra files belong "
              "to another project or a sub-sheet reached from the "
              "root sheet)")
    if project.unloaded_pcb:
        incomplete.append(
            "PCB file(s) not loaded: "
            + ", ".join(p.name for p in project.unloaded_pcb))
    if risk_stats.get("dataset_entries_invalid"):
        incomplete.append(
            f"{risk_stats['dataset_entries_invalid']} dataset "
            "entr(ies) were unusable (see PART_DATASET_ENTRY_INVALID)")
    if geo_stats.get("objects_unplaced"):
        incomplete.append(
            f"{geo_stats['objects_unplaced']} geometric object(s) in "
            "the PCB had unreadable coordinates and were excluded "
            "from the geometry-lite distance checks")
    if geo_stats.get("outline_abstained"):
        incomplete.append(
            "the Edge.Cuts outline could not be closure-checked "
            "(too many elements, or an endpoint too close to the "
            "matching tolerance to judge)")
    if geo_stats.get("edge_abstained"):
        incomplete.append(
            "the copper-to-edge check did not run (the subdivided "
            "board outline exceeded the geometry budget)")
    if geo_stats.get("zones_abstained"):
        incomplete.append(
            f"{geo_stats['zones_abstained']} zone(s) had no stored "
            "filled polygon (absent, stale or empty fill) and were "
            "excluded from the geometry-lite zone checks")
    if geo_stats.get("zones_budget_exceeded"):
        incomplete.append(
            "the geometry-lite zone check exceeded its budget and "
            "did not run")
    if geo_stats.get("zones_containment_abstained"):
        incomplete.append(
            "zone containment (point-in-fill) testing exceeded its "
            "budget; zone edge distances still ran")
    if cli_coverage.get("parity_failed"):
        incomplete.append(
            "kicad-cli's schematic-parity DRC run failed, so the "
            "official parity check did not contribute (the built-in "
            "parity checks above still ran)")
    _unrec = int(cli_coverage.get("unrecognised_severities", 0) or 0)
    if _unrec:
        # A violation whose severity string we could not map was
        # dropped, not guessed (v0.6 review RT-05): a PASS must not
        # hide that in a Coverage counter.
        incomplete.append(
            f"{_unrec} kicad-cli violation(s) carried a severity "
            "Preflight does not recognise and were not shown")
    if settings.source_path and not settings.present and settings.note:
        incomplete.append(
            "the project's KiCad settings (.kicad_pro) were present "
            f"but could not be applied: {settings.note}")
    if net_stats.get("budget_exceeded"):
        incomplete.append(
            "netlist extraction exceeded its budget (pins/wires/"
            "labels/bus segments/bus-touch work), so the ERC-lite "
            "and parity checks did not run")
    if net_stats.get("sheets_multi_instanced"):
        incomplete.append(
            "netlist extraction abstained on multiply-instanced "
            "sheet file(s) (the same sheet file used by 2+ sheet "
            "blocks; sheets are resolved per file, not per sheet "
            "instance): "
            + ", ".join(
                f"{m.get('file')} x{m.get('count')}"
                for m in net_stats["sheets_multi_instanced"])
            + ", so the ERC-lite and parity checks did not run")
    if net_stats.get("parity_budget_exceeded"):
        incomplete.append(
            "schematic<->PCB parity exceeded its matching work "
            "budget, so the parity checks did not run (the "
            "ERC-lite checks are unaffected)")
    if net_stats.get("zero_pins_resolved"):
        incomplete.append(
            "the schematic has connectivity constructs and pin "
            "definitions, but no placed symbol's pins could be "
            "resolved from them, so the ERC-lite and parity checks "
            "did not run")
    if net_stats.get("parity_ran") \
            and net_stats.get("parity_unmeasurable_refs"):
        incomplete.append(
            f"{len(net_stats['parity_unmeasurable_refs'])} part(s) "
            "were excluded from schematic<->PCB net parity because "
            "their PCB pads could not be modelled in either sense "
            "(custom pads with unmodelled primitives, or unplaced "
            "pads) or their schematic pins could not be resolved: "
            + ", ".join(net_stats["parity_unmeasurable_refs"][:12]))
    if incomplete:
        findings.append(Finding(
            "SCAN_INCOMPLETE", CAUTION,
            "Some input was present but not checked: "
            + "; ".join(incomplete)
            + ". Treat a PASS here as covering only what Coverage "
              "lists.",
            where=str(project.folder)))

    coverage = {
        "schematic": (
            {"file": project.sch_path.name,
             "files_loaded": [p.name for p in project.sch_paths_loaded],
             "parts": len(checks._sch_parts(project)),
             "no_connect_markers": project.no_connects,
             "symbols_without_reference": project.sch_symbols_no_ref}
            if project.sch_path else None),
        "pcb": (
            {"file": project.pcb_path.name,
             "footprints": len(project.footprints),
             "part_footprints": sum(1 for fp in project.footprints
                                    if fp.is_part),
             "footprints_without_reference":
                 project.pcb_footprints_no_ref}
            if project.pcb_path else None),
        "bom": (
            {"file": project.bom_path.name,
             "rows": len(project.bom_rows),
             "how_found": project.bom_auto_note}
            if project.bom_path else None),
        "file_versions": {
            "schematic": (
                {"format_version": project.sch_format_version,
                 "generator_version": project.sch_generator or None}
                if project.sch_path else None),
            "pcb": (
                {"format_version": project.pcb_format_version,
                 "generator_version": project.pcb_generator or None}
                if project.pcb_path else None),
            "tested_range": "KiCad 6-8 file formats",
        },
        "geometry": (
            {"segments_measured": len(project.geometry.segment_widths),
             "vias_measured": max(len(project.geometry.via_sizes),
                                  len(project.geometry.via_drills)),
             "plated_drills_measured": len(project.geometry.pad_drills),
             "plated_rings_measured": len(project.geometry.pad_rings),
             "slots_measured": len(project.geometry.slot_widths),
             "zones": project.geometry.zones_total,
             "zones_unfilled": project.geometry.zones_unfilled,
             "edge_cuts_items": project.geometry.edge_items,
             "dimensions_unreadable": project.geometry.unparsed_dims}
            if project.pcb_path else None),
        "geometry_lite": geo_stats if project.pcb_path else None,
        "netlist": net_stats if project.sch_path else None,
        "placement": (
            {"file": project.cpl_path.name,
             "files": [Path(p).name for p in project.cpl_files],
             "rows": len(project.cpl_refs),
             "how_found": project.cpl_auto_note,
             "broken_files": [name for name, _ in project.cpl_broken],
             "error": project.cpl_error or None}
            if project.cpl_path else
            {"file": None, "how_found": project.cpl_auto_note}),
        "bom_stats": bom_stats,
        "parse_errors": [f"{p.name}: {e}" for p, e in project.parse_errors],
        "legacy_schematics": [p.name for p in project.legacy_sch],
        "part_risk": {
            "dataset": meta.get("name", "sample dataset"),
            "snapshot_date": meta.get("snapshot_date", "unknown"),
            "live_data": online_cov.get("status") in ("ok", "partial"),
            **risk_stats,
        },
        "online": online_cov,
        "project_settings": settings.coverage_stats(),
        "kicad_cli": cli_coverage,
    }
    findings = _cap(findings)
    return Report(project_name=project.name,
                  folder=str(project.folder),
                  verdict=verdict_for(findings),
                  findings=findings,
                  coverage=coverage)


def run_bom_only(bom_path, dataset_path=None,
                 online=False, online_provider=None):
    """Standalone BOM mode: check a lone BOM CSV (any EDA tool's export).

    Row-internal BOM checks + part risk only. Coverage states plainly
    that no schematic was supplied, so no cross-checks ran.
    Raises ValueError (bad CSV) / DatasetError — CLI maps both to
    usage errors, since the user pointed at this file explicitly.
    With online=True the opt-in live part-data layer runs exactly as
    in project mode (see run())."""
    bom_path = Path(bom_path)
    rows = parse_bom(read_project_file(bom_path))
    findings, stats = bomchecks.run_bom_checks(
        rows, bom_name=bom_path.name, expected=None, all_refs=None)

    meta, parts = partrisk.load_dataset(dataset_path)

    @dataclass
    class _Shim:
        bom_rows: list
        bom_path: Path
    risk_findings, risk_stats = partrisk.run_part_risk_checks(
        _Shim(bom_rows=rows, bom_path=bom_path), meta, parts)
    online_cov = {"status": "off"}
    if online:
        from . import online as online_mod  # lazy: off == never imported
        o_findings, o_cov = online_mod.online_checks(
            rows, provider=online_provider)
        online_cov = o_cov["online"]
        risk_findings = _merge_online(risk_findings, o_findings,
                                      online_cov)
    findings.extend(risk_findings)
    findings.append(Finding(
        "BOM_ONLY_MODE", NOTE,
        "Standalone BOM mode: no schematic or PCB was supplied, so "
        "only BOM-internal checks and part-risk checks ran. "
        "Schematic cross-checks (unknown references, missing parts, "
        "footprint agreement) did not run.",
        where=bom_path.name))
    findings = _cap(findings)
    coverage = {
        "mode": "standalone BOM (no design files)",
        "schematic": None,
        "pcb": None,
        "bom": {"file": bom_path.name, "rows": len(rows),
                "how_found": "given on the command line"},
        "bom_stats": stats,
        "parse_errors": [],
        "legacy_schematics": [],
        "part_risk": {
            "dataset": meta.get("name", "sample dataset"),
            "snapshot_date": meta.get("snapshot_date", "unknown"),
            "live_data": online_cov.get("status") in ("ok", "partial"),
            **risk_stats,
        },
        "online": online_cov,
        "kicad_cli": {"available": False,
                      "erc": "not applicable — standalone BOM mode",
                      "drc": "not applicable — standalone BOM mode",
                      "failed": False},
    }
    return Report(project_name=f"BOM: {bom_path.name}",
                  folder=str(bom_path.parent),
                  verdict=verdict_for(findings),
                  findings=findings,
                  coverage=coverage)


def run_gerber_package(folder):
    """Fabrication-output mode: file-level checks on a folder of
    exported Gerbers + drill files (gerber.py). No project is loaded;
    Coverage states plainly that only file presence/consistency was
    checked, never Gerber contents."""
    folder = Path(folder)
    findings, gerber_coverage = run_gerber_checks(folder)
    names = []
    try:
        names = sorted(p.name for p in folder.iterdir()
                       if p.is_file() and not p.name.startswith("."))
    except OSError:
        names = []
    findings = _cap(findings)
    coverage = {
        "mode": "fabrication output package (file-level checks only)",
        "schematic": None,
        "pcb": None,
        "bom": None,
        "bom_stats": {},
        "parse_errors": [],
        "legacy_schematics": [],
        "part_risk": {"dataset": "not applicable — package mode",
                      "snapshot_date": "n/a", "live_data": False},
        "kicad_cli": {"available": False,
                      "erc": "not applicable — package mode",
                      "drc": "not applicable — package mode",
                      "failed": False},
        "gerber": gerber_coverage,
        "files": names[:200],
    }
    return Report(project_name=f"Gerber package: {folder.name}",
                  folder=str(folder),
                  verdict=verdict_for(findings),
                  findings=findings,
                  coverage=coverage)
