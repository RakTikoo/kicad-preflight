"""Load a KiCad project from disk: schematic(s), PCB, and BOM.

Everything is parsed from KiCad's own text formats — .kicad_sch and
.kicad_pcb are S-expressions, the BOM is CSV (see bom.py), .kicad_pro
is JSON (only the project name is read from it). No KiCad installation
is required for this layer; the official DRC/ERC checks live in
kicadcli.py and are optional.

Formats: KiCad 6, 7 and 8 project files are the tested range. On the
PCB side, KiCad 6 stores footprint references as (fp_text reference
"R1" ...) while KiCad 7/8 use (property "Reference" "R1" ...) — both
are read. The file-format version of each file is recorded so the
checks can warn honestly about files written by newer KiCad versions
(9/10) whose new constructs (embedded files, tables, pad stacks,
variants) this parser does not fully understand — never silently.
Hierarchical schematics are followed: (sheet ...) blocks in the root
sheet name sub-sheet files, which are loaded and merged. Legacy
KiCad <=5 .sch files are reported, not silently misread.

PCB geometry is captured at two levels. Property level: segment
widths, via sizes/drills, pad sizes/drills, zone fill presence,
board-outline presence — the values fab capability checks need.
Object level (v0.4, for the geometry-lite distance checks in
geometry.py): pad shapes with absolute position/rotation/layers/net,
track segments and arcs with width/layer/net, vias, and the
Edge.Cuts outline elements. Net ids/names are read as identity
labels only — connectivity is NOT traced and pins are not parsed
(see checks.py). Zone filled polygons ARE captured (v0.6, WS3) as
plain point rings in ZoneGeo — one ring per (filled_polygon ...)
node, exactly as stored in the file (the PCB file format defines
a filled polygon as a single ring with no hole sub-structure;
clearance cut-outs appear as separate rings / ring indentations).
geometry.py measures those rings in its own budgeted zone pass;
zones with no stored fill contribute no ring and abstain there,
counted in Coverage.

Safety: every file is stat-checked before reading — it must be a
regular file (a FIFO or device would block forever) and within the
size budget — and decoded as strict UTF-8 (undecodable bytes become a
parse error, never silently rewritten characters).
"""

import fnmatch
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .bom import BomRow, parse_bom  # noqa: F401  (re-exported)
from .placement import (is_bom_like_text, is_placement_like_text,
                        is_placement_text, parse_placement)
from .sexpr import SexprError

MAX_FILE_BYTES = 64 * 1024 * 1024    # 64 MB per project file (budget)

_DESIGNATOR = re.compile(r"^[A-Za-z]{1,4}\d+$")

_MPN_PROP_KEYS = {"mpn", "manufacturerpartnumber", "manufacturerpartno",
                  "mfrpartnumber", "mfrpart", "partnumber", "mpartno"}


def is_designator(ref):
    """True for ordinary reference designators (R1, C12, U3, K00, TP1).
    Placeholder or tool-generated refs (REF**, G***, kibuzzard-XXXX)
    are not designators."""
    return bool(ref) and bool(_DESIGNATOR.match(ref))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class LibPin:
    """One pin of a library symbol definition (v0.5, netlist capture)."""

    type: str = ""               # electrical type: input/output/passive/...
    name: str = ""
    number: str = ""
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0
    hidden: bool = False
    # v0.5 fix wave (audit F8/F9): a pin that cannot be resolved --
    # "" (good), "number" (no (number ...) -- counted unresolved,
    # never a resolved pin numbered "") or "coords" (its (at ...)
    # was unreadable/non-finite -- counted, never placed at a guess).
    bad: str = ""


@dataclass
class LibSymbolDef:
    """A (lib_symbols ...) definition: pins grouped by unit.

    pins_by_unit[0] holds the pins of the "Name_0_1" sub-symbol
    (they apply to every unit); pins_by_unit[k] the pins of
    "Name_k_1" (style 1 only — alternate-body-style pins are not
    read; a placed symbol does not record which style it uses)."""

    lib_id: str = ""
    is_power: bool = False       # the definition carries a (power) flag
    pins_by_unit: dict = field(default_factory=dict)

    def pins_for_unit(self, unit):
        pins = list(self.pins_by_unit.get(0, ()))
        # Numberless (bad) pins have no number to dedupe on: each
        # one is its own unresolved pin.
        seen = {p.number for p in pins if p.number}
        for pin in self.pins_by_unit.get(unit, ()):
            if pin.number and pin.number in seen:
                continue
            pins.append(pin)
            if pin.number:
                seen.add(pin.number)
        return pins


@dataclass
class LabelRec:
    name: str = ""
    kind: str = ""               # label / global_label / hierarchical_label
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0


@dataclass
class SheetPinRec:
    name: str = ""
    type: str = ""               # electrical type atom after the name
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0


@dataclass
class SheetBlockRec:
    """One (sheet ...) block in a parent sheet (v0.5)."""

    name: str = ""
    file: str = ""
    pins: list = field(default_factory=list)   # SheetPinRec


@dataclass
class SheetData:
    """Everything netlist extraction needs from one sheet file (v0.5)."""

    file: str = ""                             # sheet file name
    lib_symbols: dict = field(default_factory=dict)  # lib_id -> LibSymbolDef
    wires: list = field(default_factory=list)  # point chains [(x, y), ...]
    buses: list = field(default_factory=list)  # point chains, as wires
    # v0.6 WS3: (bus_alias "NAME" (members ...)) declarations, name
    # -> member net names. Captured for Coverage counting ONLY:
    # bus membership is not expanded into connectivity (see
    # netlist.py module docstring / v06-ws3 INTEGRATION.md).
    bus_aliases: dict = field(default_factory=dict)
    bus_entries: list = field(default_factory=list)  # ((x, y), (x, y))
    labels: list = field(default_factory=list)       # LabelRec
    junctions: list = field(default_factory=list)    # (x, y)
    no_connect_pts: list = field(default_factory=list)  # (x, y)
    sheets: list = field(default_factory=list)       # SheetBlockRec
    # v0.5 fix wave (audit F7/F8): honesty counters for malformed
    # input that used to be resolved silently (first/last-wins) or
    # dropped silently -- surfaced in the netlist Coverage stats.
    lib_defs_duplicate: int = 0   # lib_id defined 2+ times
    pins_duplicate: int = 0       # pin numbers dropped as duplicates
    coords_unreadable: int = 0    # total unreadable coordinate objects
    coords_unreadable_pins: int = 0
    coords_unreadable_labels: int = 0
    coords_unreadable_wire_points: int = 0
    coords_unreadable_other: int = 0


@dataclass
class Symbol:
    """One placed schematic symbol (a component instance)."""

    ref: str
    value: str = ""
    footprint: str = ""          # "" when the property is empty, None when absent
    lib_id: str = ""
    unit: int = 1
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False
    props: dict = field(default_factory=dict)
    sheet: str = ""              # file the symbol came from
    # --- v0.5 netlist capture: placement transform + placed pin list --
    x: float = None
    y: float = None
    rot: float = 0.0
    mirror: str = None           # "x" / "y" / None
    pin_numbers: tuple = ()      # (pin "N" ...) children of the instance

    @property
    def is_power(self):
        # Power symbols (#PWR01, #FLG01, ...) are net flags, not parts.
        return self.ref.startswith("#")

    @property
    def mpn(self):
        for key, val in self.props.items():
            if re.sub(r"[^a-z0-9]", "", key.lower()) in _MPN_PROP_KEYS:
                return val
        return None               # no MPN property stored in the schematic

    @property
    def datasheet(self):
        return self.props.get("Datasheet")


@dataclass
class FootprintInst:
    """One footprint instance placed on the PCB."""

    ref: str
    name: str = ""               # full "Library:Footprint" string
    value: str = ""
    board_only: bool = False     # (attr ... board_only ...)
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False  # (attr ... exclude_from_pos_files ...)

    @property
    def is_part(self):
        """A real component position vs board furniture (mounting holes
        marked board-only, silkscreen logos/labels with generated or
        placeholder refs). Membership and duplicate checks only ever
        consider parts."""
        return is_designator(self.ref) and not self.board_only


@dataclass
class PadGeo:
    """One pad as a copper object for the geometry-lite checks.

    Position/axes are absolute (footprint position, mirroring for
    bottom-side footprints, and KiCad's rotation convention applied —
    the convention was verified empirically against track endpoints
    in real boards, see geometry.py). `layers` holds copper layer
    names only. net_id None means the file states no net for the pad.
    """

    ref: str = ""
    name: str = ""               # pad number/name ("1", "A2", ...)
    shape: str = ""              # circle/rect/oval/roundrect/...
    cx: float = 0.0
    cy: float = 0.0
    ux: float = 1.0              # local X axis (unit) in board coords
    uy: float = 0.0
    vx: float = 0.0              # local Y axis (unit) in board coords
    vy: float = 1.0
    sx: float = 0.0              # pad size, mm
    sy: float = 0.0
    rratio: float = 0.0          # roundrect corner radius ratio
    layers: tuple = ()
    net_id: int = None
    net_name: str = ""


