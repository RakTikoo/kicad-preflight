"""Deterministic cross-checks between schematic, PCB, and BOM.

Every check reads only what project.py parsed from KiCad's text formats —
no geometry, no netlist. Each finding names the rule, the part, and the
exact values that disagree, so a maker can verify it by hand in seconds.

BOM-vs-schematic row checks live in bomchecks.py (tool-agnostic); this
module holds the schematic<->PCB cross-checks, the schematic-side part
checks, and the project-shape checks (missing files, unparseable
coverage guards). project.py's parse already guarantees:

  * KiCad 6 and 7/8 footprint storage both parse (fp_text + property),
  * only real component positions take part in PCB membership checks
    (FootprintInst.is_part: designator ref, not board_only) — mounting
    holes, logos and silkscreen labels are board furniture,
  * hierarchical sub-sheets are loaded and merged.
"""

from collections import Counter, defaultdict

from .bomchecks import PLACEHOLDER_MPNS
from .finding import BLOCK, CAUTION, NOTE, Finding


def _sch_parts(project):
    """Physical schematic parts: real components placed on the board.

    Power symbols (#PWR...) and symbols marked 'not on board' are excluded.
    Multi-unit symbols (U1 units 1..4) collapse to one part per reference.
    """
    parts = {}
    for sym in project.symbols:
        if sym.is_power or not sym.on_board:
            continue
        parts.setdefault(sym.ref, sym)
    return parts


def _bom_parts(project):
    """Schematic parts that belong in the BOM (excludes DNP / not-in-BOM)."""
    return {ref: sym for ref, sym in _sch_parts(project).items()
            if sym.in_bom and not sym.dnp}


def _norm(text):
    return " ".join((text or "").strip().lower().split())


