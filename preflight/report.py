"""Render a Report as terminal text, one-page Markdown, or JSON.

Output safety: every value interpolated into text/Markdown output is
sanitised — C0 control characters and ESC are replaced, newlines are
collapsed — because part values, references, filenames and titles are
attacker-controllable strings in an untrusted project folder (a crafted
MPN must not be able to forge a "VERDICT: PASS" line, break the
Markdown table, or inject terminal escapes). JSON needs no such help:
json.dumps escapes correctly, and is left untouched.
"""

import json
import re

from .finding import BLOCK, CAUTION, NOTE, SEVERITY_ORDER

_VERDICT_BLURB = {
    "PASS": "No blocking or caution findings. (Read Coverage — a "
            "verdict only covers what was actually checked.)",
    "CAUTION": "Nothing here breaks the build outright, but the "
               "CAUTION items are worth sorting before you order.",
    "BLOCK": "Do not order yet — the BLOCK items mean the board or "
             "the parts order will be wrong.",
}

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(text):
    """One-line, control-free rendering of an untrusted string."""
    return _CTRL.sub(" ", str(text)).strip()


def _md(text):
    return _clean(text).replace("|", "\\|")


def _grouped(findings):
    groups = {sev: [] for sev in SEVERITY_ORDER}
    for f in findings:
        groups[f.severity].append(f)
    return groups


def _coverage_lines(cov):
    lines = []
    sch = cov.get("schematic")
    if sch:
        extra = ""
        if len(sch.get("files_loaded", [])) > 1:
            extra = (f" (hierarchical: {len(sch['files_loaded'])} "
                     f"sheets loaded)")
        if sch.get("symbols_without_reference"):
            extra += (f"; {sch['symbols_without_reference']} symbol(s) "
                      "without a reference were excluded (see findings)")
        lines.append(
            f"Schematic: {sch['file']} — {sch['parts']} part(s), "
            f"{sch['no_connect_markers']} explicit no-connect "
            f"marker(s){extra}")
    else:
        lines.append("Schematic: NOT FOUND — cross-checks skipped")
    pcb = cov.get("pcb")
    if pcb:
        furniture = pcb["footprints"] - pcb.get("part_footprints",
                                                pcb["footprints"])
        extra = (f" ({furniture} board-furniture footprint(s) — "
                 "mounting holes, logos, labels — not counted as parts)"
                 if furniture else "")
        lines.append(
            f"PCB: {pcb['file']} — {pcb.get('part_footprints', pcb['footprints'])} "
            f"part footprint(s){extra}")
    else:
        lines.append("PCB: NOT FOUND — cross-checks skipped")
    bom = cov.get("bom")
    lines.append(
        f"BOM: {bom['file']} — {bom['rows']} row(s) "
        f"({bom.get('how_found', 'found')})"
        if bom else "BOM: no usable CSV found — BOM and part-risk "
                    "checks skipped (see findings)")
    stats = cov.get("bom_stats") or {}
    if stats.get("lcsc_only_rows"):
        lines.append(
            f"BOM identifiers: {stats['lcsc_only_rows']} row(s) are "
            "identified by LCSC/JLCPCB part number (no MPN) — accepted "
            "as identified")
    versions = cov.get("file_versions") or {}
    ver_bits = []
    for kind in ("schematic", "pcb"):
        entry = versions.get(kind)
        if entry:
            stamp = entry.get("format_version")
            gen = entry.get("generator_version")
            ver_bits.append(
                f"{kind} format {stamp if stamp is not None else '?'}"
                + (f" (KiCad {gen})" if gen else ""))
    if ver_bits:
        lines.append(
            "File versions: " + ", ".join(ver_bits)
            + f" — tested range: {versions.get('tested_range', '?')}")
    geo = cov.get("geometry")
    if geo:
        unreadable = geo.get("dimensions_unreadable", 0)
        lines.append(
            f"Board measurements: {geo['segments_measured']} track "
            f"segment(s), {geo['vias_measured']} via(s), "
            f"{geo['plated_drills_measured']} plated drill(s), "
            f"{geo['plated_rings_measured']} plated ring(s), "
            f"{geo['slots_measured']} slot(s) measured against fab "
            f"minimums; {geo['zones']} zone(s) "
            f"({geo['zones_unfilled']} unfilled); "
            f"{geo['edge_cuts_items']} outline item(s) on Edge.Cuts"
            + (f"; {unreadable} dimension value(s) unreadable "
               "(see findings)" if unreadable else ""))
    placement = cov.get("placement")
    if placement and placement.get("file"):
        files = placement.get("files") or []
        merged = (f" (merged {len(files)} position files)"
                  if len(files) > 1 else "")
        lines.append(
            f"Position file: {placement['file']} — "
            f"{placement.get('rows', 0)} row(s) checked against the "
            f"PCB (assembly orders only){merged}")
        for broken in placement.get("broken_files", []):
            lines.append(
                f"Position file not readable: {broken} (see findings)")
        if placement.get("error"):
            lines.append(
                f"Position file error: {placement['error']}")
    elif placement is not None and cov.get("pcb"):
        lines.append(
            "Position file: none found — CPL agreement not checked "
            "(only needed for assembly orders)")
    for err in cov.get("parse_errors", []):
        lines.append(f"Parse error: {err}")
    for legacy in cov.get("legacy_schematics", []):
        lines.append(f"Legacy schematic present (not supported): "
                     f"{legacy}")
    risk = cov.get("part_risk", {})
    lines.append(
        f"Part risk: {risk.get('mpns_checked', 0)} MPN(s) checked "
        f"against {risk.get('dataset')} (snapshot "
        f"{risk.get('snapshot_date')}, SAMPLE data — not live "
        f"distributor data); {risk.get('mpns_found', 0)} found in "
        "dataset")
    cli = cov.get("kicad_cli", {})
    if cli.get("available"):
        lines.append(f"kicad-cli: found at {cli.get('path')}")
        lines.append(f"  Official ERC: {cli.get('erc')}")
        lines.append(f"  Official DRC: {cli.get('drc')}")
    elif cov.get("mode"):
        lines.append("kicad-cli: not applicable — standalone BOM mode")
    else:
        lines.append("kicad-cli: NOT FOUND — official DRC/ERC skipped "
                     "(install KiCad to enable them); the built-in "
                     "text checks above still ran")
    return lines


