"""Fabrication-readiness checks: board outline, zone fills, published
fab capability minimums, and position-file (CPL) agreement.

These checks read only the property-level measurements project.py
extracted from the .kicad_pcb text (segment widths, via/pad drills,
annular rings, slot widths, zone fill presence, outline presence) —
no geometry computation, no net parsing. Thresholds are the published
standard-process minimums of JLCPCB and PCBWay (official capability
pages, checked 2026-11-10; see docs/COVERAGE.md for sources). A value
below a *standard* minimum is a CAUTION, not a BLOCK: fabs raise an
engineering question or offer an advanced process rather than
hard-rejecting, and another fab may accept the board as-is. The one
BLOCK here is a missing board outline — without one there is nothing
to manufacture at all, and a missing Edge.Cuts outline is the most
common cause of a held order at both fabs.

Every rule fires only on a measured value. When the board contains no
tracks (or no vias, no drilled pads), the corresponding check abstains
and Coverage reports the zero count — absence of evidence is never
reported as compliance.
"""

from .finding import BLOCK, CAUTION, Finding

# Published standard-process minimums (mm), JLCPCB + PCBWay, 2-layer
# 1 oz copper, checked 2026-11-10 (docs/COVERAGE.md §sources).
TRACK_WIDTH_MIN = 0.10        # both fabs, standard minimum trace width
DRILL_MIN_BOTH = 0.15         # JLCPCB minimum mechanical drill
DRILL_MIN_PCBWAY = 0.20       # PCBWay standard minimum drill
VIA_DRILL_MIN_BOTH = 0.15     # JLCPCB minimum via drill
VIA_SIZE_MIN = 0.25           # JLCPCB minimum via diameter
RING_MIN = 0.15               # below every published PTH ring minimum
SLOT_MIN = 0.50               # both fabs, minimum plated slot width


def _fmt(value):
    return f"{value:.3f}".rstrip("0").rstrip(".") + " mm"