@dataclass
class TrackGeo:
    """One straight copper segment (arcs are subdivided into these
    by geometry.py; raw arcs are kept in PcbGeometry.arcs)."""

    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    width: float = 0.0
    layer: str = ""
    net_id: int = None
    net_name: str = ""


@dataclass
class ArcGeo:
    """One copper arc by its three defining points (start/mid/end)."""

    sx: float = 0.0
    sy: float = 0.0
    mx: float = 0.0
    my: float = 0.0
    ex: float = 0.0
    ey: float = 0.0
    width: float = 0.0
    layer: str = ""
    net_id: int = None
    net_name: str = ""


@dataclass
class ViaGeo:
    cx: float = 0.0
    cy: float = 0.0
    size: float = 0.0            # outer diameter, mm
    layers: tuple = ()
    net_id: int = None
    net_name: str = ""


@dataclass
class EdgeElem:
    """One root-level Edge.Cuts drawing item.

    kind/pts: line (p1, p2); arc (start, mid, end) — mid None for the
    legacy (angle ...) form, with `angle` set instead; circle
    (centre, radius); rect (corner1, corner2); poly (point tuple,
    implicitly closed); curve (4 Bézier control points)."""

    kind: str = ""
    pts: tuple = ()
    angle: float = None          # legacy KiCad arc form only


@dataclass
class ZoneGeo:
    """One root-level copper zone and its stored filled polygons
    (v0.6, WS3).

    `polygons` holds one (layer, points) entry per readable
    (filled_polygon ...) node: the ring exactly as stored, layer
    canonicalised to the board's copper-layer spelling. A zone
    whose fill is absent, stale-empty or unreadable contributes
    an empty list — geometry.py abstains for that zone alone and
    counts it. Keepout / rule-area zones are not ZoneGeo at all
    (they are not pours; see _zone_fill_state)."""

    net_id: int = None
    net_name: str = ""
    name: str = ""               # the zone's (name ...) property, if any
    polygons: list = field(default_factory=list)


@dataclass
class PcbGeometry:
    """Property-level PCB measurements for the fab-capability checks.

    Only values printed in the file are recorded — no geometric
    computation (no clearances, no distances). Lists hold every
    measured value; rings/slots keep the footprint reference so a
    finding can name an example part."""

    segment_widths: list = field(default_factory=list)  # copper segments, mm
    via_sizes: list = field(default_factory=list)       # mm
    via_drills: list = field(default_factory=list)      # mm
    pad_drills: list = field(default_factory=list)      # plated round drills, mm
    pad_rings: list = field(default_factory=list)       # (ring mm, ref) plated
    slot_widths: list = field(default_factory=list)     # (narrow dim mm, ref) plated ovals
    zones_total: int = 0
    zones_unfilled: int = 0
    edge_items: int = 0          # root-level drawing items on Edge.Cuts
    unparsed_dims: int = 0       # dimension values present but unreadable
    # --- object-level geometry (v0.4, geometry-lite) -------------------
    pads: list = field(default_factory=list)        # PadGeo, copper pads
    tracks: list = field(default_factory=list)      # TrackGeo
    arcs: list = field(default_factory=list)        # ArcGeo
    vias: list = field(default_factory=list)        # ViaGeo
    edge_elems: list = field(default_factory=list)  # EdgeElem
    gr_copper: list = field(default_factory=list)
    # root-level graphic items on copper layers (TrackGeo-shaped,
    # net always None): they are copper for edge-distance purposes
    pads_non_copper: int = 0     # np_thru_hole pads (no copper annulus)
    # v0.5 fix wave (audit F3): np_thru_hole pads are not copper
    # geometry, but they DO carry (ref, pad name, net) membership in
    # the file -- recorded here (PadGeo-shaped) for netlist parity
    # only; the geometry layer never sees this list.
    pads_membership_extra: list = field(default_factory=list)
    pads_shape_skipped: int = 0  # custom/trapezoid pads: not modelled
    # v0.6 WS3: root zones with their stored filled polygons, and
    # the count of filled_polygon rings dropped as unreadable.
    zones_geo: list = field(default_factory=list)      # ZoneGeo
    zones_fill_unreadable: int = 0
    # v0.5: per-ref count of pads the modelled membership cannot see
    # (shape-skipped, unplaced, or non-positive size) — schematic<->
    # PCB parity abstains per footprint ref on this, not globally.
    pad_issues_by_ref: dict = field(default_factory=dict)
    # v0.6 WS3: per-ref count of pads that contribute NEITHER a
    # modelled pad NOR a membership-only record (pads_membership_
    # extra). Parity abstains per ref on THIS count: a trapezoid
    # pad, or a custom pad whose primitive set is modelled (see
    # _pad_geo), states its (ref, pad name, net) in the file, so
    # its membership is measurable even though its clearance
    # geometry is not modelled.
    pad_membership_missing_by_ref: dict = field(default_factory=dict)
    pad_total_by_ref: dict = field(default_factory=dict)  # all pad nodes
    pads_unplaced: int = 0       # pads whose position could not be read
    items_unplaced: int = 0      # tracks/vias/edge items unreadable coords
    copper_layers: list = field(default_factory=list)


@dataclass
class Project:
    name: str = ""
    folder: Path = None
    pro_path: Path = None
    sch_path: Path = None
    sch_paths_loaded: list = field(default_factory=list)   # incl. sub-sheets
    missing_sheets: list = field(default_factory=list)     # Sheetfile refs not on disk
    unloaded_sch: list = field(default_factory=list)       # .kicad_sch files not loaded
    unloaded_pcb: list = field(default_factory=list)       # .kicad_pcb files not loaded
    legacy_sch: list = field(default_factory=list)         # legacy .sch files present
    pcb_path: Path = None
    bom_path: Path = None
    bom_auto_note: str = ""      # how the BOM was chosen, for Coverage
    symbols: list = field(default_factory=list)
    footprints: list = field(default_factory=list)
    bom_rows: list = field(default_factory=list)
    bom_error: str = ""          # set when a BOM file exists but won't parse
    sch_format_version: int = None    # (version NNNNNNNN) of the root sheet
    sch_generator: str = ""           # (generator_version "8.0")
    sch_stamp_raw: str = ""           # stamp text present but unreadable
    sub_sch_versions: list = field(default_factory=list)
    # (name, format_version, generator, stamp_raw) for sub-sheets
    pcb_format_version: int = None
    pcb_generator: str = ""
    pcb_stamp_raw: str = ""
    geometry: PcbGeometry = field(default_factory=PcbGeometry)
    nets: dict = field(default_factory=dict)   # net id -> name, from the PCB
    board_rules: dict = field(default_factory=dict)
    # design rules stated by the project itself (.kicad_pro):
    # {"min_clearance": float|None, "min_copper_edge_clearance":
    #  float|None, "netclasses": {name: clearance},
    #  "netclass_patterns": [(pattern, class name)]} — values are
    #  positive floats only; 0.0 in KiCad means "not set".
    cpl_path: Path = None             # position/CPL CSV, when one was found
    cpl_refs: list = field(default_factory=list)
    cpl_error: str = ""          # position-named file exists but won't parse
    cpl_auto_note: str = ""
    cpl_files: list = field(default_factory=list)  # all CPL files merged
    cpl_broken: list = field(default_factory=list)
    # (name, error) position-like files that failed while another parsed
    no_connects: int = 0
    sheet_data: list = field(default_factory=list)  # SheetData, per sheet (v0.5)
    sch_symbols_no_ref: int = 0  # placed symbols skipped: no Reference
    pcb_footprints_no_ref: int = 0
    parse_errors: list = field(default_factory=list)  # (path, error message)

    @property
    def sch_failed(self):
        return any(p == self.sch_path for p, _ in self.parse_errors)

    @property
    def pcb_failed(self):
        return any(p == self.pcb_path for p, _ in self.parse_errors)


# ---------------------------------------------------------------------------
# Safe reading
# ---------------------------------------------------------------------------


