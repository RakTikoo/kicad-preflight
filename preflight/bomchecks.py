"""BOM checks — tool-agnostic, deliberately KiCad-free.

These checks consume parsed BOM rows (bom.py) and, when available, a
parts list ({ref: object with .value/.footprint/.dnp/.in_bom}). With no
parts list they still run every row-internal check — that is the
standalone-BOM mode, usable on a CSV exported from any EDA tool
(KiCad-first, not KiCad-only; see EXTENSIONS_AND_GITHUB.md §1).
Nothing in this module imports the KiCad parser.
"""

from .finding import BLOCK, CAUTION, NOTE, Finding

PLACEHOLDER_MPNS = {"~", "tbd", "tba", "n/a", "na", "-", "?", "x", "xx",
                    "xxx", "none", "unknown", "dnp", "no part"}


def _norm(text):
    return " ".join((text or "").strip().lower().split())


def run_bom_checks(rows, bom_name="BOM", expected=None, all_refs=None,
                   sch_name="schematic"):
    """Check BOM rows.

    expected: {ref: part} for parts that belong in the BOM, or None
    (standalone mode — schematic-dependent checks are skipped).
    all_refs: set of every schematic ref (superset of expected), or None.
    """
    findings = []
    seen_refs = {}
    lcsc_only_rows = 0

    for row in rows:
        where = f"{bom_name} line {row.line_no}"
        refs_label = ", ".join(row.refs) or "no references"

        for ref in row.refs:
            if ref in seen_refs:
                findings.append(Finding(
                    "BOM_DUPLICATE_REFERENCE", CAUTION,
                    f"{ref} appears in the BOM twice (lines "
                    f"{seen_refs[ref]} and {row.line_no}). One of the rows "
                    "will double-order or shadow the other.",
                    where=where))
            else:
                seen_refs[ref] = row.line_no
            if expected is not None and ref not in all_refs:
                findings.append(Finding(
                    "BOM_UNKNOWN_REFERENCE", BLOCK,
                    f"The BOM lists {ref}, but there is no {ref} in the "
                    "schematic. You would order a part the design does "
                    "not use — or the schematic is missing a part.",
                    where=where, evidence=f"BOM line: {ref}"))
            elif expected is not None and ref not in expected:
                findings.append(Finding(
                    "BOM_PART_NOT_ORDERABLE", CAUTION,
                    f"The BOM lists {ref}, but in the schematic {ref} is "
                    "marked do-not-populate / not-in-BOM. Ordering it "
                    "buys a part the build does not fit — or the "
                    "schematic flag is wrong.",
                    where=where, evidence=f"BOM line: {ref}"))

        if not row.refs:
            if row.qty_raw or row.mpn or row.value or row.lcsc:
                findings.append(Finding(
                    "BOM_ROW_WITHOUT_REFERENCES", CAUTION,
                    f"BOM line {row.line_no} has quantity/part data "
                    f"(qty '{row.qty_raw or '—'}', value "
                    f"'{row.value or '—'}') but lists no reference "
                    "designators. It cannot be matched to the design "
                    "and will be silently ignored by assemblers.",
                    where=where))
            continue

        if row.qty is None or row.qty != len(row.refs):
            findings.append(Finding(
                "BOM_QUANTITY_MISMATCH", BLOCK,
                f"BOM line {row.line_no} says quantity "
                f"'{row.qty_raw or '(blank)'}' but lists "
                f"{len(row.refs)} reference(s) "
                f"({', '.join(row.refs)}). The order quantity "
                "and the design disagree.",
                where=where,
                evidence=f"qty={row.qty_raw!r} refs={len(row.refs)}"))

        mpn_norm = _norm(row.mpn)
        if mpn_norm in PLACEHOLDER_MPNS and row.mpn.strip():
            findings.append(Finding(
                "BOM_PLACEHOLDER_MPN", CAUTION,
                f"BOM line {row.line_no} ({refs_label}) has a placeholder "
                f"manufacturer part number ('{row.mpn}'). Replace it "
                "with the real MPN before ordering — a placeholder "
                "orders whatever the distributor guesses.",
                where=where))
        elif not row.mpn and not row.lcsc:
            findings.append(Finding(
                "BOM_MISSING_MPN", CAUTION,
                f"BOM line {row.line_no} ({refs_label}) has no "
                "manufacturer part number and no LCSC/JLCPCB part "
                "number. Without one, the distributor — and "
                "Preflight's part-risk checks — cannot tell exactly "
                "which part you mean.",
                where=where))
        elif not row.mpn and row.lcsc:
            lcsc_only_rows += 1

        if not row.footprint:
            findings.append(Finding(
                "BOM_MISSING_FOOTPRINT", CAUTION,
                f"BOM line {row.line_no} ({refs_label}) has no "
                "footprint/package. The assembler cannot confirm the "
                "part physically matches the board.",
                where=where))

        # Cross-checks against the schematic part (first ref's part is
        # the row's identity in a grouped BOM).
        if expected is not None and row.refs:
            part = next((expected[r] for r in row.refs if r in expected),
                        None)
            if part is not None:
                if row.footprint and part.footprint \
                        and _norm(row.footprint) != _norm(part.footprint):
                    findings.append(Finding(
                        "BOM_FOOTPRINT_MISMATCH", CAUTION,
                        f"BOM line {row.line_no} ({refs_label}) gives "
                        f"footprint '{row.footprint}' but the schematic "
                        f"says '{part.footprint}'. One of them is the "
                        "package that will actually be soldered — make "
                        "them agree.",
                        where=where,
                        evidence=f"bom={row.footprint}  "
                                 f"schematic={part.footprint}"))
                if row.value and part.value:
                    bv, sv = _norm(row.value), _norm(part.value)
                    if bv != sv and bv not in sv and sv not in bv:
                        findings.append(Finding(
                            "BOM_VALUE_DIFFERS", NOTE,
                            f"BOM line {row.line_no} ({refs_label}) "
                            f"describes the part as '{row.value}' but "
                            f"the schematic value is '{part.value}'. "
                            "Worth a look — BOM tools often normalise "
                            "values, so this may be harmless.",
                            where=where,
                            evidence=f"bom={row.value!r}  "
                                     f"schematic={part.value!r}"))

    if expected is not None:
        for ref in sorted(expected):
            if ref not in seen_refs:
                findings.append(Finding(
                    "SCHEMATIC_PART_MISSING_FROM_BOM", CAUTION,
                    f"{ref} ({expected[ref].value or 'part'}) is in the "
                    "schematic but missing from the BOM. It will not be "
                    "ordered or assembled.",
                    where=f"{sch_name} · {ref}"))
    return findings, {"lcsc_only_rows": lcsc_only_rows,
                      "refs_seen": len(seen_refs)}
