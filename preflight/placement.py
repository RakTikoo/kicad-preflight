"""Position / pick-and-place (CPL) CSV parsing — tool-agnostic.

Assembly houses place parts from a position file, not from the board
file: KiCad's own .pos CSV export (Ref / PosX / PosY / Rot / Side) or a
fab's CPL (JLCPCB: Designator / Mid X / Mid Y / Layer / Rotation). A
designator in the CPL that is not on the board, or a board part with
no CPL row, is a classic assembly-order failure (the fab's uploader
flags the mismatch and holds the order). This module parses such a
file into its designator list; the checks live in fabchecks.py.

Like bom.py, this module imports nothing KiCad-specific.
"""

import csv
import io
import re

# Same budgets philosophy as bom.py: a position file past these limits
# is reported as unreadable rather than parsed without bound.
MAX_CPL_ROWS = 20_000
CSV_FIELD_LIMIT = 1_000_000          # chars per field (deliberate cap)

csv.field_size_limit(CSV_FIELD_LIMIT)

_REF_ALIASES = {"ref", "refs", "reference", "references", "designator",
                "designators", "refdes"}
_X_ALIASES = {"posx", "midx", "x", "centerx", "positionx",
              "xcoordinate", "centrex"}
_Y_ALIASES = {"posy", "midy", "y", "centery", "positiony",
              "ycoordinate", "centrey"}
_QTY_ALIASES = {"quantity", "qty", "qnty", "count"}
_MPN_ALIASES = {"mpn", "manufacturerpartnumber", "manufacturerpartno",
                "mfrpartnumber", "mfrpart", "partnumber", "mpartno",
                "manufacturerpart", "lcsc", "lcscpart", "lcscpartno",
                "lcscpartnumber", "jlcpcbpart", "jlcpcbpartno",
                "jlcpcbpartnumber"}


def _norm_header(cell):
    return re.sub(r"[^a-z0-9]", "", (cell or "").lower())


def _delimiter(text):
    """Sniff the CSV delimiter from the first line: European Excel
    exports position files semicolon-delimited; everything else in
    this domain uses commas."""
    first = text.split("\n", 1)[0]
    return ";" if first.count(";") > first.count(",") else ","


def _first_row(text):
    try:
        return next(csv.reader(io.StringIO(text),
                               delimiter=_delimiter(text)))
    except (csv.Error, StopIteration):
        return None


def _header_kind(header_cells):
    """Classify a CSV header row: 'placement' or None.

    A position file has a designator column AND X and Y columns. An
    extra Quantity column does NOT disqualify it here — a CPL with a
    stray quantity column is still a position file (the BOM claim on
    such a file is decided by find_bom, which runs first and prefers
    files carrying BOM-only columns; see is_bom_like_text).
    """
    header = [_norm_header(c) for c in header_cells]
    has_ref = any(h in _REF_ALIASES for h in header)
    has_x = any(h in _X_ALIASES for h in header)
    has_y = any(h in _Y_ALIASES for h in header)
    if has_ref and has_x and has_y:
        return "placement"
    return None


def is_placement_text(text):
    """True when `text` starts with a position-file header row."""
    first = _first_row(text)
    return first is not None and _header_kind(first) == "placement"


def is_placement_like_text(text):
    """True when the header has the position SHAPE (designator + X +
    Y columns), even if the file as a whole failed to parse as one.
    Used by discovery to decide that a failed file was position-like
    and its failure must be reported, whatever its filename."""
    return is_placement_text(text)


def is_bom_like_text(text):
    """True when the header carries BOM-only columns (quantity, MPN
    or LCSC part number). Position files never carry part numbers,
    so a refs+X+Y file WITH such columns is a BOM that happens to
    include coordinates — find_bom must not skip it as a CPL."""
    first = _first_row(text)
    if first is None:
        return False
    header = [_norm_header(c) for c in first]
    return any(h in _QTY_ALIASES or h in _MPN_ALIASES for h in header)


def parse_placement(text):
    """Parse a position CSV into a list of designator strings.

    Raises ValueError when the header is not a position-file header or
    the file is over budget. Rows are consumed as a stream and the row
    budget is enforced DURING the read (a hostile 3M-row file is
    rejected at row 20,001, not after being materialised). Rows with
    an empty designator cell are skipped (a trailing blank line is
    not a part). Duplicate designators are preserved — the checks
    flag them.
    """
    reader = csv.reader(io.StringIO(text), delimiter=_delimiter(text))
    try:
        header_row = next(reader)
    except StopIteration:
        raise ValueError("position file is empty") from None
    except csv.Error as exc:
        raise ValueError(f"CSV parse error: {exc}") from exc
    if _header_kind(header_row) != "placement":
        header_label = ", ".join(header_row)
        if len(header_label) > 160:
            header_label = header_label[:160] + "…"
        raise ValueError(
            "no recognisable position-file header (need a designator "
            "column plus X/Y columns): " + header_label)
    header = [_norm_header(c) for c in header_row]
    ref_idx = next(i for i, h in enumerate(header) if h in _REF_ALIASES)
    refs = []
    row_count = 0
    try:
        for raw in reader:
            if not any((c or "").strip() for c in raw):
                continue
            row_count += 1
            if row_count > MAX_CPL_ROWS:
                raise ValueError(
                    f"position file has more than {MAX_CPL_ROWS} "
                    "rows (scan budget)")
            if ref_idx < len(raw):
                ref = (raw[ref_idx] or "").strip()
                if ref:
                    refs.append(ref)
    except csv.Error as exc:
        raise ValueError(f"CSV parse error: {exc}") from exc
    return refs