def run_fab_checks(project):
    findings = []
    if project.pcb_path is None or project.pcb_failed:
        return findings
    geo = project.geometry
    pcb_name = project.pcb_path.name

    # --- board outline ---------------------------------------------------
    # A footprint whose reference could not be read still proves the
    # board carries footprints (its pads were measured); the check
    # must not abstain just because the footprint list is empty.
    if (project.footprints or project.pcb_footprints_no_ref) \
            and geo.edge_items == 0:
        findings.append(Finding(
            "BOARD_OUTLINE_MISSING", BLOCK,
            "The PCB contains footprints but no board outline: no "
            "root-level drawing item on the Edge.Cuts layer "
            "(footprint-internal graphics are not counted). Without "
            "an outline there is nothing to cut — a missing or open "
            "outline is the most common reason both JLCPCB and "
            "PCBWay hold an order for an engineering question. Draw "
            "a closed outline on Edge.Cuts before ordering.",
            where=pcb_name))

    # --- zone fills -------------------------------------------------------
    if geo.zones_unfilled:
        findings.append(Finding(
            "ZONE_UNFILLED", CAUTION,
            f"{geo.zones_unfilled} of {geo.zones_total} copper zone(s) "
            "on the board have no filled polygon stored in the file. "
            "An unfilled (or stale) zone exports as missing copper — "
            "the pour you see in the editor is not in the Gerbers. "
            "Refill all zones (Edit → Fill All Zones, or B) and save "
            "before exporting.",
            where=pcb_name))

    # --- track width ------------------------------------------------------
    if geo.segment_widths:
        narrow = [w for w in geo.segment_widths if w < TRACK_WIDTH_MIN]
        if narrow:
            findings.append(Finding(
                "TRACK_WIDTH_BELOW_FAB_MIN", CAUTION,
                f"{len(narrow)} track segment(s) are narrower than "
                f"0.10 mm (thinnest: {_fmt(min(geo.segment_widths))}). "
                "0.10 mm is the published standard-process minimum "
                "trace width at both JLCPCB and PCBWay (1–2 layer, "
                "1 oz); thinner tracks need an advanced process or "
                "will be widened/held at engineering review.",
                where=pcb_name,
                evidence=f"segments={len(geo.segment_widths)} "
                         f"below_min={len(narrow)} "
                         f"min={min(geo.segment_widths):.3f}"))

    # --- drilled holes (plated pads) --------------------------------------
    if geo.pad_drills:
        smallest = min(geo.pad_drills)
        if smallest < DRILL_MIN_PCBWAY:
            if smallest < DRILL_MIN_BOTH:
                scope = ("below the standard minimum drill of both "
                         "JLCPCB (0.15 mm) and PCBWay (0.20 mm)")
            else:
                scope = ("below PCBWay's standard minimum drill "
                         "(0.20 mm); JLCPCB accepts down to 0.15 mm")
            findings.append(Finding(
                "DRILL_BELOW_FAB_MIN", CAUTION,
                f"The smallest plated drill on the board is "
                f"{_fmt(smallest)} — {scope}. Expect an engineering "
                "question, an advanced-process upcharge, or the hole "
                "being drilled at the fab's minimum instead.",
                where=pcb_name,
                evidence=f"plated_drills={len(geo.pad_drills)} "
                         f"min={smallest:.3f}"))

    # --- vias ---------------------------------------------------------------
    via_problems = []
    if geo.via_drills:
        smallest = min(geo.via_drills)
        if smallest < VIA_DRILL_MIN_BOTH:
            via_problems.append(
                f"smallest via drill {_fmt(smallest)} is below the "
                "0.15 mm standard minimum via drill (JLCPCB)")
        elif smallest < DRILL_MIN_PCBWAY:
            via_problems.append(
                f"smallest via drill {_fmt(smallest)} is below "
                "PCBWay's 0.20 mm standard minimum via drill")
    if geo.via_sizes:
        smallest_size = min(geo.via_sizes)
        if smallest_size < VIA_SIZE_MIN:
            via_problems.append(
                f"smallest via diameter {_fmt(smallest_size)} is below "
                "the 0.25 mm standard minimum via diameter (JLCPCB)")
    if via_problems:
        findings.append(Finding(
            "VIA_BELOW_FAB_MIN", CAUTION,
            "Via geometry below published standard minimums: "
            + "; ".join(via_problems) + ". Microvia-class vias need "
            "an advanced process at standard prices — fine if that "
            "is what you intended and your fab offers it, but it is "
            "not part of the standard process.",
            where=pcb_name,
            evidence=f"vias={max(len(geo.via_sizes), len(geo.via_drills))}"))

    # --- annular rings (plated pads only) ----------------------------------
    if geo.pad_rings:
        ring, ref = min(geo.pad_rings)
        if ring < RING_MIN:
            thin = sum(1 for r, _ in geo.pad_rings if r < RING_MIN)
            findings.append(Finding(
                "ANNULAR_RING_BELOW_FAB_MIN", CAUTION,
                f"{thin} plated pad(s) have an annular ring thinner "
                f"than 0.15 mm (thinnest: {_fmt(ring)} at {ref}). "
                "Published PTH ring minimums are 0.15 mm (tight tier) "
                "to 0.20 mm (standard) — a thinner ring risks "
                "breakout during drilling and a held order. "
                "Non-plated holes are excluded: their ring is zero "
                "by design.",
                where=f"{pcb_name} · {ref}",
                evidence=f"plated_pads={len(geo.pad_rings)} "
                         f"below_min={thin} min={ring:.3f}"))

    # --- plated slots ---------------------------------------------------------
    if geo.slot_widths:
        width, ref = min(geo.slot_widths)
        if width < SLOT_MIN:
            thin = sum(1 for w, _ in geo.slot_widths if w < SLOT_MIN)
            findings.append(Finding(
                "SLOT_BELOW_FAB_MIN", CAUTION,
                f"{thin} plated slot(s) are narrower than 0.50 mm "
                f"(narrowest: {_fmt(width)} at {ref}). 0.50 mm is "
                "the minimum plated slot width in both JLCPCB's and "
                "PCBWay's standard process; narrower slots are "
                "milled oversize or held for review.",
                where=f"{pcb_name} · {ref}",
                evidence=f"slots={len(geo.slot_widths)} "
                         f"below_min={thin} min={width:.3f}"))

    # --- dimensions that could not be read --------------------------------
    if geo.unparsed_dims:
        findings.append(Finding(
            "DIMENSION_UNREADABLE", CAUTION,
            f"{geo.unparsed_dims} dimension value(s) in the PCB "
            "(track widths, via or pad drills) are present but could "
            "not be read as numbers, so those items were NOT measured "
            "against the fab minimums above. The file may come from a "
            "third-party generator or a newer KiCad — check those "
            "dimensions by hand before ordering.",
            where=pcb_name,
            evidence=f"unreadable_dimensions={geo.unparsed_dims}"))
    return findings