def run_cross_checks(project):
    findings = []
    sch_name = project.sch_path.name if project.sch_path else "schematic"
    pcb_name = project.pcb_path.name if project.pcb_path else "PCB"

    # --- no project files at all ---------------------------------------
    if project.sch_path is None and project.pcb_path is None:
        findings.append(Finding(
            "NO_PROJECT_FILES", BLOCK,
            "No schematic and no PCB file were found, so nothing could "
            "be checked. A PASS here would be meaningless — point "
            "Preflight at a folder containing a .kicad_sch or "
            ".kicad_pcb file.",
            where=str(project.folder)))
        return findings

    # --- missing counterpart files -------------------------------------
    if project.sch_path is not None and project.pcb_path is None:
        findings.append(Finding(
            "MISSING_PCB_FILE", BLOCK,
            "This project has a schematic but no .kicad_pcb file, so the "
            "schematic-to-board cross-checks could not run. Export or "
            "create the PCB before ordering.",
            where=sch_name))
    if project.pcb_path is not None and project.sch_path is None:
        legacy = (f" A legacy KiCad <=5 schematic "
                  f"({', '.join(p.name for p in project.legacy_sch)}) is "
                  "present but not supported in this version."
                  if project.legacy_sch else "")
        findings.append(Finding(
            "MISSING_SCHEMATIC_FILE", BLOCK,
            "This project has a PCB but no .kicad_sch file, so the "
            "schematic-to-board cross-checks could not run." + legacy,
            where=pcb_name))
    if project.sch_path is None or project.pcb_path is None:
        return findings

    # --- a referenced sub-sheet is missing ------------------------------
    for sheet_file in project.missing_sheets:
        findings.append(Finding(
            "MISSING_SHEET_FILE", BLOCK,
            f"The schematic references sub-sheet '{sheet_file}', but "
            "that file is not in the project folder. Its parts were "
            "not checked.",
            where=sch_name))

    # --- parse-failure guard: don't fabricate findings from an empty side
    if project.sch_failed or project.pcb_failed:
        return findings

    sch_parts = _sch_parts(project)
    pcb_parts = [fp for fp in project.footprints if fp.is_part]
    pcb_by_ref = {}
    for fp in pcb_parts:
        pcb_by_ref.setdefault(fp.ref, fp)

    # --- coverage guard: a non-empty PCB that yielded no parts ----------
    if sch_parts and project.footprints and not pcb_parts:
        pass  # all footprints are board furniture; membership below is
              # still meaningful (every sch part is missing) — but that
              # is vanishingly unlikely; fall through.
    if sch_parts and not project.footprints:
        findings.append(Finding(
            "PCB_NO_FOOTPRINTS_PARSED", BLOCK,
            "The PCB file parsed but contains no footprint entries at "
            "all, while the schematic has parts. Either the board is "
            "genuinely unplaced, or the file uses a format this "
            "version cannot read — either way the cross-checks cannot "
            "be trusted, so no per-part blame is assigned.",
            where=pcb_name))
        return findings

    # --- unannotated symbols ----------------------------------------------
    if project.sch_symbols_no_ref:
        findings.append(Finding(
            "UNANNOTATED_SYMBOL", CAUTION,
            f"{project.sch_symbols_no_ref} placed schematic symbol(s) "
            "have a value/footprint but no reference designator. They "
            "were excluded from every check — annotate the schematic "
            "so each part has a reference.",
            where=sch_name))

    # --- duplicate reference designators ---------------------------------
    sch_keys = Counter((s.ref, s.unit) for s in project.symbols
                       if not s.is_power)
    for (ref, unit), count in sorted(sch_keys.items()):
        if count > 1:
            findings.append(Finding(
                "DUPLICATE_REFERENCE_SCHEMATIC", BLOCK,
                f"Reference {ref} (unit {unit}) appears {count} times "
                "in the schematic. Two parts with one reference means "
                "the BOM and the board cannot both be right — "
                "re-annotate.",
                where=f"{sch_name} · {ref}"))
    pcb_counts = Counter(fp.ref for fp in pcb_parts)
    for ref, count in sorted(pcb_counts.items()):
        if count > 1:
            findings.append(Finding(
                "DUPLICATE_REFERENCE_PCB", BLOCK,
                f"Reference {ref} appears {count} times on the PCB. "
                "Duplicate designators corrupt the position file and "
                "assembly — re-annotate the schematic and update the "
                "board.",
                where=f"{pcb_name} · {ref}"))

    # --- multi-unit divergence --------------------------------------------
    by_ref = defaultdict(list)
    for sym in project.symbols:
        if not sym.is_power:
            by_ref[sym.ref].append(sym)
    for ref in sorted(by_ref):
        units = by_ref[ref]
        if len(units) < 2:
            continue
        lib_ids = {s.lib_id for s in units if s.lib_id}
        if len(lib_ids) > 1:
            findings.append(Finding(
                "MULTI_UNIT_PART_CONFLICT", BLOCK,
                f"{ref} is used by {len(units)} symbols with different "
                f"library parts ({', '.join(sorted(lib_ids))}). Two "
                "different parts sharing one reference will collapse "
                "into a single BOM line — one of them will never be "
                "ordered. Give each part its own reference.",
                where=f"{sch_name} · {ref}"))
            continue
        footprints = {s.footprint for s in units
                      if s.footprint and s.footprint.strip()}
        if len(footprints) > 1:
            findings.append(Finding(
                "MULTI_UNIT_PROPERTY_DIVERGENCE", CAUTION,
                f"The units of {ref} disagree on the footprint "
                f"({', '.join(sorted(footprints))}). Only one footprint "
                "can be placed — make the units agree.",
                where=f"{sch_name} · {ref}"))

    # --- schematic <-> PCB membership --------------------------------------
    for ref in sorted(sch_parts):
        if ref not in pcb_by_ref:
            findings.append(Finding(
                "SCH_PART_NOT_ON_PCB", BLOCK,
                f"{ref} ({sch_parts[ref].value or 'part'}) is in the "
                "schematic but has no footprint on the PCB. It will not "
                "be on the board you receive — place it or remove it "
                "from the schematic.",
                where=f"{sch_name} · {ref}",
                evidence="in schematic, missing from PCB"))
    for ref in sorted(pcb_by_ref):
        if ref not in sch_parts:
            findings.append(Finding(
                "PCB_PART_NOT_IN_SCHEMATIC", BLOCK,
                f"{ref} is placed on the PCB but does not exist in the "
                "schematic. The schematic is the source of truth — a "
                "stray footprint usually means the board was edited by "
                "hand.",
                where=f"{pcb_name} · {ref}",
                evidence="on PCB, missing from schematic"))

    # --- footprint agreement + schematic-side part checks ------------------
    no_datasheet = []
    mismatch_groups = {}   # (sch_fp, pcb_fp) -> [refs]
    for ref in sorted(sch_parts):
        sym = sch_parts[ref]
        where = f"{sch_name} · {ref}"
        if sym.footprint is None or sym.footprint.strip() == "":
            findings.append(Finding(
                "SCHEMATIC_FOOTPRINT_MISSING", CAUTION,
                f"{ref} has no footprint assigned in the schematic. "
                "Assign one before exporting — an unassigned footprint "
                "becomes a missing or wrong part at assembly.",
                where=where))
        else:
            if ":" not in sym.footprint:
                findings.append(Finding(
                    "FOOTPRINT_NO_LIBRARY", CAUTION,
                    f"{ref}'s footprint '{sym.footprint}' has no "
                    "library prefix (expected 'Library:Footprint'). "
                    "Without it the footprint cannot be re-exported "
                    "or shared reliably.",
                    where=where))
            elif ref in pcb_by_ref \
                    and _norm(pcb_by_ref[ref].name) != _norm(sym.footprint):
                # Aggregated after the loop: 24 identical disagreements
                # (e.g. every keyswitch on a keyboard board) are ONE
                # finding with 24 refs, not 24 findings.
                key = (sym.footprint, pcb_by_ref[ref].name)
                mismatch_groups.setdefault(key, []).append(ref)
        if not (sym.value or "").strip() or sym.value.strip() == "~":
            findings.append(Finding(
                "SCHEMATIC_VALUE_MISSING", CAUTION,
                f"{ref} has no value in the schematic. The BOM line "
                "for it will be guesswork.",
                where=where))
        mpn = sym.mpn
        if mpn is not None and _norm(mpn) in PLACEHOLDER_MPNS:
            findings.append(Finding(
                "SCHEMATIC_PLACEHOLDER_MPN", CAUTION,
                f"{ref}'s schematic MPN property is a placeholder "
                f"('{mpn}'). If the BOM inherits it, the order will "
                "too.",
                where=where))
        if sym.datasheet is not None and not sym.datasheet.strip():
            no_datasheet.append(ref)
    for (sch_fp, pcb_fp), refs in sorted(mismatch_groups.items()):
        refs_label = ", ".join(refs[:10]) + ("…" if len(refs) > 10 else "")
        findings.append(Finding(
            "FOOTPRINT_MISMATCH", BLOCK,
            f"{len(refs)} part(s) ({refs_label}): the schematic asks "
            f"for footprint '{sch_fp}' but the PCB has '{pcb_fp}'. "
            "The soldered part will not fit both — they disagree.",
            where=f"{sch_name} · {refs[0]}",
            evidence=f"schematic={sch_fp}  pcb={pcb_fp}  "
                     f"refs={len(refs)}"))
    if no_datasheet:
        findings.append(Finding(
            "SCHEMATIC_DATASHEET_MISSING", NOTE,
            f"{len(no_datasheet)} part(s) have an empty Datasheet "
            f"property in the schematic ({', '.join(no_datasheet[:8])}"
            f"{'…' if len(no_datasheet) > 8 else ''}). Fine for "
            "passives; for the one special part, the datasheet is "
            "where the pinout truth lives.",
            where=sch_name))
    return findings


