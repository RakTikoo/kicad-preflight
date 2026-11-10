"""BOM CSV parsing — deliberately independent of the KiCad parser.

A BOM is a BOM in any EDA tool: KiCad, JLCPCB, Altium, Eagle/Fusion and
EasyEDA exports differ in header spellings, not in the problems they
carry. This module (and bomchecks.py) imports nothing KiCad-specific,
so the BOM layer can be reused — and run standalone on a CSV — without
the schematic/PCB parser. See EXTENSIONS_AND_GITHUB.md §1 (scope:
KiCad-first, not KiCad-only).
"""

import csv
import io
import re
from dataclasses import dataclass, field

# Budgets (stated in the README): a BOM past these limits is reported
# as unreadable rather than parsed without bound.
MAX_BOM_ROWS = 20_000
MAX_REFS_PER_ROW = 5_000
CSV_FIELD_LIMIT = 1_000_000          # chars per field (deliberate cap)

csv.field_size_limit(CSV_FIELD_LIMIT)

_HEADER_ALIASES = {
    "refs": {"reference", "references", "ref", "refs", "designator",
             "designators", "refdes"},
    "qty": {"quantity", "qty", "qnty", "count"},
    "value": {"value", "val", "component", "comment"},
    "footprint": {"footprint", "package"},
    "mpn": {"mpn", "manufacturerpartnumber", "manufacturerpartno",
            "mfrpartnumber", "mfrpart", "partnumber", "mpartno",
            "manufacturerpart"},
    "manufacturer": {"manufacturer", "mfr", "brand"},
    "lcsc": {"lcsc", "lcscpart", "lcscpartno", "lcscpartnumber",
             "lcscpartnumber1", "jlcpcbpart", "jlcpcbpartno",
             "jlcpcbpartnumber"},
    "description": {"description", "desc"},
}


@dataclass
class BomRow:
    line_no: int
    refs: list = field(default_factory=list)
    qty_raw: str = ""
    qty: int = None              # None when the quantity is not a whole number
    value: str = ""
    footprint: str = ""
    mpn: str = ""
    manufacturer: str = ""
    lcsc: str = ""


_REF_RANGE = re.compile(r"^([A-Za-z]+)(\d+)\s*-\s*(?:([A-Za-z]+)\s*)?(\d+)$")
_SPACED_RANGE = re.compile(r"([A-Za-z]+\d+)\s+-\s+([A-Za-z]+\d+)")


def _norm_header(cell):
    return re.sub(r"[^a-z0-9]", "", (cell or "").lower())


def parse_designators(cell):
    """Split a reference list ("R1, R2, R5") into designators.

    Both documented range forms are expanded: "R1-R3" and "R1-3"
    (and the spaced "R1 - R3"). Anything that does not look like a
    designator is kept verbatim so the checks can flag it. Expansion
    is capped (200 per range, MAX_REFS_PER_ROW per cell).
    """
    text = _SPACED_RANGE.sub(r"\1-\2", (cell or "").strip())
    refs = []
    for token in re.split(r"[,;\s]+", text):
        if not token:
            continue
        m = _REF_RANGE.match(token)
        if m:
            prefix, start, prefix2, end = (m.group(1), int(m.group(2)),
                                           m.group(3), int(m.group(4)))
            if (prefix2 is None or prefix2 == prefix) \
                    and end >= start and end - start <= 200:
                refs.extend(f"{prefix}{n}" for n in range(start, end + 1))
                continue
        refs.append(token)
        if len(refs) >= MAX_REFS_PER_ROW:
            break
    return refs[:MAX_REFS_PER_ROW]


def _parse_qty(qty_raw):
    """Whole-number quantity or None. No silent truncation, no overflow:
    '2.9' -> None, '1e999'/'inf' -> None, '3.0' -> 3, ' 3 ' handled by
    the caller's strip."""
    if not qty_raw:
        return None
    try:
        return int(qty_raw)
    except ValueError:
        pass
    try:
        value = float(qty_raw)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if value.is_integer() and abs(value) < 1e9:
        return int(value)
    return None


def parse_bom(text):
    """Parse BOM CSV text into BomRow objects. Raises ValueError on bad shape."""
    try:
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if any((c or "").strip() for c in r)]
    except csv.Error as exc:
        raise ValueError(f"CSV parse error: {exc}") from exc
    if not rows:
        raise ValueError("BOM file is empty")
    if len(rows) - 1 > MAX_BOM_ROWS:
        raise ValueError(f"BOM has more than {MAX_BOM_ROWS} rows "
                         "(scan budget) — split it or raise the budget "
                         "in preflight/bom.py")
    header = [_norm_header(c) for c in rows[0]]
    colmap = {}
    for idx, name in enumerate(header):
        for field_name, aliases in _HEADER_ALIASES.items():
            if name in aliases and field_name not in colmap:
                colmap[field_name] = idx
    if "refs" not in colmap:
        raise ValueError(
            "no recognisable reference/designator column in BOM header: "
            + ", ".join(rows[0]))
    bom_rows = []
    for offset, raw in enumerate(rows[1:]):
        def cell(field_name):
            idx = colmap.get(field_name)
            if idx is None or idx >= len(raw):
                return ""
            return (raw[idx] or "").strip()

        qty_raw = cell("qty")
        bom_rows.append(BomRow(
            line_no=offset + 2,
            refs=parse_designators(cell("refs")),
            qty_raw=qty_raw,
            qty=_parse_qty(qty_raw),
            value=cell("value"),
            footprint=cell("footprint"),
            mpn=cell("mpn"),
            manufacturer=cell("manufacturer"),
            lcsc=cell("lcsc"),
        ))
    return bom_rows