def read_project_file(path):
    """Read a project file as strict UTF-8 text, with budgets.

    Raises SexprError (recorded by callers as a parse error) for
    non-regular files, oversize files, and undecodable bytes — never
    blocks on a FIFO, never silently rewrites characters.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        raise SexprError(f"cannot stat file ({exc})")
    if not stat.S_ISREG(st.st_mode):
        raise SexprError("not a regular file (FIFO/device/directory?) — "
                         "refusing to read it")
    if st.st_size > MAX_FILE_BYTES:
        raise SexprError(f"file is {st.st_size} bytes, over the "
                         f"{MAX_FILE_BYTES}-byte scan budget")
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SexprError(f"cannot read file ({exc})")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SexprError(f"not valid UTF-8 ({exc}) — refusing to guess "
                         "at corrupted characters")


# ---------------------------------------------------------------------------
# Schematic / PCB parsing
# ---------------------------------------------------------------------------


def _properties(node):
    props = {}
    for prop in sexpr.children(node, "property"):
        if len(prop) >= 3 and isinstance(prop[1], str):
            props[prop[1]] = prop[2] if isinstance(prop[2], str) else ""
    return props


def _flag(node, name, default):
    flag = sexpr.child(node, name)
    if flag is not None and len(flag) >= 2 and isinstance(flag[1], str):
        return flag[1].strip().lower() == "yes"
    return default


def _safe_int(value, max_digits=9):
    """int() from untrusted text, or None (audit F6).

    Python >=3.11 refuses int() on strings over 4,300 digits with a
    ValueError that used to escape some call sites and kill the
    whole run with no report.  Any digit string longer than
    max_digits is treated as unparseable here -- no legitimate
    KiCad version, unit, style or net id is anywhere near 9 digits.
    """
    text = str(value).strip()
    digits = text[1:] if text[:1] in ("+", "-") else text
    if digits.isdigit() and len(digits) > max_digits:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _file_versions(root):
    """(format_version, generator_version, stamp_raw) from a file root.

    (version 20240108) is KiCad's format stamp; (generator_version
    "8.0") names the writing application version. Either may be
    absent (KiCad 6 files often lack generator_version) — absence is
    recorded as None/"", never guessed. A stamp that is PRESENT but
    cannot be interpreted (a malformed version string, a generator
    stored as a list node) is returned in stamp_raw instead of being
    silently dropped: unreadable format evidence must warn, the way
    an unparseable file BLOCKs (red-team finding, v0.3 fix wave)."""
    version = None
    raw_bits = []
    ver_node = sexpr.child(root, "version")
    if ver_node is not None and len(ver_node) >= 2:
        version = _safe_int(ver_node[1])
        if version is None:
            raw_bits.append(f"version stamp {str(ver_node[1])[:40]!r}")
    generator = ""
    gen_node = sexpr.child(root, "generator_version")
    if gen_node is not None and len(gen_node) >= 2:
        if isinstance(gen_node[1], str):
            generator = gen_node[1].strip()
        elif isinstance(gen_node[1], list):
            # (generator_version (9 0)) — a list node; read its atoms
            # as a dotted version rather than discarding the stamp.
            atoms = [str(a) for a in gen_node[1] if not isinstance(a, list)]
            if atoms:
                generator = ".".join(atoms)
            else:
                raw_bits.append("generator_version stamp (empty list)")
        else:
            generator = str(gen_node[1]).strip()
    return version, generator, "; ".join(raw_bits)


def _num(value):
    """Float from an S-expression atom, or None when not numeric.

    Non-finite values are rejected: KiCad never writes 'nan'/'inf'
    as a dimension, and a NaN would poison min() comparisons in the
    fab checks (order-dependent, silently suppressing real minima).
    A trailing unit suffix ('0.05mm', written by some third-party
    generators) is accepted. A list value yields its first numeric
    scalar atom — (width (0.05)) is a dimension, not an absence.
    Callers count None results on real dimension nodes as unreadable
    (geometry.unparsed_dims) so nothing vanishes silently.
    """
    if isinstance(value, list):
        for item in value:
            if isinstance(item, list):
                continue
            result = _num(item)
            if result is not None:
                return result
        return None
    text = str(value).strip()
    if text.lower().endswith("mm"):
        text = text[:-2].strip()
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _contains_hide(node):
    """True when a pin is marked hidden: a bare `hide` atom or a
    (hide ...) list DIRECTLY under the pin node. (A `hide` inside
    the pin's (name ...) / (number ...) effects hides the pin's
    TEXT, not the pin, and must not count.)"""
    if not isinstance(node, list):
        return False
    for item in node[1:]:
        if isinstance(item, str) and item == "hide":
            return True
        if isinstance(item, list) and sexpr.head(item) == "hide":
            return True
    return False


_UNIT_STYLE = re.compile(r"_(\d+)_(\d+)$")


def _parse_lib_symbols(root):
    """(defs, counters) from a root's (lib_symbols ...) node.

    counters (audit F7/F8): lib_defs_duplicate, pins_duplicate,
    coords_unreadable_pins -- malformed input that is resolved
    deterministically (first def / first pin wins, as before) but
    no longer silently.
    """
    defs = {}
    counters = {"lib_defs_duplicate": 0, "pins_duplicate": 0,
                "coords_unreadable_pins": 0}
    libs = sexpr.child(root, "lib_symbols")
    if libs is None:
        return defs, counters
    for sym in sexpr.children(libs, "symbol"):
        if len(sym) < 2 or not isinstance(sym[1], str):
            continue
        lib_id = sym[1]
        if lib_id in defs:
            counters["lib_defs_duplicate"] += 1
        libdef = LibSymbolDef(
            lib_id=lib_id,
            is_power=sexpr.child(sym, "power") is not None)
        by_unit_style = {}       # (unit, style) -> [LibPin]
        for sub in sexpr.children(sym, "symbol"):
            if len(sub) < 2 or not isinstance(sub[1], str):
                continue
            match = _UNIT_STYLE.search(sub[1])
            if not match:
                continue
            # Audit F6: the unit/style digit strings are untrusted;
            # a >9-digit one is unparseable, never an int() crash.
            unit_no = _safe_int(match.group(1))
            style = _safe_int(match.group(2))
            if unit_no is None or style is None:
                continue
            pins = []
            for pin in sexpr.children(sub, "pin"):
                if len(pin) < 2 or not isinstance(pin[1], str):
                    continue
                name_node = sexpr.child(pin, "name")
                num_node = sexpr.child(pin, "number")
                number = num_node[1] if num_node is not None \
                    and len(num_node) >= 2 \
                    and isinstance(num_node[1], str) else ""
                name = name_node[1] if name_node is not None \
                    and len(name_node) >= 2 \
                    and isinstance(name_node[1], str) else ""
                at = _at_of(pin)
                if at is None:
                    # Audit F8: counted, and kept as a bad pin so a
                    # placed instance counts it unresolved instead
                    # of the pin vanishing from the file's record.
                    counters["coords_unreadable_pins"] += 1
                    pins.append(LibPin(
                        type=pin[1], name=name, number=number,
                        x=0.0, y=0.0, rot=0.0,
                        hidden=_contains_hide(pin), bad="coords"))
                    continue
                pins.append(LibPin(
                    type=pin[1], name=name, number=number,
                    x=at[0], y=at[1], rot=at[2],
                    hidden=_contains_hide(pin),
                    # Audit F9: no (number ...) = unresolved, not a
                    # resolved pin numbered "".
                    bad="" if number else "number"))
            if pins:
                by_unit_style.setdefault((unit_no, style),
                                         []).extend(pins)
        # Style 0 is the body-style-common part and style 1 the
        # normal body: pins from both apply (corpus boards keep
        # whole connectors' pins in _0_0). Style 2 is the alternate
        # (De Morgan) body — its pins duplicate style 1's numbers,
        # so it is only a fallback for a unit with no 0/1 pins.
        for unit_no in sorted({u for u, _s in by_unit_style}):
            merged, seen = [], set()
            for style in (0, 1):
                for pin in by_unit_style.get((unit_no, style), ()):
                    if pin.number and pin.number in seen:
                        counters["pins_duplicate"] += 1
                        continue
                    merged.append(pin)
                    if pin.number:
                        seen.add(pin.number)
            if not merged:
                merged = list(by_unit_style.get((unit_no, 2), ()))
            if merged:
                libdef.pins_by_unit[unit_no] = merged
        defs[lib_id] = libdef
    return defs, counters


def _pts_of(node):
    """(points, unreadable) from a (pts (xy ..) ..) child.

    Points whose coordinates are unreadable/non-finite are dropped
    (as before) but now counted (audit F8), never silently lost."""
    pts_node = sexpr.child(node, "pts")
    points = []
    unreadable = 0
    if pts_node is None:
        return points, unreadable
    for pt in pts_node[1:]:
        if isinstance(pt, list) and sexpr.head(pt) == "xy" \
                and len(pt) >= 3:
            x, y = _num(pt[1]), _num(pt[2])
            if x is not None and y is not None:
                points.append((x, y))
            else:
                unreadable += 1
    return points, unreadable


def parse_schematic(text, source_name=""):
    """Return (symbols, no_connects, title, sheet_files, skipped_no_ref,
    format_version, generator_version, stamp_raw, sheet_data) from
    .kicad_sch text. Every (kicad_sch ...) root in the file is read;
    placed symbols are the root's direct (symbol ...) children
    (definitions inside (lib_symbols ...) share the shape and are
    correctly ignored). sheet_data (v0.5) carries the connectivity
    constructs — lib pin definitions, wires, buses, labels,
    junctions, no-connect points and sheet blocks — for netlist.py."""
    forms = sexpr.parse(text)
    roots = [f for f in forms if sexpr.head(f) == "kicad_sch"]
    if not roots:
        raise SexprError("no (kicad_sch ...) root form — legacy KiCad <=5 "
                         "schematic format is not supported")
    symbols = []
    no_connects = 0
    title = ""
    sheet_files = []
    skipped_no_ref = 0
    format_version, generator, stamp_raw = _file_versions(roots[0])
    sheet_data = SheetData(file=source_name)

    def _coords(kind, n=1):
        sheet_data.coords_unreadable += n
        attr = f"coords_unreadable_{kind}"
        setattr(sheet_data, attr, getattr(sheet_data, attr) + n)

    for root in roots:
        _defs, _counters = _parse_lib_symbols(root)
        sheet_data.lib_defs_duplicate += \
            _counters["lib_defs_duplicate"]
        sheet_data.pins_duplicate += _counters["pins_duplicate"]
        if _counters["coords_unreadable_pins"]:
            _coords("pins", _counters["coords_unreadable_pins"])
        for lib_id, libdef in _defs.items():
            if lib_id in sheet_data.lib_symbols:
                # A second (kicad_sch ...) root redefining a lib_id:
                # the first root wins (setdefault, as before) and
                # the duplicate is counted, not silent (audit F7).
                sheet_data.lib_defs_duplicate += 1
            sheet_data.lib_symbols.setdefault(lib_id, libdef)
        for node in sexpr.children(root, "symbol"):
            props = _properties(node)
            ref = props.get("Reference", "").strip()
            if not ref:
                # An unannotated symbol: counted and reported by the
                # caller, never silently dropped (audit finding 22).
                if props.get("Value") or props.get("Footprint"):
                    skipped_no_ref += 1
                continue
            lib = sexpr.child(node, "lib_id")
            unit_node = sexpr.child(node, "unit")
            unit = 1
            if unit_node is not None and len(unit_node) >= 2:
                parsed_unit = _safe_int(unit_node[1])
                unit = parsed_unit if parsed_unit is not None else 1
            value = props.get("Value", "")
            at = _at_of(node)
            mirror_node = sexpr.child(node, "mirror")
            mirror = None
            if mirror_node is not None and len(mirror_node) >= 2 \
                    and isinstance(mirror_node[1], str) \
                    and mirror_node[1] in ("x", "y"):
                mirror = mirror_node[1]
            pin_numbers = tuple(
                pin[1] for pin in sexpr.children(node, "pin")
                if len(pin) >= 2 and isinstance(pin[1], str))
            symbols.append(Symbol(
                ref=ref,
                value=value,
                footprint=props.get("Footprint", None),
                lib_id=lib[1] if lib is not None and len(lib) >= 2
                       and isinstance(lib[1], str) else "",
                unit=unit,
                in_bom=_flag(node, "in_bom", True),
                on_board=_flag(node, "on_board", True),
                # KiCad 6 encodes DNP as the value text + (in_bom no);
                # KiCad 7+ has an explicit (dnp yes) node.
                dnp=_flag(node, "dnp", False)
                    or value.strip().upper() == "DNP",
                props=props,
                sheet=source_name,
                x=at[0] if at else None,
                y=at[1] if at else None,
                rot=at[2] if at else 0.0,
                mirror=mirror,
                pin_numbers=pin_numbers,
            ))
        # --- v0.5 connectivity constructs ------------------------------
        for node in sexpr.children(root, "wire"):
            points, bad_pts = _pts_of(node)
            if bad_pts:
                _coords("wire_points", bad_pts)
            if len(points) >= 2:
                sheet_data.wires.append(points)
        for node in sexpr.children(root, "bus"):
            points, bad_pts = _pts_of(node)
            if bad_pts:
                _coords("wire_points", bad_pts)
            if len(points) >= 2:
                sheet_data.buses.append(points)
        for node in sexpr.children(root, "bus_alias"):
            if len(node) >= 2 and isinstance(node[1], str) \
                    and node[1] not in sheet_data.bus_aliases:
                members_node = sexpr.child(node, "members")
                members = []
                if members_node is not None:
                    members = [a for a in members_node[1:]
                               if isinstance(a, str)]
                sheet_data.bus_aliases[node[1]] = members
        for node in sexpr.children(root, "bus_entry"):
            at = _at_of(node)
            size = sexpr.child(node, "size")
            if at is not None and size is not None and len(size) >= 3:
                w, h = _num(size[1]), _num(size[2])
                if w is not None and h is not None:
                    sheet_data.bus_entries.append(
                        ((at[0], at[1]), (at[0] + w, at[1] + h)))
                else:
                    _coords("other")
            elif at is None:
                _coords("other")
        for kind in ("label", "global_label", "hierarchical_label"):
            for node in sexpr.children(root, kind):
                at = _at_of(node)
                if at is not None and len(node) >= 2 \
                        and isinstance(node[1], str):
                    sheet_data.labels.append(LabelRec(
                        name=node[1], kind=kind,
                        x=at[0], y=at[1], rot=at[2]))
                elif at is None:
                    _coords("labels")
        for node in sexpr.children(root, "junction"):
            at = _at_of(node)
            if at is not None:
                sheet_data.junctions.append((at[0], at[1]))
            else:
                _coords("other")
        for node in sexpr.children(root, "no_connect"):
            at = _at_of(node)
            if at is not None:
                sheet_data.no_connect_pts.append((at[0], at[1]))
            else:
                _coords("other")
        no_connects += sum(1 for _ in sexpr.children(root, "no_connect"))
        for sheet in sexpr.children(root, "sheet"):
            sprops = _properties(sheet)
            if sprops.get("Sheetfile"):
                sheet_files.append(sprops["Sheetfile"])
            block = SheetBlockRec(
                name=sprops.get("Sheetname", ""),
                file=sprops.get("Sheetfile", ""))
            for pin in sexpr.children(sheet, "pin"):
                at = _at_of(pin)
                if at is not None and len(pin) >= 2 \
                        and isinstance(pin[1], str):
                    block.pins.append(SheetPinRec(
                        name=pin[1],
                        type=pin[2] if len(pin) >= 3
                             and isinstance(pin[2], str) else "",
                        x=at[0], y=at[1], rot=at[2]))
                elif at is None:
                    _coords("other")
            sheet_data.sheets.append(block)
        if not title:
            title_block = sexpr.child(root, "title_block")
            if title_block is not None:
                title_node = sexpr.child(title_block, "title")
                if title_node is not None and len(title_node) >= 2 \
                        and isinstance(title_node[1], str):
                    title = title_node[1]
    return symbols, no_connects, title, sheet_files, skipped_no_ref, \
        format_version, generator, stamp_raw, sheet_data


def _layer_of(node):
    layer = sexpr.child(node, "layer")
    if layer is not None and len(layer) >= 2 and isinstance(layer[1], str):
        return layer[1]
    return ""


def _at_of(node):
    """(x, y, rotation_degrees) from an (at ...) child, or None.

    Values that are present but unreadable return None — callers
    count the object as unplaced rather than guessing a position."""
    at = sexpr.child(node, "at")
    if at is None:
        return None
    vals = [a for a in at[1:] if not isinstance(a, list)]
    if len(vals) < 2:
        return None
    x, y = _num(vals[0]), _num(vals[1])
    if x is None or y is None:
        return None
    rot = 0.0
    if len(vals) >= 3:
        rot = _num(vals[2])
        if rot is None:
            return None
    return x, y, rot


def _point_of(node, name):
    """(x, y) from a named point child like (start 1.0 2.0), or None."""
    pt = sexpr.child(node, name)
    if pt is None or len(pt) < 3:
        return None
    x, y = _num(pt[1]), _num(pt[2])
    if x is None or y is None:
        return None
    return x, y


def _net_of(node):
    """(net_id, net_name) from a (net 12 "GND") child.

    (None, "") when no net node exists. An id that cannot be read as
    an integer yields (None, name) — identity unknown, never guessed.
    """
    net = sexpr.child(node, "net")
    if net is None or len(net) < 2:
        return None, ""
    net_id = _safe_int(net[1])
    name = net[2] if len(net) >= 3 and isinstance(net[2], str) else ""
    return net_id, name


def _copper_layer_names(node, all_copper):
    """Copper layers named by a (layers ...) node, expanded.

    '*.Cu' expands to every copper layer the board declares;
    KiCad's compact 'F&B.Cu' token expands per side. Non-copper
    layers (Mask/SilkS/...) are dropped. Layer matching is
    case-insensitive but canonical board spellings are returned."""
    layers_node = sexpr.child(node, "layers")
    if layers_node is None:
        return ()
    canon = {name.lower(): name for name in all_copper}
    found = []
    for atom in layers_node[1:]:
        if not isinstance(atom, str):
            continue
        tokens = [atom]
        if "&" in atom and atom.lower().endswith(".cu"):
            suffix = atom[atom.index("."):]
            tokens = [part + suffix for part in atom.split("&")]
        for token in tokens:
            low = token.lower()
            if low == "*.cu":
                for name in all_copper:
                    if name not in found:
                        found.append(name)
            elif low in canon and canon[low] not in found:
                found.append(canon[low])
    return tuple(found)


def _stroke_width(node):
    """Stroke/line width of a graphic item (KiCad 6 (width ...) or
    KiCad 7+ (stroke (width ...))), or None when unreadable."""
    width = sexpr.child(node, "width")
    if width is not None and len(width) >= 2:
        return _num(width[1])
    stroke = sexpr.child(node, "stroke")
    if stroke is not None:
        inner = sexpr.child(stroke, "width")
        if inner is not None and len(inner) >= 2:
            return _num(inner[1])
    return None


_PAD_MODELLED_SHAPES = {"circle", "rect", "oval", "roundrect"}


# Graphic-item heads the geometry layer already models exactly
# (see _edge_elem): a custom pad whose (primitives ...) consist
# solely of these — plus non-shape annotation items — has a
# modelled primitive set in the sense of the PCB file format's
# custom-pad-primitives section.
_CUSTOM_PRIMITIVE_MODELLED = {"gr_line", "gr_rect", "gr_circle",
                              "gr_arc", "gr_poly", "gr_curve"}
_CUSTOM_PRIMITIVE_ANNOTATION = {"gr_text", "gr_text_box", "gr_bbox"}


def _custom_primitives_modelled(pad):
    """True when a custom pad's primitive set is fully modelled.

    Every shape primitive must be a graphic kind _edge_elem can
    rebuild exactly, with readable points; annotation items
    (text / text box / annotation bounding box — the file format
    defines the last as locating the pad number, not copper) are
    not shape primitives and do not disqualify. Any other head,
    an unreadable primitive, or no shape primitive at all means
    the set is NOT modelled."""
    prims = sexpr.child(pad, "primitives")
    if prims is None:
        return False
    found = False
    for item in prims[1:]:
        if not isinstance(item, list):
            continue
        head = sexpr.head(item)
        if head in ("width", "fill"):
            continue
        if head in _CUSTOM_PRIMITIVE_MODELLED:
            if _edge_elem(item) is None:
                return False
            found = True
        elif head in _CUSTOM_PRIMITIVE_ANNOTATION:
            continue
        else:
            return False
    return found


def _pad_geo(pad, ref, fp_at, fp_flipped, all_copper, geo):
    """Record one pad as a copper object in geo.pads (v0.4).

    Non-plated holes (np_thru_hole) carry no copper annulus and are
    counted, not modelled. Custom and trapezoid pads are counted as
    shape-skipped: their exact outline cannot be derived from the
    stored parameters without risking a wrong shape (a wrong shape
    could manufacture a false clearance violation), so the distance
    checks abstain for them and Coverage says so. Pad position uses
    KiCad's rotation convention (verified empirically against track
    endpoints in real boards — see geometry.py).

    v0.6 WS3 (parity only): a shape-skipped pad still states its
    (ref, pad name, net) in the file, and membership needs exactly
    that — not clearance-grade geometry. A trapezoid pad, or a
    custom pad whose primitive set is modelled
    (_custom_primitives_modelled), with a readable placement and
    size is therefore ALSO recorded on pads_membership_extra —
    the same side list NPTH pads use — so netlist parity can
    measure its ref. It remains shape-skipped for geometry, and
    pads_shape_skipped still counts it. A shape-skipped pad that
    fails those tests (custom pad with unmodelled primitives,
    unreadable placement/size) records a membership-missing count
    instead, and parity keeps abstaining for its ref."""
    pad_type = pad[2] if len(pad) >= 3 and isinstance(pad[2], str) else ""
    pad_type = pad_type.strip().lower()
    if pad_type == "np_thru_hole":
        geo.pads_non_copper += 1
        # Audit F3: an NPTH pad is not copper geometry, but KiCad
        # does assign it a net (grounded mounting/shield holes) and
        # parity membership needs no shape modelling -- record the
        # stated (ref, pad name, net) on the side list for netlist
        # parity only.  NOT a pad issue: the geometry layer is
        # unchanged and the ref is not made unmeasurable by this.
        if ref:
            _nid, _nname = _net_of(pad)
            geo.pads_membership_extra.append(PadGeo(
                ref=ref,
                name=pad[1] if len(pad) >= 2
                and isinstance(pad[1], str) else "",
                shape="np_thru_hole",
                net_id=_nid, net_name=_nname))
        return

    def _issue():
        if ref:
            geo.pad_issues_by_ref[ref] = \
                geo.pad_issues_by_ref.get(ref, 0) + 1

    def _missing():
        # A pad measurable in NEITHER sense: parity abstains for
        # its ref on this count (v0.6 WS3).
        _issue()
        if ref:
            geo.pad_membership_missing_by_ref[ref] = \
                geo.pad_membership_missing_by_ref.get(ref, 0) + 1

    shape = pad[3] if len(pad) >= 4 and isinstance(pad[3], str) else ""
    shape = shape.strip().lower()
    at = _at_of(pad)
    size = sexpr.child(pad, "size")
    sx = sy = None
    if size is not None and len(size) >= 3:
        sx, sy = _num(size[1]), _num(size[2])
    placed = fp_at is not None and at is not None \
        and sx is not None and sy is not None and sx > 0 and sy > 0
    if shape not in _PAD_MODELLED_SHAPES:
        geo.pads_shape_skipped += 1
        if placed and ref and (shape == "trapezoid" or (
                shape == "custom"
                and _custom_primitives_modelled(pad))):
            # Membership-only record (parity); NEVER a geometry
            # object — geometry.pads is untouched by this branch.
            _nid, _nname = _net_of(pad)
            geo.pads_membership_extra.append(PadGeo(
                ref=ref,
                name=pad[1] if len(pad) >= 2
                and isinstance(pad[1], str) else "",
                shape=shape,
                net_id=_nid, net_name=_nname))
        else:
            _missing()
        return
    if fp_at is None or at is None or sx is None or sy is None:
        geo.pads_unplaced += 1
        _missing()
        return
    if sx <= 0 or sy <= 0:
        # A non-positive pad size is not a copper shape (audit
        # F7: a -2 mm pad silently measured distances from a
        # negative radius). Counted with the other excluded
        # objects so Coverage stays honest.
        geo.pads_unplaced += 1
        _missing()
        return
    fx, fy, frot = fp_at
    lx, ly, prot = at
    # No mirroring for back-side footprints: KiCad rewrites a
    # flipped footprint's pad positions (and orientations) into
    # the same local convention when it saves the board, so the
    # plain rotation below renders front and back parts alike.
    # Verified on corpus B.Cu parts: pad centres computed this
    # way land on their same-net connections (median distance
    # 0.000 mm); an X-mirror — the intuitive guess — lands them
    # a median 3.1 mm away, inside foreign copper.
    fr = math.radians(frot)
    cx = fx + lx * math.cos(fr) + ly * math.sin(fr)
    cy = fy - lx * math.sin(fr) + ly * math.cos(fr)
    # Pad SHAPE axes use the pad's stored rotation ALONE. In a
    # .kicad_pcb, KiCad writes each pad's orientation as its
    # absolute board-frame orientation (the footprint rotation is
    # already composed into it at save time); composing it with
    # the footprint rotation again double-rotates every pad whose
    # footprint is placed rotated. Verified empirically on the
    # corpus boards: with the stored rotation alone, a SOT-23's
    # long axis and an ESP32 module pad both land exactly one
    # design-clearance (0.2 mm) from the neighbouring foreign-net
    # tracks/vias; with the composed rotation they overlap them.
    total = math.radians(prot)
    cos_t, sin_t = math.cos(total), math.sin(total)
    rratio = 0.0
    if shape == "roundrect":
        rr = sexpr.child(pad, "roundrect_rratio")
        if rr is not None and len(rr) >= 2:
            value = _num(rr[1])
            if value is not None:
                rratio = min(max(value, 0.0), 0.5)
    net_id, net_name = _net_of(pad)
    geo.pads.append(PadGeo(
        ref=ref,
        name=pad[1] if len(pad) >= 2 and isinstance(pad[1], str) else "",
        shape=shape, cx=cx, cy=cy,
        ux=cos_t, uy=-sin_t, vx=sin_t, vy=cos_t,
        sx=sx, sy=sy, rratio=rratio,
        layers=_copper_layer_names(pad, all_copper),
        net_id=net_id, net_name=net_name))


def _edge_elem(node):
    """An EdgeElem from a root-level graphic item, or None when its
    defining points cannot be read (caller counts it as unplaced —
    the closure check then abstains rather than guessing)."""
    head = sexpr.head(node)
    if head in ("gr_line", "segment"):
        p1, p2 = _point_of(node, "start"), _point_of(node, "end")
        if p1 and p2:
            return EdgeElem("line", (p1, p2))
    elif head in ("gr_arc", "arc"):
        p1, p2 = _point_of(node, "start"), _point_of(node, "end")
        mid = _point_of(node, "mid")
        if p1 and p2 and mid:
            return EdgeElem("arc", (p1, mid, p2))
        angle_node = sexpr.child(node, "angle")
        if p1 and p2 and angle_node is not None and len(angle_node) >= 2:
            angle = _num(angle_node[1])
            if angle is not None:
                return EdgeElem("arc", (p1, p2), angle=angle)
    elif head == "gr_circle":
        centre = _point_of(node, "center")
        end = _point_of(node, "end")
        if centre and end:
            radius = math.hypot(end[0] - centre[0], end[1] - centre[1])
            return EdgeElem("circle", (centre, radius))
    elif head == "gr_rect":
        p1, p2 = _point_of(node, "start"), _point_of(node, "end")
        if p1 and p2:
            return EdgeElem("rect", (p1, p2))
    elif head in ("gr_poly", "gr_curve"):
        pts_node = sexpr.child(node, "pts")
        pts = []
        ok = pts_node is not None
        if ok:
            for pt in pts_node[1:]:
                if isinstance(pt, list) and sexpr.head(pt) == "xy" \
                        and len(pt) >= 3:
                    x, y = _num(pt[1]), _num(pt[2])
                    if x is None or y is None:
                        ok = False
                        break
                    pts.append((x, y))
                else:
                    ok = False
                    break
        if ok and head == "gr_poly" and len(pts) >= 3:
            return EdgeElem("poly", tuple(pts))
        if ok and head == "gr_curve" and len(pts) == 4:
            return EdgeElem("curve", tuple(pts))
    return None


def _drill_diameter(drill):
    """Diameter from a (drill ...) node, or None.

    The diameter is the first numeric scalar after the head; named
    sub-nodes like (offset 0 0) may precede it in third-party files
    and are skipped as lists, never read as a diameter."""
    for item in drill[1:]:
        if isinstance(item, list):
            continue
        value = _num(item)
        if value is not None:
            return value
    return None


def _pad_geometry(pad, ref, geo):
    """Record drill/ring/slot measurements for one pad node.

    Only plated (thru_hole) pads take part: a non-plated mounting hole
    (np_thru_hole) has pad size == drill by design, so counting it
    would report a zero 'annular ring' on every healthy board (seen
    on all three census corpus boards during the v0.3 calibration).
    The type token is matched case-insensitively — KiCad writes
    canonical case, but a hand-edited or third-party file is exactly
    the untrusted input this tool audits. Dimensions that are present
    but unreadable are counted in geo.unparsed_dims, never dropped
    silently.
    """
    pad_type = pad[2] if len(pad) >= 3 and isinstance(pad[2], str) else ""
    if pad_type.strip().lower() != "thru_hole":
        return
    drill = sexpr.child(pad, "drill")
    if drill is None or len(drill) < 2:
        return
    size = sexpr.child(pad, "size")
    size_dims = None
    if size is not None and len(size) >= 3:
        sx, sy = _num(size[1]), _num(size[2])
        if sx is not None and sy is not None:
            size_dims = (sx, sy)
    if drill[1] == "oval":
        dims = [_num(drill[2]) if len(drill) >= 3 else None,
                _num(drill[3]) if len(drill) >= 4 else None]
        if any(d is None for d in dims):
            geo.unparsed_dims += 1
        dims = [d for d in dims if d is not None]
        if dims:
            geo.slot_widths.append((min(dims), ref))
            if size_dims is not None:
                # Ring along the narrow axis of an oval pad.
                geo.pad_rings.append(
                    ((min(size_dims) - min(dims)) / 2, ref))
        return
    drill_d = _drill_diameter(drill)
    if drill_d is None:
        geo.unparsed_dims += 1
        return
    geo.pad_drills.append(drill_d)
    if size_dims is not None:
        geo.pad_rings.append(((min(size_dims) - drill_d) / 2, ref))


def _zone_fill_state(zone):
    """'filled' / 'unfilled' / None (not a copper pour) for a root zone.

    A keepout / rule-area zone has no filled polygon BY DESIGN and is
    not a pour at all (None). A pour counts as filled only when every
    layer it declares carries a (filled_polygon ...) whose (pts ...)
    holds at least one point — an empty fill node is not a fill, and
    a two-layer zone filled on one layer is a partial (stale) fill.
    """
    if sexpr.child(zone, "keepout") is not None:
        return None
    layers_node = sexpr.child(zone, "layers")
    declared = []
    if layers_node is not None:
        for atom in layers_node[1:]:
            if not isinstance(atom, str):
                continue
            if "&" in atom:
                # KiCad's compact multi-layer notation: 'F&B.Cu'
                # means F.Cu and B.Cu in a single token.
                parts = atom.split("&")
                suffix = ""
                if "." in parts[-1]:
                    suffix = parts[-1][parts[-1].index("."):]
                declared.extend(p if "." in p else p + suffix
                                for p in parts)
            else:
                declared.append(atom)
    if not declared:
        single = _layer_of(zone)
        declared = [single] if single else []
    filled_layers = set()
    any_fill = False
    for fill in sexpr.children(zone, "filled_polygon"):
        pts = sexpr.child(fill, "pts")
        points = 0
        if pts is not None:
            points = sum(1 for p in pts[1:]
                         if isinstance(p, list) and sexpr.head(p) == "xy")
        if points == 0:
            continue
        any_fill = True
        layer = _layer_of(fill)
        if layer:
            filled_layers.add(layer)
    if not any_fill:
        return "unfilled"
    if declared:
        # Declared layers may be wildcards ('*.Cu' is what KiCad
        # writes for all-copper zones) — match them as globs. The
        # cross product is bounded (v0.4 audit: a crafted zone with
        # thousands of declared/filled layers made this quadratic in
        # fnmatch calls): exact names go through the set, and the
        # glob scan only runs when both sides are plausibly sized —
        # real boards declare at most 32 copper layers.
        filled_set = set(filled_layers)
        for layer in declared:
            if layer in filled_set:
                continue
            if not any(mark in layer for mark in "*?["):
                return "unfilled"
            if len(filled_layers) > 256 or len(declared) > 1024:
                return "unfilled"
            if not any(fnmatch.fnmatchcase(filled, layer)
                       for filled in filled_layers):
                return "unfilled"
    return "filled"


def _zone_geo(zone, geo, canon_layer):
    """Append a ZoneGeo for one root-level zone node (v0.6 WS3).

    Net identity comes from the zone's own (net N) / (net_name
    ...) nodes — KiCad stores them separately on zones, unlike
    pads/tracks where (net N "name") is one node; the board's
    (net ...) table fills the name in later when the zone states
    only an id. Each (filled_polygon ...) contributes one ring:
    its (layer ...) canonicalised, its points exactly as stored.
    A ring with unreadable points is dropped and counted
    (zones_fill_unreadable); a ring with fewer than 3 points is
    empty copper and dropped silently-by-design (it is the
    stale/empty-fill case _zone_fill_state already reports)."""
    net_node = sexpr.child(zone, "net")
    net_id = None
    if net_node is not None and len(net_node) >= 2:
        net_id = _safe_int(net_node[1])
    name_node = sexpr.child(zone, "net_name")
    net_name = name_node[1] if name_node is not None \
        and len(name_node) >= 2 and isinstance(name_node[1], str) else ""
    zone_name_node = sexpr.child(zone, "name")
    zone_name = zone_name_node[1] if zone_name_node is not None \
        and len(zone_name_node) >= 2 \
        and isinstance(zone_name_node[1], str) else ""
    polygons = []
    for fill in sexpr.children(zone, "filled_polygon"):
        layer = _layer_of(fill)
        if layer:
            layer = canon_layer.get(layer.lower(), layer)
        points, unreadable = _pts_of(fill)
        if unreadable:
            geo.zones_fill_unreadable += 1
            continue
        if len(points) < 3:
            continue
        polygons.append((layer, tuple(points)))
    geo.zones_geo.append(ZoneGeo(
        net_id=net_id, net_name=net_name, name=zone_name,
        polygons=polygons))


def parse_pcb(text):
    """Return (footprints, skipped_no_ref, geometry, format_version,
    generator_version, stamp_raw, nets) from .kicad_pcb text.

    References/values are read from BOTH storage styles: KiCad 7/8
    (property "Reference" ...) and KiCad 6 (fp_text reference ...).
    (attr ...) flags are captured (board_only, exclude_from_bom,
    exclude_from_pos_files). Footprints with no reference at all are
    counted by the caller via the second return value. Geometry
    (segment/arc widths, via/pad drills, annular rings, slots, zone
    fills, outline presence) is collected for the fab-capability
    checks in fabchecks.py, and object-level geometry (pads, tracks,
    arcs, vias, outline elements, each with layer and net identity)
    for the geometry-lite distance checks in geometry.py. `nets`
    maps net id -> net name from the file's (net ...) declarations.
    """
    forms = sexpr.parse(text)
    root = next((f for f in forms if sexpr.head(f) == "kicad_pcb"), None)
    if root is None:
        raise SexprError("no (kicad_pcb ...) root form")
    format_version, generator, stamp_raw = _file_versions(root)
    geo = PcbGeometry()
    footprints = []
    skipped_no_ref = 0
    nets = {}
    for node in sexpr.children(root, "net"):
        if len(node) >= 3:
            _nid = _safe_int(node[1])
            if _nid is not None:
                nets[_nid] = \
                    node[2] if isinstance(node[2], str) else ""
    layers_node = sexpr.child(root, "layers")
    if layers_node is not None:
        for entry in layers_node[1:]:
            if isinstance(entry, list) and len(entry) >= 2 \
                    and isinstance(entry[1], str) \
                    and entry[1].lower().endswith(".cu"):
                geo.copper_layers.append(entry[1])
    if not geo.copper_layers:
        geo.copper_layers = ["F.Cu", "B.Cu"]
    for node in sexpr.children(root, "footprint"):
        name = node[1] if len(node) >= 2 and isinstance(node[1], str) else ""
        props = _properties(node)
        ref = props.get("Reference", "").strip()
        value = props.get("Value", "")
        for fp_text in sexpr.children(node, "fp_text"):
            if len(fp_text) >= 3 and isinstance(fp_text[2], str):
                if fp_text[1] == "reference" and not ref:
                    ref = fp_text[2].strip()
                elif fp_text[1] == "value" and not value:
                    value = fp_text[2]
        fp_layer = _layer_of(node)
        fp_at = _at_of(node)
        fp_flipped = fp_layer.lower().startswith("b.")
        for pad in sexpr.children(node, "pad"):
            if ref:
                geo.pad_total_by_ref[ref] = \
                    geo.pad_total_by_ref.get(ref, 0) + 1
            _pad_geometry(pad, ref, geo)
            _pad_geo(pad, ref, fp_at, fp_flipped, geo.copper_layers, geo)
        if not ref:
            skipped_no_ref += 1
            continue
        attr = sexpr.child(node, "attr")
        attr_tokens = set()
        if attr is not None:
            attr_tokens = {a for a in attr[1:] if isinstance(a, str)}
        footprints.append(FootprintInst(
            ref=ref, name=name, value=value,
            board_only="board_only" in attr_tokens,
            exclude_from_bom="exclude_from_bom" in attr_tokens,
            exclude_from_pos="exclude_from_pos_files" in attr_tokens))
    # Root-level board items: tracks, vias, zones, outline drawings.
    # Layer names are matched case-insensitively: KiCad writes
    # canonical case, but the file is untrusted input. Track/arc
    # layers are canonicalised to the board's declared spellings so
    # geometry bucketing never splits 'f.cu' from 'F.Cu' (v0.4
    # review RT-17).
    canon_layer = {name.lower(): name for name in geo.copper_layers}
    for node in root[1:]:
        if not isinstance(node, list):
            continue
        node_head = sexpr.head(node)
        layer = _layer_of(node)
        if node_head in ("segment", "arc"):
            # Copper tracks: straight segments and (arc ...) curves.
            if layer.lower().endswith(".cu"):
                layer = canon_layer.get(layer.lower(), layer)
                width = sexpr.child(node, "width")
                w = None
                if width is not None and len(width) >= 2:
                    w = _num(width[1])
                    if w is not None:
                        geo.segment_widths.append(w)
                    else:
                        geo.unparsed_dims += 1
                start = _point_of(node, "start")
                end = _point_of(node, "end")
                net_id, net_name = _net_of(node)
                if node_head == "segment":
                    if start and end and w is not None:
                        geo.tracks.append(TrackGeo(
                            start[0], start[1], end[0], end[1], w,
                            layer, net_id, net_name))
                    elif w is not None:
                        geo.items_unplaced += 1
                else:
                    mid = _point_of(node, "mid")
                    if start and end and mid and w is not None:
                        geo.arcs.append(ArcGeo(
                            start[0], start[1], mid[0], mid[1],
                            end[0], end[1], w, layer, net_id, net_name))
                    elif w is not None:
                        geo.items_unplaced += 1
            elif layer.lower() == "edge.cuts":
                geo.edge_items += 1
                elem = _edge_elem(node)
                if elem is not None:
                    geo.edge_elems.append(elem)
                else:
                    geo.items_unplaced += 1
        elif node_head == "via":
            size = sexpr.child(node, "size")
            size_val = None
            if size is not None and len(size) >= 2:
                size_val = _num(size[1])
                if size_val is not None:
                    geo.via_sizes.append(size_val)
                else:
                    geo.unparsed_dims += 1
            drill = sexpr.child(node, "drill")
            if drill is not None and len(drill) >= 2:
                v = _drill_diameter(drill)
                if v is not None:
                    geo.via_drills.append(v)
                else:
                    geo.unparsed_dims += 1
            at = _at_of(node)
            if at is not None and size_val is not None:
                net_id, net_name = _net_of(node)
                via_layers = _copper_layer_names(node, geo.copper_layers)
                if not via_layers:
                    # A via with no (layers ...) node: KiCad's plain
                    # through via spans the whole stack (v0.4 review
                    # RT-16 — silently dropping it hid violations).
                    via_layers = tuple(geo.copper_layers)
                else:
                    # KiCad lists only a via's END layers ("F.Cu"
                    # "B.Cu" for a through via); the barrel occupies
                    # every copper layer between them (v0.4 review
                    # RT-15). Expand to the full stack range.
                    idx = [geo.copper_layers.index(n)
                           for n in via_layers]
                    via_layers = tuple(
                        geo.copper_layers[min(idx):max(idx) + 1])
                geo.vias.append(ViaGeo(
                    at[0], at[1], size_val, via_layers,
                    net_id, net_name))
            elif size_val is not None:
                geo.items_unplaced += 1
        elif node_head == "zone":
            state = _zone_fill_state(node)
            if state is not None:
                geo.zones_total += 1
                if state == "unfilled":
                    geo.zones_unfilled += 1
                _zone_geo(node, geo, canon_layer)
        elif node_head in ("gr_line", "gr_arc", "gr_circle", "gr_rect",
                           "gr_poly", "gr_curve"):
            if layer.lower() == "edge.cuts":
                geo.edge_items += 1
                elem = _edge_elem(node)
                if elem is not None:
                    geo.edge_elems.append(elem)
                else:
                    geo.items_unplaced += 1
            elif layer.lower().endswith(".cu"):
                # Board-level copper graphics (drawn pours, logos in
                # copper): copper for edge-distance purposes. They
                # carry no net, so the clearance check skips them.
                elem = _edge_elem(node)
                width = _stroke_width(node)
                if elem is not None and width is not None:
                    geo.gr_copper.append((elem, width))
                else:
                    geo.items_unplaced += 1
    # Net-name resolution (v0.4 review FP-1 / RT-10): KiCad writes
    # only (net N) on segments, arcs and vias — the name lives in
    # the board's (net N "name") table. Without this pass, netclass
    # pattern thresholds silently never applied to tracks and vias.
    if nets:
        for group in (geo.pads, geo.tracks, geo.arcs, geo.vias):
            for obj in group:
                if not obj.net_name and obj.net_id in nets:
                    obj.net_name = nets[obj.net_id]
        for zone in geo.zones_geo:
            if not zone.net_name and zone.net_id in nets:
                zone.net_name = nets[zone.net_id]
    return footprints, skipped_no_ref, geo, format_version, generator, \
        stamp_raw, nets


# ---------------------------------------------------------------------------
# Project discovery / loading
# ---------------------------------------------------------------------------


def _first(folder, pattern):
    matches = sorted(folder.glob(pattern))
    return matches[0] if matches else None


def _pick(folder, stem, pattern):
    """Prefer the file whose stem matches the project stem, else the first."""
    if stem:
        candidate = folder / pattern.replace("*", stem)
        if candidate.is_file():
            return candidate
    return _first(folder, pattern)


def find_bom(folder, stem):
    """Auto-detect the BOM CSV in a folder. Returns (path, note).

    Candidates are tried in order — files with 'bom' in the name first
    (any case), then any other CSV — and the first whose header parses
    as a BOM wins, so a pick-and-place CSV is never mistaken for the
    BOM. Returns (None, note) when no CSV parses as a BOM; a bom-named
    file that fails to parse is returned as (path, note) so the caller
    can report it as unreadable instead of pretending there is no BOM.
    """
    candidates = sorted(folder.glob("*.csv")) + sorted(folder.glob("*.CSV"))
    seen, ordered = set(), []
    for cand in candidates:
        if cand not in seen:
            seen.add(cand)
            ordered.append(cand)
    bom_named = [c for c in ordered if "bom" in c.name.lower()]
    ordered = bom_named + [c for c in ordered if c not in bom_named]
    first_bom_named_failure = None
    for cand in ordered:
        try:
            text = read_project_file(cand)
        except SexprError:
            if cand in bom_named and first_bom_named_failure is None:
                first_bom_named_failure = cand
            continue
        if is_placement_text(text) and not is_bom_like_text(text):
            continue            # a pure position file is not a BOM.
            # A file carrying BOM-only columns (quantity / MPN /
            # LCSC) alongside position columns is treated as a BOM:
            # position files never carry part numbers (audit M9).
        try:
            parse_bom(text)
            return cand, ("auto-detected" if len(ordered) > 1
                          else "found in project folder")
        except ValueError:
            if cand in bom_named and first_bom_named_failure is None:
                first_bom_named_failure = cand
            continue
    if first_bom_named_failure is not None:
        return first_bom_named_failure, "named like a BOM but unparseable"
    return None, ("CSV files present but none parses as a BOM"
                  if ordered else "no CSV found")


_CPL_NAME_HINTS = ("pos", "cpl", "placement", "pick", "centroid",
                   "position")


def find_cpl(folder, bom_path):
    """Auto-detect position/CPL CSV(s) in the project folder.

    Returns (path, note, refs, error, files, broken). ALL files that
    parse as position files are merged — real projects ship one file
    per board side (top/bottom), and checking only the first would
    blame the other side's parts as missing. The BOM file itself is
    never a candidate. A file that fails to parse is recorded in
    `broken` when its name suggests a position file OR its header
    has the position shape (designator + X + Y columns) — a failure
    must never be silently dropped just because the filename was
    neutral (red-team C05 / audit M7). When nothing parses, the first
    recorded failure is returned as (path, ..., error) so the caller
    reports CPL_UNREADABLE; absence of any position-like file is not
    an error (plenty of orders are PCB-only).
    """
    candidates = sorted(folder.glob("*.csv")) + sorted(folder.glob("*.CSV"))
    seen, ordered = set(), []
    for cand in candidates:
        if cand not in seen and cand != bom_path:
            seen.add(cand)
            ordered.append(cand)
    named = [c for c in ordered
             if any(h in c.name.lower() for h in _CPL_NAME_HINTS)]
    ordered = named + [c for c in ordered if c not in named]
    successes = []               # (path, refs)
    failures = []                # (path, error)
    for cand in ordered:
        try:
            text = read_project_file(cand)
        except SexprError as exc:
            if cand in named:
                failures.append((cand, str(exc)))
            continue
        try:
            refs = parse_placement(text)
            successes.append((cand, refs))
        except ValueError as exc:
            if cand in named or is_placement_like_text(text):
                failures.append((cand, str(exc)))
            continue
    if successes:
        refs = []
        for _, file_refs in successes:
            refs.extend(file_refs)
        files = [str(c) for c, _ in successes]
        note = ("auto-detected" if len(successes) == 1 else
                f"merged {len(successes)} position files")
        broken = [(c.name, e) for c, e in failures]
        return successes[0][0], note, refs, "", files, broken
    if failures:
        cand, error = failures[0]
        return cand, "named like a position file but unparseable", \
            [], error, [], []
    return None, "no position/CPL file found", [], "", [], []


def _positive(value):
    """A positive finite float from JSON data, or None.

    KiCad stores 0.0 for "not set" in design rules, and JSON from an
    untrusted project file may carry strings/bools — both mean the
    rule is not stated, never a threshold of zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def _board_rules(pro_data):
    """Design rules the project states for itself, from .kicad_pro.

    Reads board.design_settings.rules (min_clearance,
    min_copper_edge_clearance) and net_settings netclasses with
    their assignment patterns. Only positive values are kept — a
    stored 0.0 is KiCad's "unset". Malformed subtrees degrade to
    "not stated" field by field; nothing here can raise."""
    rules = {"min_clearance": None, "min_copper_edge_clearance": None,
             "netclasses": {}, "netclass_patterns": []}
    board = pro_data.get("board")
    if isinstance(board, dict):
        settings = board.get("design_settings")
        if isinstance(settings, dict):
            stated = settings.get("rules")
            if isinstance(stated, dict):
                rules["min_clearance"] = _positive(
                    stated.get("min_clearance"))
                rules["min_copper_edge_clearance"] = _positive(
                    stated.get("min_copper_edge_clearance"))
    net_settings = pro_data.get("net_settings")
    if isinstance(net_settings, dict):
        classes = net_settings.get("classes")
        if isinstance(classes, list):
            for entry in classes:
                if isinstance(entry, dict) \
                        and isinstance(entry.get("name"), str):
                    clearance = _positive(entry.get("clearance"))
                    if clearance is not None:
                        rules["netclasses"][entry["name"]] = clearance
        patterns = net_settings.get("netclass_patterns")
        if isinstance(patterns, list):
            # Capped: the list comes from untrusted JSON and is
            # matched per net name in the geometry pass (KiCad's
            # own pattern lists are dozens long at most).
            for entry in patterns[:1000]:
                if isinstance(entry, dict) \
                        and isinstance(entry.get("pattern"), str) \
                        and isinstance(entry.get("netclass"), str):
                    rules["netclass_patterns"].append(
                        (entry["pattern"], entry["netclass"]))
    return rules