def render_text(report):
    groups = _grouped(report.findings)
    out = []
    out.append(f"Preflight report for: {_clean(report.project_name)}")
    out.append(f"Project folder: {_clean(report.folder)}")
    out.append("")
    counts = ", ".join(f"{len(groups[s])} {s}" for s in SEVERITY_ORDER)
    out.append(f"Findings: {len(report.findings)} ({counts})")
    for sev in SEVERITY_ORDER:
        for f in groups[sev]:
            loc = f" — {_clean(f.where)}" if f.where else ""
            out.append(f"  [{sev}] {f.rule}{loc}")
            out.append(f"      {_clean(f.message)}")
            if f.evidence:
                out.append(f"      > {_clean(f.evidence)}")
    out.append("")
    out.append("Coverage — what was actually checked:")
    out.extend(f"  {_clean(line)}" for line in _coverage_lines(report.coverage))
    out.append("")
    out.append(f"VERDICT: {report.verdict}")
    out.append(_VERDICT_BLURB[report.verdict])
    return "\n".join(out) + "\n"


def render_markdown(report):
    groups = _grouped(report.findings)
    out = []
    out.append(f"# Preflight report — {_md(report.project_name)}")
    out.append("")
    out.append(f"**Verdict: {report.verdict}** — "
               f"{_VERDICT_BLURB[report.verdict]}")
    out.append("")
    counts = ", ".join(f"{len(groups[s])} {s}" for s in SEVERITY_ORDER)
    out.append(f"Findings: {len(report.findings)} ({counts}).")
    for sev in SEVERITY_ORDER:
        if not groups[sev]:
            continue
        out.append("")
        out.append(f"## {sev} ({len(groups[sev])})")
        out.append("")
        out.append("| Rule | Where | Finding |")
        out.append("| --- | --- | --- |")
        for f in groups[sev]:
            msg = _md(f.message)
            if f.evidence:
                msg += f" ({_md(f.evidence)})"
            out.append(f"| {f.rule} | {_md(f.where) or '—'} | {msg} |")
    out.append("")
    out.append("## Coverage — what was actually checked")
    out.append("")
    out.extend(f"- {_md(line)}" for line in _coverage_lines(report.coverage))
    out.append("")
    out.append("---")
    out.append("Generated by KiCad Preflight (open source, MIT). "
               "Part-risk data is the bundled SAMPLE snapshot, not "
               "live distributor data.")
    return "\n".join(out) + "\n"


def render_json(report):
    return json.dumps(report.to_dict(), indent=2) + "\n"


RENDERERS = {"text": render_text, "md": render_markdown, "json": render_json}
