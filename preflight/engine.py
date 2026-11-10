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
  7. Offline part-risk checks against the sample dataset (partrisk.py)
  8. Official kicad-cli DRC/ERC, when installed (kicadcli.py)

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

from . import bomchecks, checks, fabchecks, geometry, kicadcli, partrisk
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


def run(target, bom_override=None, dataset_path=None):
    """Run all checks on `target` and return a Report.

    Raises partrisk.DatasetError for a bad user-supplied dataset —
    the CLI turns that into a usage error (exit 3)."""
    project = load_project(target, bom_override=bom_override)
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
    findings.extend(geo_findings)
    bom_findings, bom_stats = checks.run_bom_layer(project)
    findings.extend(bom_findings)
    findings.extend(fabchecks.run_cpl_checks(project))

    meta, parts = partrisk.load_dataset(dataset_path)
    risk_findings, risk_stats = partrisk.run_part_risk_checks(
        project, meta, parts)
    findings.extend(risk_findings)

    cli_findings, cli_coverage = kicadcli.run_official_checks(project)
    findings.extend(cli_findings)
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
            "live_data": False,
            **risk_stats,
        },
        "kicad_cli": cli_coverage,
    }
    findings = _cap(findings)
    return Report(project_name=project.name,
                  folder=str(project.folder),
                  verdict=verdict_for(findings),
                  findings=findings,
                  coverage=coverage)


def run_bom_only(bom_path, dataset_path=None):
    """Standalone BOM mode: check a lone BOM CSV (any EDA tool's export).

    Row-internal BOM checks + part risk only. Coverage states plainly
    that no schematic was supplied, so no cross-checks ran.
    Raises ValueError (bad CSV) / DatasetError — CLI maps both to
    usage errors, since the user pointed at this file explicitly.
    """
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
            "live_data": False,
            **risk_stats,
        },
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
