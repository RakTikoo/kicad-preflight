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


_TAG_SHAPED = re.compile(r"<(/?[a-zA-Z][^<>\s]*(?:\s[^<>]*)?/?)>")


def _md(text):
    # Escape tag-shaped HTML (v0.6 audit L2): catalogue/BOM strings
    # are third-party data and a Markdown report may be rendered
    # through pipelines that pass raw HTML through. Only tag-shaped
    # sequences are escaped, so ordinary comparisons ("0.1 mm <
    # 0.127 mm") render unchanged.
    return _TAG_SHAPED.sub(r"&lt;\1&gt;",
                           _clean(text).replace("|", "\\|"))


def _grouped(findings):
    groups = {sev: [] for sev in SEVERITY_ORDER}
    for f in findings:
        groups[f.severity].append(f)
    return groups


def _coverage_lines(cov):
    lines = []
    gerber = cov.get("gerber")
    if gerber:
        layers = ", ".join(gerber.get("layers", [])) or "none"
        schemes = ", ".join(gerber.get("schemes", [])) or "unknown"
        lines.append(
            f"Fabrication package: {gerber.get('files_classified', 0)} "
            f"file(s) classified into layer roles ({layers}); "
            f"naming scheme(s): {schemes}")
        lines.append(
            "Package checks are file-level only (presence, size, "
            "naming consistency) — Gerber and drill file contents "
            "are never parsed, and no board design was checked")
        if gerber.get("unrecognised"):
            lines.append(
                f"Unrecognised fab-extension file(s): "
                f"{', '.join(gerber['unrecognised'])} (see findings)")
        return lines
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
    lite = cov.get("geometry_lite")
    if lite:
        bits = [
            f"Geometry-lite (subset — not KiCad DRC): "
            f"{lite.get('objects', 0)} copper object(s) measured "
            "with exact shapes (pads, tracks, arcs, vias"
            + (", filled zone polygons)"
               if lite.get("zones_modelled") else ")")]
        if lite.get("clearance_threshold") is not None:
            bits.append(
                f"clearance minimum "
                f"{lite['clearance_threshold']:g} mm "
                f"({lite.get('clearance_source', '')})")
        if lite.get("edge_threshold") is not None:
            bits.append(
                f"copper-to-edge minimum "
                f"{lite['edge_threshold']:g} mm "
                f"({lite.get('edge_source', '')})")
        if lite.get("outline_closed") is True:
            bits.append("outline closed")
        elif lite.get("outline_closed") is False:
            bits.append("outline NOT closed (see findings)")
        lines.append("; ".join(bits))
        skipped = []
        if lite.get("pads_shape_skipped"):
            skipped.append(
                f"{lite['pads_shape_skipped']} custom/trapezoid "
                "pad(s)")
        if lite.get("zones_modelled"):
            lines.append(
                f"Geometry-lite zones: {lite['zones_modelled']} "
                f"zone(s) ({lite.get('zones_polygons', 0)} filled "
                "polygon(s)) distance-checked against other copper "
                "and the board outline"
                + (f"; {lite['zones_edge_abstained']} zone "
                   "polygon(s) not edge-checked (the project "
                   "states no edge clearance of its own)"
                   if lite.get("zones_edge_abstained") else ""))
        if lite.get("zones_abstained"):
            skipped.append(
                f"{lite['zones_abstained']} zone(s) with no readable "
                "filled polygon")
        if skipped:
            lines.append(
                "Geometry-lite exclusions: " + " and ".join(skipped)
                + " not distance-checked; copper objects with no "
                  "stated net are never paired")
    netlist = cov.get("netlist")
    if netlist:
        if netlist.get("ran"):
            lines.append(
                f"Netlist (ERC-lite + parity, subset — not KiCad "
                f"ERC): {netlist.get('pins_resolved', 0)} schematic "
                f"pin(s) resolved into {netlist.get('nets_total', 0)} "
                f"net(s) ({netlist.get('nets_named', 0)} named); "
                f"{netlist.get('pins_unresolved', 0)} pin(s) "
                f"unresolved, {netlist.get('pins_bus_connected', 0)} "
                "bus-connected pin(s) excluded")
            if netlist.get("parity_ran"):
                lines.append(
                    f"Net parity: {netlist.get('parity_nets_compared', 0)} "
                    f"net(s) compared pin-by-pin over "
                    f"{netlist.get('parity_scope_refs', 0)} part(s); "
                    f"{len(netlist.get('parity_unmeasurable_refs', []))} "
                    "part(s) excluded as unmeasurable (see findings)")
        elif netlist.get("abstained"):
            lines.append(
                f"Netlist: not extracted ({netlist['abstained']}) — "
                "ERC-lite and parity checks did not run")
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
    if risk.get("live_data"):
        lines.append(
            f"Part risk: {risk.get('mpns_checked', 0)} MPN(s) checked "
            f"against {risk.get('dataset')} (snapshot "
            f"{risk.get('snapshot_date')}, SAMPLE data), superseded "
            "per MPN by live distributor data where the online "
            "lookup answered (see the Online parts line below); "
            f"{risk.get('mpns_found', 0)} found in dataset")
    else:
        lines.append(
            f"Part risk: {risk.get('mpns_checked', 0)} MPN(s) checked "
            f"against {risk.get('dataset')} (snapshot "
            f"{risk.get('snapshot_date')}, SAMPLE data — not live "
            f"distributor data); {risk.get('mpns_found', 0)} found in "
            "dataset")
    online = cov.get("online") or {}
    if online.get("status") not in (None, "off"):
        if online.get("status") == "unavailable":
            lines.append(
                "Online parts (opt-in): UNAVAILABLE "
                f"({online.get('reason') or 'provider error'}) — "
                "the offline results above stand unchanged")
        elif online.get("status") == "no-mpns":
            lines.append(
                "Online parts (opt-in): no BOM part numbers to "
                "look up")
        else:
            sem = online.get("semantic") or {}
            lines.append(
                f"Online parts (opt-in): {online.get('provider')} — "
                f"{online.get('answered', 0)}/{online.get('mpns', 0)} "
                f"MPN(s) answered, {online.get('not_found', 0)} not "
                f"found, {online.get('from_cache', 0)} served from "
                f"cache; semantic comparison: "
                f"{sem.get('compared_attributes', 0)} attribute(s) "
                f"compared, {sem.get('unstated_attributes', 0)} not "
                "stated by one side or the other (never inferred)")
    pset = cov.get("project_settings") or {}
    if pset.get("present") or pset.get("waivers_applied_total"):
        bits = []
        if pset.get("drc_severity_overrides") or \
                pset.get("erc_severity_overrides"):
            bits.append(
                f"{pset.get('drc_severity_overrides', 0) + pset.get('erc_severity_overrides', 0)} "
                "severity override(s)")
        if pset.get("drc_exclusions_stored") or \
                pset.get("erc_exclusions_stored"):
            bits.append(
                f"{pset.get('drc_exclusions_stored', 0) + pset.get('erc_exclusions_stored', 0)} "
                "stored exclusion(s)")
        if pset.get("waivers_applied_total"):
            bits.append(
                f"{pset['waivers_applied_total']} finding(s) waived "
                "by the project's own KiCad settings (see findings "
                "counts in the JSON report)")
        if bits:
            lines.append(
                "Project KiCad settings (.kicad_pro): "
                + ", ".join(bits))
    if pset.get("note") and not pset.get("present") \
            and pset.get("source"):
        lines.append(
            f"Project KiCad settings: {pset['note']}")
    cli = cov.get("kicad_cli", {})
    if cli.get("available"):
        lines.append(f"kicad-cli: found at {cli.get('path')}")
        lines.append(f"  Official ERC: {cli.get('erc')}")
        lines.append(f"  Official DRC: {cli.get('drc')}")
        if cli.get("drc_parity"):
            lines.append(
                f"  Official DRC schematic parity: "
                f"{cli['drc_parity']}")
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