def run_bom_layer(project):
    """BOM <-> schematic layer, delegating row checks to bomchecks.py.

    Kept as a thin adapter so checks.py's public shape stays stable
    for the engine; the logic itself is tool-agnostic.
    """
    from . import bomchecks

    findings = []
    bom_name = project.bom_path.name if project.bom_path else "BOM"
    sch_name = project.sch_path.name if project.sch_path else "schematic"
    if project.bom_error:
        findings.append(Finding(
            "BOM_UNREADABLE", BLOCK,
            f"The BOM file could not be read as a BOM CSV: "
            f"{project.bom_error}",
            where=bom_name))
        return findings, {}
    if project.bom_path is None:
        if project.sch_path is not None:
            findings.append(Finding(
                "BOM_MISSING", CAUTION,
                "No BOM CSV was found in the project folder, so the "
                "BOM sanity and part-risk checks did not run. Export "
                "the BOM before ordering — that file is what the "
                "distributor and assembler actually see.",
                where=str(project.folder)))
        return findings, {}
    if project.sch_path is None or project.sch_failed:        # Standalone-ish: row-internal checks only; schematic-dependent
        # claims would be fabricated from an unreadable schematic.
        rows_findings, stats = bomchecks.run_bom_checks(
            project.bom_rows, bom_name=bom_name, expected=None,
            all_refs=None)
        return findings + rows_findings, stats

    expected = _bom_parts(project)
    # Existence for BOM purposes (FP-tester fix, v0.5 fix wave): a
    # ref exists if ANY non-power symbol carries it -- including
    # on_board=no symbols (e.g. a mating connector ordered via the
    # BOM), which _sch_parts (physical parts only) excludes.  The
    # "expected in BOM" set above stays _bom_parts, so DNP /
    # not-in-BOM / not-on-board handling is otherwise as before:
    # such a ref is not "unknown", it is at most not-orderable.
    all_refs = {sym.ref for sym in project.symbols
                if not sym.is_power}
    rows_findings, stats = bomchecks.run_bom_checks(
        project.bom_rows, bom_name=bom_name, expected=expected,
        all_refs=all_refs, sch_name=sch_name)
    return findings + rows_findings, stats