def _short_ref(ref):
    """Length-capped rendering of an untrusted designator string —
    slicing by count alone lets 8 huge refs bloat a report."""
    return ref if len(ref) <= 40 else ref[:40] + "…"


def _ref_list(refs):
    return ", ".join(_short_ref(r) for r in refs[:8]) \
        + ("…" if len(refs) > 8 else "")


def run_cpl_checks(project):
    """Position-file (CPL) agreement with the PCB, when a CPL exists.

    These checks only matter for assembly orders — the messages say
    so — and only run when a position file was actually supplied.
    """
    from collections import Counter

    findings = []
    cpl_name = project.cpl_path.name if project.cpl_path else "CPL"
    if project.cpl_error:
        findings.append(Finding(
            "CPL_UNREADABLE", CAUTION,
            f"A position/CPL file was found but could not be parsed "
            f"({project.cpl_error[:300]}), so its agreement with the "
            "board was not checked. If you order assembly, the fab "
            "will read this file — fix or regenerate it.",
            where=cpl_name))
        if not project.cpl_refs:
            return findings
    for broken_name, broken_error in project.cpl_broken:
        findings.append(Finding(
            "CPL_UNREADABLE", CAUTION,
            f"Position file {broken_name} could not be parsed "
            f"({broken_error[:300]}); the agreement checks below "
            f"used the other position file(s) instead. If this file "
            "is part of your assembly upload, fix or remove it.",
            where=broken_name))
    if project.cpl_path is None:
        return findings
    if project.pcb_path is None or project.pcb_failed:
        return findings

    all_refs = {fp.ref for fp in project.footprints}
    # Footprints KiCad was told to keep out of position files
    # (exclude_from_pos_files: test points, shields, tooling) are not
    # expected in the CPL — blaming them is a false alarm.
    part_refs = {fp.ref for fp in project.footprints
                 if fp.is_part and not fp.exclude_from_pos}
    cpl_refs = set(project.cpl_refs)

    duplicates = sorted(r for r, n in Counter(project.cpl_refs).items()
                        if n > 1)
    if duplicates:
        findings.append(Finding(
            "CPL_DUPLICATE_REFERENCE", CAUTION,
            f"The position file lists {len(duplicates)} designator(s) "
            f"more than once ({_ref_list(duplicates)}). A duplicated "
            "placement row can double-place or confuse the "
            "assembler's uploader — regenerate the position file.",
            where=cpl_name))

    unknown = sorted(r for r in cpl_refs if r not in all_refs)
    if unknown:
        findings.append(Finding(
            "CPL_UNKNOWN_REFERENCE", CAUTION,
            f"The position file lists {len(unknown)} designator(s) "
            f"that do not exist on the PCB ({_ref_list(unknown)}). "
            "A CPL/BOM/board mismatch is a standard cause of a held "
            "assembly order — regenerate the position file from the "
            "current board.",
            where=cpl_name))
    # An existing but EMPTY position file lands here with every part
    # missing — a truncated export must not pass silently.
    missing = sorted(r for r in part_refs if r not in cpl_refs)
    if missing:
        findings.append(Finding(
            "CPL_PART_MISSING", CAUTION,
            f"{len(missing)} board part(s) have no row in the "
            f"position file ({_ref_list(missing)}). If you order "
            "assembly, those parts will not be placed. (Only matters "
            "for assembly orders; a PCB-only order does not need a "
            "position file.)",
            where=cpl_name))
    return findings