def load_project(target, bom_override=None):
    """Load the project at `target` (a folder or a project/schematic/PCB file).

    Returns a Project. Files that exist but fail to parse are recorded in
    project.parse_errors rather than raising — the report turns them into
    BLOCK findings instead of crashing.
    """
    target = Path(target)
    if target.is_file():
        folder = target.parent
        stem_hint = target.stem
    else:
        folder = target
        stem_hint = None

    project = Project(folder=folder)

    pro = None
    if target.is_file() and target.suffix == ".kicad_pro":
        pro = target
    else:
        # Stem-aware (v0.6 review RT-10): a folder can hold stray or
        # other projects' .kicad_pro files; the project's own file
        # shares the project stem. Prefer a match with the target
        # stem or with a schematic/board stem in the folder; a lone
        # file is unambiguous; several files with no stem match are
        # ambiguous and NONE is used (fail-safe: settings and board
        # rules from another project must not leak into this one).
        candidates = sorted(folder.glob("*.kicad_pro"))
        if len(candidates) == 1:
            pro = candidates[0]
        elif candidates:
            stems = {stem_hint} if stem_hint else set()
            stems |= {p.stem for p in folder.glob("*.kicad_sch")}
            stems |= {p.stem for p in folder.glob("*.kicad_pcb")}
            match = [c for c in candidates if c.stem in stems]
            pro = match[0] if len(match) == 1 else None
    project.pro_path = pro

    stem = stem_hint or (pro.stem if pro else None)

    if target.is_file() and target.suffix == ".kicad_sch":
        project.sch_path = target
    else:
        project.sch_path = _pick(folder, stem, "*.kicad_sch")
    if target.is_file() and target.suffix == ".kicad_pcb":
        project.pcb_path = target
    else:
        project.pcb_path = _pick(folder, stem, "*.kicad_pcb")

    project.legacy_sch = sorted(folder.glob("*.sch"))
    all_sch = sorted(folder.glob("*.kicad_sch"))
    all_pcb = sorted(folder.glob("*.kicad_pcb"))

    # --- schematic(s): root sheet + hierarchical sub-sheets -----------
    name = ""
    loaded = set()
    queue = [project.sch_path] if project.sch_path else []
    while queue:
        sch_path = queue.pop(0)
        if sch_path is None or sch_path in loaded:
            continue
        loaded.add(sch_path)
        project.sch_paths_loaded.append(sch_path)
        try:
            symbols, no_connects, title, sheet_files, skipped, \
                fmt_version, generator, stamp_raw, sheet_data = \
                parse_schematic(read_project_file(sch_path),
                                source_name=sch_path.name)
            project.symbols.extend(symbols)
            project.sheet_data.append(sheet_data)
            project.no_connects += no_connects
            project.sch_symbols_no_ref += skipped
            if sch_path == project.sch_path:
                project.sch_format_version = fmt_version
                project.sch_generator = generator
                project.sch_stamp_raw = stamp_raw
            else:
                project.sub_sch_versions.append(
                    (sch_path.name, fmt_version, generator, stamp_raw))
            if not name:
                name = title
            for sheet_file in sheet_files:
                sub = (sch_path.parent / sheet_file)
                if sub.is_file():
                    if sub not in loaded and sub not in queue:
                        queue.append(sub)
                else:
                    project.missing_sheets.append(sheet_file)
        except SexprError as exc:
            project.parse_errors.append((sch_path, str(exc)))
    project.unloaded_sch = [p for p in all_sch if p not in loaded]
    project.unloaded_pcb = [p for p in all_pcb if p != project.pcb_path]

    # --- PCB ------------------------------------------------------------
    if project.pcb_path is not None:
        try:
            (project.footprints, project.pcb_footprints_no_ref,
             project.geometry, project.pcb_format_version,
             project.pcb_generator, project.pcb_stamp_raw,
             project.nets) = parse_pcb(
                read_project_file(project.pcb_path))
        except SexprError as exc:
            project.parse_errors.append((project.pcb_path, str(exc)))

    # --- board design rules stated in the .kicad_pro ------------------
    pro_data = None
    if pro is not None:
        try:
            parsed = json.loads(read_project_file(pro))
            if isinstance(parsed, dict):
                pro_data = parsed
        except (SexprError, ValueError, RecursionError):
            pro_data = None
    if pro_data is not None:
        project.board_rules = _board_rules(pro_data)

    # --- project name: schematic title > .kicad_pro > file stem --------
    if not name and pro is not None:
        filename = ""
        if pro_data is not None:
            inner = pro_data.get("meta")
            if isinstance(inner, dict):
                candidate = inner.get("filename", "")
                if isinstance(candidate, str):
                    filename = candidate
        name = Path(filename).stem if filename else pro.stem
    if not name:
        for path in (project.sch_path, project.pcb_path):
            if path is not None:
                name = path.stem
                break
    project.name = name or folder.name

    # --- BOM --------------------------------------------------------------
    bom_path = Path(bom_override) if bom_override else None
    if bom_path is None:
        bom_path, project.bom_auto_note = find_bom(folder, stem)
    else:
        project.bom_auto_note = "given on the command line"
    project.bom_path = bom_path
    if bom_path is not None:
        try:
            project.bom_rows = parse_bom(read_project_file(bom_path))
        except (ValueError, SexprError) as exc:
            project.bom_error = str(exc)

    # --- position / CPL file (assembly orders) -------------------------
    (project.cpl_path, project.cpl_auto_note, project.cpl_refs,
     project.cpl_error, project.cpl_files,
     project.cpl_broken) = find_cpl(folder, bom_path)
    return project