# ---------------------------------------------------------------------------
# KiCad file-format version handling (v0.3)
#
# Preflight is tested against KiCad 6/7/8 files. Format stamps:
#   schematic (version ...): 6=20211123, 7=20230121, 8=20231120,
#                            9=20250114, 10=20260306
#   pcb       (version ...): 6=20211014, 7=20221018, 8=20240108,
#                            9=20241229, 10=20260206
# (Anchors from real KiCad-written files; see docs/COVERAGE.md.)
# A file stamped newer than the tested range is still parsed — the
# core symbol/footprint model is stable — but KiCad 9 added embedded
# files, tables and pad stacks, and KiCad 10 added design variants
# (per-variant BOM/DNP overrides), jumpers and back-drilling, which
# this parser does NOT fully understand. The honest behaviour is to
# say so at CAUTION, never to let such a file pass in silence.
# ---------------------------------------------------------------------------

_SCH_TESTED_MAX = 20231120      # KiCad 8 schematic format
_PCB_TESTED_MAX = 20240108      # KiCad 8 board format
_SCH_KNOWN_MIN = 20211123       # KiCad 6 schematic format
_PCB_KNOWN_MIN = 20211014       # KiCad 6 board format


def _generator_major(generator):
    text = str(generator).strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    try:
        return int(text.split(".")[0])
    except (TypeError, ValueError):
        return None


def run_version_checks(project):
    findings = []
    checks = []
    if project.sch_path is not None and not project.sch_failed:
        checks.append(("schematic", project.sch_path.name,
                       project.sch_format_version, project.sch_generator,
                       project.sch_stamp_raw,
                       _SCH_TESTED_MAX, _SCH_KNOWN_MIN))
    for name, version, generator, stamp_raw in project.sub_sch_versions:
        checks.append(("schematic", name, version, generator, stamp_raw,
                       _SCH_TESTED_MAX, _SCH_KNOWN_MIN))
    if project.pcb_path is not None and not project.pcb_failed:
        checks.append(("PCB", project.pcb_path.name,
                       project.pcb_format_version, project.pcb_generator,
                       project.pcb_stamp_raw,
                       _PCB_TESTED_MAX, _PCB_KNOWN_MIN))
    for kind, name, version, generator, stamp_raw, tested_max, \
            known_min in checks:
        major = _generator_major(generator)
        newer = (version is not None and version > tested_max) \
            or (major is not None and major >= 9)
        older = version is not None and version < known_min
        unreadable = bool(stamp_raw)
        if not (newer or older or unreadable):
            continue
        stamp = []
        if version is not None:
            stamp.append(f"format version {version}")
        if generator:
            stamp.append(f"generator_version {generator}")
        if stamp_raw:
            stamp.append(f"unreadable stamp ({stamp_raw})")
        stamp_text = ", ".join(stamp) or "an unrecognised format stamp"
        if unreadable and not (newer or older):
            message = (
                f"This {kind} file carries a format stamp Preflight "
                f"cannot interpret ({stamp_text}). A stamp that cannot "
                "be read cannot be confirmed as a tested KiCad 6–8 "
                "format, so the file is treated as untested rather "
                "than silently passed.")
        elif newer:
            message = (
                f"This {kind} file was written by a newer KiCad than "
                f"Preflight is tested against ({stamp_text}; tested "
                "range: KiCad 6–8). The core agreement checks ran, "
                "but newer constructs — KiCad 9's embedded files, "
                "tables and pad stacks, KiCad 10's design variants "
                "(per-variant BOM/DNP), jumpers and back-drilling — "
                "are not fully parsed, so treat a PASS with care and "
                "sanity-check the findings by hand.")
        else:
            message = (
                f"This {kind} file carries {stamp_text}, older than "
                "the KiCad 6 format Preflight is tested against. It "
                "was parsed on a best-effort basis; constructs from "
                "that era may be misread, so treat a PASS with care.")
        findings.append(Finding(
            "KICAD_VERSION_UNTESTED", CAUTION, message, where=name,
            evidence=f"format_version={version if version is not None else '—'} "
                     f"generator_version={generator or '—'}"))
    return findings
