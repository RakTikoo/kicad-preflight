"""Netlist extraction + ERC-lite + schematic<->PCB parity (v0.5).

This layer builds the schematic netlist natively from what
project.py captured (lib pin definitions, placed-symbol transforms,
wires, labels, junctions, no-connect markers, sheet blocks) and
checks a conservative subset of KiCad ERC plus pin-level parity
against the PCB's modelled pads. It is NOT KiCad ERC: the full
pin-type matrix, dangling-wire and similar checks stay delegated
to the kicad-cli layer when KiCad is installed (docs/COVERAGE.md).

Pin positions — verified empirically, not assumed: a lib pin's
local (x, y) is in library coordinates (Y up); the sheet is Y
down, so the transform is mirror first ((mirror x): y -> -y,
(mirror y): x -> -x, applied to the library point), then negate y
(library -> sheet), then rotate by the symbol's (at) rotation in
KiCad's file convention — rotating (x, y) by t maps it to
(x·cos t + y·sin t, -x·sin t + y·cos t), the same convention
geometry.py verified for pads — then translate by the symbol
position. With this transform, computed pin points coincide with
a wire endpoint/point, a label, a junction, a no-connect marker,
or another pin on 99.1% (upsy-desky), 92.9% (esp-rust-board) and
100% (piantor, both sides) of corpus pins; the textbook rotation
without the Y negation lands at 79–88% and mis-assigns named
pins (the ICM-42670's AP_SCL pin lands on the wire carrying the
IO8_SCL global label only under the transform above).

Connectivity follows KiCad semantics: coincident points connect;
a point lying on a wire segment connects to it; two segments
crossing at interior points connect ONLY when a junction exists
at the crossing; labels name nets (local labels are per-sheet,
global labels and power-symbol nets are global, hierarchical
labels bind to the same-named sheet pin in the parent sheet).
A hidden (invisible) power_in pin on an ordinary symbol connects
globally to the net named by the pin name, wherever it sits — the
documented legacy power-pin behaviour. KiCad semantics here were
reconciled against the official documentation on 2026-11-10 (see
kicad-preflight-review/research-netlist-semantics.md): in
particular the power-pin driven test accepts only power_out pins
as drivers, and the pin-conflict matrix also rates
power_out + power_out as an error.
Buses do not create connectivity here: a bus is a bundle of
unnamed signals, so a pin whose only connection is via a bus
segment / bus entry (an unnamed wire stub touching a bus) is
counted as bus-connected and excluded from every rule below.
v0.6 WS3 sharpening (evidence-based, no behaviour change): the
corpus and samples contain NO bus segments, bus entries, bus
aliases or bus-notation labels at all, so bus expansion has no
real file to validate against. What IS sound without expansion:
a stub wire carrying a member label already merges by that
label's name above (KiCad connectivity for a ripped-out member
is by its label; the bus is graphical grouping), and those nets
are counted (bus_named_nets), not excluded. What is NOT claimed:
selecting a member for an UNLABELLED stub from a bus label's
syntax — vector (NAME[a..b]), group (NAME{a b c} / {a b c}) or
(bus_alias ...) definitions, whose prefixing rules (members of
a named group bus are PREFIX.member) and cross-sheet behaviour
cannot be verified against any file available here. Those pins
stay excluded + counted exactly as in v0.5; bus_alias
definitions and bus-notation labels are now counted in Coverage
(bus_alias_definitions / bus_notation_labels) so a future board
that does use buses is at least visible. See the v06-ws3
INTEGRATION.md for the full reasoning.

Abstention is the default failure mode, mirroring the geometry
layer: no connectivity constructs at all (or no pin definitions
anywhere in the file) abstains silently; budgets, unresolved
library definitions, skipped/unplaced PCB pads and bus-connected
pins are counted in Coverage, and the engine turns a budget
breach, a zero-resolved real schematic, or unmeasurable parity
refs into SCAN_INCOMPLETE.
"""

import math
from dataclasses import dataclass, field

from . import checks as _checks_mod  # for _sch_parts semantics
from .finding import BLOCK, CAUTION, NOTE, Finding

# Budgets (corpus scale is a few thousand pins; caps sit ~50x up).
MAX_PINS = 200000
MAX_WIRE_SEGMENTS = 100000
MAX_LABELS = 100000
MAX_NODES = 300000
MAX_SEG_PAIR_CHECKS = 2000000
MAX_POINT_SEG_CHECKS = 2000000

DRIVER_TYPES = {"power_out", "output", "bidirectional", "tri_state",
                "open_collector", "open_emitter"}
# KiCad's power-pin ERC test is stricter than the signal test: a net
# carrying power_in pins counts as driven ONLY by a power_out pin
# (a regulator pin typed power_out, or a PWR_FLAG, whose pin is
# power_out).  Ordinary output / bidirectional / passive pins do not
# satisfy it -- that is the documented "Input Power pin not driven by
# any Output Power pins" error (Schematic Editor manual, ERC
# section).  Reconciled against the docs 2026-11-10; see
# kicad-preflight-review/research-netlist-semantics.md.
POWER_DRIVER_TYPES = {"power_out"}
EPS = 0.0005                 # mm; point-on-segment tolerance


def _key(x, y):
    # Legacy rounded bucket -- retained only as a cell helper
    # fallback name; point coincidence no longer uses it (audit
    # F5: a single round(...,3) bucket both false-merges points
    # 0.0008 mm apart and false-splits points 0.00002 mm apart
    # across a bucket boundary).  See _PointGrid.
    return (round(x, 3), round(y, 3))


class _PointGrid:
    """EPS-distance point index (audit F5).

    Points are bucketed on a 0.001 mm floor grid; a query scans
    the 3x3 neighbourhood and yields only stored points whose
    actual Euclidean distance is <= EPS, so coincidence is a
    distance test, not bucket membership.  Points at absurd
    magnitudes (where float spacing already dwarfs EPS) are kept
    on a side list and compared by distance directly."""

    _SCALE = 1000.0          # 1 / 0.001 mm cell

    def __init__(self):
        self._cells = {}
        self._large = []
        self._exact = set()

    def _cell(self, x, y):
        if abs(x) >= 1e12 or abs(y) >= 1e12:
            return None
        return (math.floor(x * self._SCALE),
                math.floor(y * self._SCALE))

    def add(self, x, y, payload):
        # Only the first payload at an exact coordinate is stored:
        # callers union a new point with the representative of each
        # distinct nearby coordinate, and every later point at a
        # coordinate was itself unioned with that representative
        # when it was added, so connectivity is preserved while a
        # mass of coincident points (5k labels at one point) stays
        # linear instead of quadratic.
        if (x, y) in self._exact:
            return
        self._exact.add((x, y))
        cell = self._cell(x, y)
        if cell is None:
            self._large.append((x, y, payload))
        else:
            self._cells.setdefault(cell, []).append((x, y, payload))

    def near(self, x, y):
        """Payloads stored within EPS of (x, y)."""
        found = []
        cell = self._cell(x, y)
        candidates = list(self._large)
        if cell is not None:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    candidates.extend(
                        self._cells.get((cell[0] + dx, cell[1] + dy),
                                        ()))
        for px, py, payload in candidates:
            if math.hypot(px - x, py - y) <= EPS:
                found.append(payload)
        return found


def _pin_sort_key(number):
    text = str(number)
    # Audit F6: a pin number is untrusted text; a >9-digit number
    # is sorted as a string, never fed to int() (Python's int-str
    # limit would kill the whole run with no report).
    if text.isdigit() and len(text) <= 9:
        return (0, int(text), "")
    return (1, 0, text)


def pin_point(sym, pin):
    """Absolute sheet position of a lib pin on a placed symbol."""
    lx, ly = pin.x, pin.y
    if sym.mirror == "x":
        ly = -ly
    elif sym.mirror == "y":
        lx = -lx
    ly = -ly                   # library Y-up -> sheet Y-down
    rot = math.radians(sym.rot or 0.0)
    c, s = math.cos(rot), math.sin(rot)
    return (sym.x + lx * c + ly * s, sym.y - lx * s + ly * c)


class _DSU:
    def __init__(self):
        self.parent = []

    def add(self):
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, a):
        parent = self.parent
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _pt_seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0 or not math.isfinite(length_sq):
        # Audit F14: a segment with non-finite length (1e308-scale
        # coordinates overflow length_sq to inf, making the
        # projection NaN) is matched endpoint-only for that
        # segment; such segments are counted at extraction time
        # (degenerate_segments), never projected through NaN.
        return min(math.hypot(px - x1, py - y1),
                   math.hypot(px - x2, py - y2))
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_degenerate(seg):
    dx, dy = seg[2] - seg[0], seg[3] - seg[1]
    return not math.isfinite(dx * dx + dy * dy)


def _on_seg(px, py, seg):
    return _pt_seg_dist(px, py, seg[0], seg[1], seg[2], seg[3]) <= EPS


def _proper_crossing(a, b):
    """Intersection point of two segments when they cross strictly
    in both interiors, else None."""
    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = orient(b[0], b[1], b[2], b[3], a[0], a[1])
    d2 = orient(b[0], b[1], b[2], b[3], a[2], a[3])
    d3 = orient(a[0], a[1], a[2], a[3], b[0], b[1])
    d4 = orient(a[0], a[1], a[2], a[3], b[2], b[3])
    if not ((d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)
            and d1 and d2 and d3 and d4):
        return None
    den = (a[0] - a[2]) * (b[1] - b[3]) - (a[1] - a[3]) * (b[0] - b[2])
    if den == 0:
        return None
    t = ((a[0] - b[0]) * (b[1] - b[3])
         - (a[1] - b[1]) * (b[0] - b[2])) / den
    return (a[0] + t * (a[2] - a[0]), a[1] + t * (a[3] - a[1]))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PinMember:
    ref: str = ""
    number: str = ""
    type: str = ""
    name: str = ""
    sheet: str = ""
    point: tuple = ()
    is_power_symbol: bool = False
    hidden: bool = False


@dataclass
class Net:
    name: str = None             # chosen display name (label/power), or None
    local_name: str = None       # the label/power name before sheet prefixing
    canonical_name: str = None   # sheet-qualified for sub-sheet local nets
    name_source: str = None      # power/global/hierarchical/label/synthetic
    members: list = field(default_factory=list)      # PinMember
    sheetpin_types: list = field(default_factory=list)
    wire_count: int = 0
    label_count: int = 0
    bus_excluded: bool = False

    @property
    def is_power(self):
        return self.name_source == "power" or any(
            m.type in ("power_in", "power_out") for m in self.members)

    @property
    def display_name(self):
        return self.canonical_name or self.name or "unnamed net"

    def synthetic_name(self):
        if self.members:
            first = sorted(self.members,
                           key=lambda m: (m.ref,
                                          _pin_sort_key(m.number)))[0]
            return f"Net-({first.ref}-{first.number})"
        return "Net-(wire)"


@dataclass
class NetlistResult:
    nets: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class _Comp:
    """One connected component within one sheet (pre-global-merge)."""

    __slots__ = ("sheet", "pins", "labels", "sheetpins", "wires",
                 "bus_touched")

    def __init__(self, sheet):
        self.sheet = sheet
        self.pins = []           # PinMember
        self.labels = []         # LabelRec
        self.sheetpins = []      # (block_index, SheetPinRec)
        self.wires = 0
        self.bus_touched = False


def extract_netlist(project):
    """Build the netlist. Returns a NetlistResult (nets may be
    empty when the layer abstains; stats say why)."""
    stats = {"ran": False}
    result = NetlistResult(nets=[], stats=stats)
    if project.sch_path is None or project.sch_failed:
        stats["abstained"] = "no schematic"
        return result
    sheets = list(project.sheet_data)
    total_constructs = sum(len(sd.wires) + len(sd.labels)
                           + len(sd.junctions) for sd in sheets)
    total_lib_pins = sum(
        len(pins) for sd in sheets for libdef in sd.lib_symbols.values()
        for pins in libdef.pins_by_unit.values())
    # Audit F7/F8 honesty counters, aggregated from the parser and
    # reported in Coverage whatever the layer then does.
    stats.update({
        "sheets": len(sheets),
        "wires": sum(len(sd.wires) for sd in sheets),
        "labels": sum(len(sd.labels) for sd in sheets),
        "junctions": sum(len(sd.junctions) for sd in sheets),
        "lib_pin_definitions": total_lib_pins,
        "lib_defs_duplicate": sum(sd.lib_defs_duplicate
                                  for sd in sheets),
        "pins_duplicate": sum(sd.pins_duplicate for sd in sheets),
        "coords_unreadable": sum(sd.coords_unreadable
                                 for sd in sheets),
        "coords_unreadable_pins": sum(sd.coords_unreadable_pins
                                      for sd in sheets),
        "coords_unreadable_labels": sum(sd.coords_unreadable_labels
                                        for sd in sheets),
        "coords_unreadable_wire_points": sum(
            sd.coords_unreadable_wire_points for sd in sheets),
        "coords_unreadable_other": sum(sd.coords_unreadable_other
                                       for sd in sheets),
        "bus_segments": sum(
            sum(len(chain) - 1 for chain in sd.buses
                if len(chain) >= 2) + len(sd.bus_entries)
            for sd in sheets),
        # v0.6 WS3: bus definitions present in the files, counted
        # only (not expanded — see the module docstring).
        "bus_alias_definitions": sum(
            len(getattr(sd, "bus_aliases", {})) for sd in sheets),
        "bus_notation_labels": sum(
            1 for sd in sheets for lab in sd.labels
            if "{" in lab.name or ".." in lab.name),
    })
    # RT-44 / audit F4: a sheet FILE instantiated by 2+ sheet
    # blocks is two KiCad sheet instances with separate local
    # nets, but the loader resolves per file.  The layer abstains
    # wholesale (no ERC/parity findings) rather than merging the
    # instances into phantom shorts -- the same honesty pattern
    # as a budget breach, named by the engine's SCAN_INCOMPLETE.
    from collections import Counter as _Counter
    _block_files = _Counter()
    for sd in sheets:
        for block in sd.sheets:
            if block.file:
                _block_files[block.file] += 1
    multi = [{"file": f, "count": c}
             for f, c in sorted(_block_files.items()) if c >= 2]
    if multi:
        stats["sheets_multi_instanced"] = multi
    else:
        stats["sheets_multi_instanced"] = []
    if total_constructs == 0:
        stats["abstained"] = "no connectivity constructs in schematic"
        return result
    if multi:
        stats["abstained"] = (
            "sheet file(s) instantiated multiple times: "
            + ", ".join(f"{m['file']} x{m['count']}" for m in multi))
        return result
    if total_lib_pins == 0:
        # A parts-only file whose library stubs carry no pin
        # definitions at all (the samples/ fixtures are this shape):
        # there is nothing to extract, by the file's own design.
        # Audit F10: symbols whose instances CLAIM pins (a (pin
        # "N") list) against a zero-pin definition are still
        # counted as claimed-but-unresolvable here, so the honesty
        # of the coverage does not depend on unrelated content.
        _claimed = 0
        _claimed_refs = set()
        _lib_by_file = {sd.file: sd.lib_symbols for sd in sheets}
        for _sym in project.symbols:
            _ld = _lib_by_file.get(_sym.sheet, {}).get(_sym.lib_id)
            if _ld is not None and not _ld.pins_by_unit \
                    and _sym.pin_numbers:
                _claimed += len(_sym.pin_numbers)
                _claimed_refs.add(_sym.ref)
        if _claimed:
            stats.update({
                "pins_total": _claimed,
                "pins_resolved": 0,
                "pins_unresolved": _claimed,
                "sch_unresolved_refs": sorted(_claimed_refs),
            })
        stats["abstained"] = "no pin definitions in schematic"
        return result

    symbols_by_sheet = {}
    for sym in project.symbols:
        symbols_by_sheet.setdefault(sym.sheet, []).append(sym)

    pins_total = pins_resolved = pins_unresolved = 0
    wire_segments_total = 0
    bus_segments_total = 0
    label_total = 0
    node_total = 0
    seg_pair_checks = 0
    point_seg_checks = 0
    bus_touch_checks = 0
    degenerate_segments = 0
    budget_exceeded = False
    comps = []                   # _Comp, across all sheets
    no_pin_refs = set()          # refs whose lib def has no pins at all
    sheetpin_comp = {}           # (sheet, block_idx, pin_name) -> comp idx
    hier_label_comps = {}        # (sheet, name) -> [comp idx]
    nc_points_by_sheet = {}
    dangling_labels = []         # (sheet, LabelRec)
    sch_unresolved_refs = set()

    for sd in sheets:
        if budget_exceeded:
            break
        lib = sd.lib_symbols
        dsu = _DSU()
        kinds = []               # ("pin", PinMember) / ("wire", seg) /
                                 # ("label", LabelRec) / ("sheetpin", rec)
        def _add(kind, obj):
            kinds.append((kind, obj))
            return dsu.add()

        # --- pins ---------------------------------------------------
        pin_nodes = []
        detached_nodes = set()   # hidden power_in pins (RT-13 fix)
        for sym in symbols_by_sheet.get(sd.file, ()):
            libdef = lib.get(sym.lib_id)
            if libdef is not None and not libdef.pins_by_unit \
                    and not sym.pin_numbers:
                # The definition carries no pins at all (graphics-
                # only symbols, e.g. Mechanical:MountingHole as
                # shipped in the piantor schematic): there is
                # nothing to resolve, and the ref is not blamed.
                no_pin_refs.add(sym.ref)
                continue
            pins = []
            if libdef is not None and sym.x is not None:
                pins = libdef.pins_for_unit(sym.unit)
            if not pins:
                count = len(sym.pin_numbers) or 1
                pins_unresolved += count
                pins_total += count
                sch_unresolved_refs.add(sym.ref)
                continue
            # RT-09 fix: power-symbol status follows FILE evidence.
            # When an embedded lib def exists, its (power) flag
            # alone decides; the lib_id "power:" prefix / "#" ref
            # heuristics apply only when no def is embedded.
            if libdef is not None:
                is_power_symbol = bool(libdef.is_power)
            else:
                is_power_symbol = bool(
                    sym.lib_id.startswith("power:")
                    or sym.ref.startswith("#"))
            for pin in pins:
                pins_total += 1
                if getattr(pin, "bad", ""):
                    # Audit F8/F9: numberless or coord-unreadable
                    # pins are unresolved -- counted, excluded from
                    # membership and naming, never a resolved pin
                    # numbered "" and never placed at a guess.
                    pins_unresolved += 1
                    sch_unresolved_refs.add(sym.ref)
                    continue
                pins_resolved += 1
                point = pin_point(sym, pin)
                member = PinMember(
                    ref=sym.ref, number=pin.number, type=pin.type,
                    name=pin.name, sheet=sd.file, point=point,
                    is_power_symbol=is_power_symbol, hidden=pin.hidden)
                node = _add("pin", member)
                pin_nodes.append(node)
                if pin.hidden and pin.type == "power_in" \
                        and not is_power_symbol:
                    # RT-13 fix: a hidden power_in pin connects to
                    # its named net BY NAME as a singleton -- it
                    # must not union the geometric component it
                    # happens to sit on.  Detach it from every
                    # geometric union below; the global merge
                    # enrols only this pin in its name group.
                    detached_nodes.add(node)
        # --- wires ---------------------------------------------------
        segs = []
        for chain in sd.wires:
            for a, b in zip(chain, chain[1:]):
                segs.append((a[0], a[1], b[0], b[1]))
        wire_segments_total += len(segs)
        degenerate_segments += sum(1 for s in segs
                                   if _seg_degenerate(s))
        seg_nodes = [_add("wire", seg) for seg in segs]
        # Bus segments are built here (not in the bus-touch phase)
        # so they count toward the segment budget (audit F1).
        bus_segs = []
        for chain in sd.buses:
            for a, b in zip(chain, chain[1:]):
                bus_segs.append((a[0], a[1], b[0], b[1]))
        bus_segs.extend((a[0], a[1], b[0], b[1])
                        for a, b in sd.bus_entries)
        bus_segments_total += len(bus_segs)
        degenerate_segments += sum(1 for s in bus_segs
                                   if _seg_degenerate(s))
        # --- labels / sheet pins -------------------------------------
        label_nodes = []
        for lab in sd.labels:
            label_nodes.append(_add("label", lab))
            label_total += 1
        sheetpin_nodes = []
        for block_idx, block in enumerate(sd.sheets):
            for spin in block.pins:
                node = _add("sheetpin", (block_idx, spin))
                sheetpin_nodes.append(node)
        node_total += len(kinds)
        if (pins_total > MAX_PINS
                or wire_segments_total + bus_segments_total
                > MAX_WIRE_SEGMENTS or label_total > MAX_LABELS
                or node_total > MAX_NODES):
            budget_exceeded = True
            break

        junction_grid = _PointGrid()
        for _jx, _jy in sd.junctions:
            junction_grid.add(_jx, _jy, True)
        nc_points_by_sheet[sd.file] = list(sd.no_connect_pts)
        # --- union by coincident points (EPS distance, audit F5) --
        point_grid = _PointGrid()

        def _register(x, y, node):
            for other in point_grid.near(x, y):
                dsu.union(node, other)
            point_grid.add(x, y, node)

        for node in pin_nodes:
            if node in detached_nodes:
                continue
            member = kinds[node][1]
            _register(member.point[0], member.point[1], node)
        for node, seg in zip(seg_nodes, segs):
            _register(seg[0], seg[1], node)
            _register(seg[2], seg[3], node)
        for node in label_nodes:
            lab = kinds[node][1]
            _register(lab.x, lab.y, node)
        for node in sheetpin_nodes:
            spin = kinds[node][1][1]
            _register(spin.x, spin.y, node)
        # --- point-on-segment -----------------------------------------
        # Detached hidden power pins take no geometric union at all.
        point_nodes = [n for n in
                       pin_nodes + label_nodes + sheetpin_nodes
                       if n not in detached_nodes]
        point_xy = []
        for node in point_nodes:
            kind, obj = kinds[node]
            if kind == "pin":
                point_xy.append(obj.point)
            elif kind == "label":
                point_xy.append((obj.x, obj.y))
            else:
                point_xy.append((obj[1].x, obj[1].y))
        for node, (px, py) in zip(point_nodes, point_xy):
            for seg_node, seg in zip(seg_nodes, segs):
                point_seg_checks += 1
                if point_seg_checks > MAX_POINT_SEG_CHECKS:
                    budget_exceeded = True
                    break
                if _on_seg(px, py, seg):
                    dsu.union(node, seg_node)
            if budget_exceeded:
                break
        if budget_exceeded:
            break
        # --- wire-wire: endpoint-on-segment + junctioned crossings ----
        for i, seg_a in enumerate(segs):
            for j in range(i + 1, len(segs)):
                seg_pair_checks += 1
                if seg_pair_checks > MAX_SEG_PAIR_CHECKS:
                    budget_exceeded = True
                    break
                seg_b = segs[j]
                # An endpoint of either segment lying on the other
                # segment joins them (shared endpoints included —
                # those are already unioned via the point map, and
                # re-unioning is harmless). Interior crossings join
                # ONLY via a junction at the crossing point.
                joined = any(
                    _on_seg(px, py, seg_b)
                    for px, py in ((seg_a[0], seg_a[1]),
                                   (seg_a[2], seg_a[3]))) or any(
                    _on_seg(px, py, seg_a)
                    for px, py in ((seg_b[0], seg_b[1]),
                                   (seg_b[2], seg_b[3])))
                if not joined:
                    cross = _proper_crossing(seg_a, seg_b)
                    if cross is not None \
                            and junction_grid.near(*cross):
                        joined = True
                if joined:
                    dsu.union(seg_nodes[i], seg_nodes[j])
            if budget_exceeded:
                break
        if budget_exceeded:
            break
        # --- bus touch (budgeted, audit F1) ---------------------------
        # Every bus-touch point test charges the same work budget
        # as a point-on-wire test; a breach abstains honestly
        # instead of running an unbudgeted O(points x bus_segs).
        bus_touch_nodes = set()
        if bus_segs and not budget_exceeded:
            # Bus-touch is a pure function of the point: results
            # are cached per exact point, so 5,000 labels at one
            # point cost one scan of the bus segments, not 5,000
            # (audit F1).  Only tests actually performed charge
            # the budget; a cached answer performs none.
            _bus_cache = {}

            def _bus_touch(px, py):
                nonlocal bus_touch_checks, point_seg_checks, \
                    budget_exceeded
                if (px, py) in _bus_cache:
                    return _bus_cache[(px, py)]
                touched = False
                for bs in bus_segs:
                    bus_touch_checks += 1
                    point_seg_checks += 1
                    if point_seg_checks > MAX_POINT_SEG_CHECKS:
                        budget_exceeded = True
                        return False
                    if _on_seg(px, py, bs):
                        touched = True
                        break
                _bus_cache[(px, py)] = touched
                return touched

            for node, (px, py) in zip(point_nodes, point_xy):
                if _bus_touch(px, py):
                    bus_touch_nodes.add(node)
                if budget_exceeded:
                    break
            if not budget_exceeded:
                for seg_node, seg in zip(seg_nodes, segs):
                    if _bus_touch(seg[0], seg[1]) \
                            or _bus_touch(seg[2], seg[3]):
                        bus_touch_nodes.add(seg_node)
                    if budget_exceeded:
                        break
        if budget_exceeded:
            break
        # --- components --------------------------------------------------
        by_root = {}
        for node in range(len(kinds)):
            by_root.setdefault(dsu.find(node), []).append(node)
        for root_nodes in by_root.values():
            comp = _Comp(sd.file)
            comp.bus_touched = any(n in bus_touch_nodes
                                   for n in root_nodes)
            for node in root_nodes:
                kind, obj = kinds[node]
                if kind == "pin":
                    comp.pins.append(obj)
                elif kind == "wire":
                    comp.wires += 1
                elif kind == "label":
                    comp.labels.append(obj)
                elif kind == "sheetpin":
                    comp.sheetpins.append(obj)
            comp_idx = len(comps)
            comps.append(comp)
            for block_idx, spin in comp.sheetpins:
                sheetpin_comp[(sd.file, block_idx, spin.name)] = comp_idx
            for lab in comp.labels:
                if lab.kind == "hierarchical_label":
                    hier_label_comps.setdefault(
                        (sd.file, lab.name), []).append(comp_idx)
            if comp.labels and not comp.pins and comp.wires == 0 \
                    and not comp.sheetpins:
                for lab in comp.labels:
                    dangling_labels.append((sd.file, lab))

    stats.update({
        "pins_total": pins_total,
        "pins_resolved": pins_resolved,
        "pins_unresolved": pins_unresolved,
        "wire_segments": wire_segments_total,
        "bus_segments_total": bus_segments_total,
        "seg_pair_checks": seg_pair_checks,
        "point_seg_checks": point_seg_checks,
        "bus_touch_checks": bus_touch_checks,
        "degenerate_segments": degenerate_segments,
        "dangling_labels": len(dangling_labels),
        "sch_unresolved_refs": sorted(sch_unresolved_refs),
        "no_pin_refs": sorted(no_pin_refs),
    })
    if budget_exceeded:
        stats["budget_exceeded"] = True
        return result
    if pins_resolved == 0:
        stats["zero_pins_resolved"] = True
        return result
    stats["ran"] = True

    # --- global merge over components --------------------------------
    gdsu = _DSU()
    for _ in comps:
        gdsu.add()
    sym_by_ref_sheet = {}
    for sym in project.symbols:
        sym_by_ref_sheet.setdefault((sym.sheet, sym.ref), sym)

    def _power_names(comp):
        """ALL distinct power-symbol names on a component.

        RT-48b fix: a wire carrying two power symbols of different
        names shorts those two global nets in KiCad -- grouping
        only the first name hid the short.  Names are EXACT (audit
        F11): KiCad net names are case- and whitespace-sensitive,
        so the symbol Value is not stripped here, unlike labels
        which were already exact."""
        names = []
        for member in comp.pins:
            if not member.is_power_symbol:
                continue
            sym = sym_by_ref_sheet.get((member.sheet, member.ref))
            if sym is None:
                continue
            base = sym.lib_id.split(":")[-1]
            if base == "PWR_FLAG":
                continue        # a flag drives a net; it names none
            value = sym.value or ""
            if value and value != "PWR_FLAG" and value not in names:
                names.append(value)
        return names

    def _power_name(comp):
        names = _power_names(comp)
        return names[0] if names else None

    groups = {}
    hidden_power_pins = 0
    for idx, comp in enumerate(comps):
        for power in _power_names(comp):
            groups.setdefault(("G", power), []).append(idx)
        # Documented KiCad semantic: an invisible (hidden) power_in
        # pin on an ordinary symbol is connected globally to the net
        # named by the pin's name, regardless of where it sits
        # (legacy logic-family power pins; KiCad Library Convention
        # S4.3).  Power symbols' own pins are excluded -- they merge
        # via the symbol value above.
        for member in comp.pins:
            if member.hidden and member.type == "power_in" \
                    and not member.is_power_symbol and member.name:
                groups.setdefault(("G", member.name), []).append(idx)
                hidden_power_pins += 1
        for lab in comp.labels:
            if lab.kind == "global_label":
                groups.setdefault(("G", lab.name), []).append(idx)
                # RT-06, verified against KiCad's documentation
                # (see research addendum): within one sheet, labels
                # of the same name connect regardless of type, and
                # a global label additionally exports the name.  So
                # a global label also enrols in its sheet's unified
                # per-sheet namespace.
                groups.setdefault(("L", comp.sheet, lab.name),
                                  []).append(idx)
            elif lab.kind in ("label", "hierarchical_label"):
                # RT-07 fix: local and hierarchical labels share
                # ONE per-sheet namespace in KiCad (a hierarchical
                # label is a sheet-local label that additionally
                # binds to the parent sheet pin, below).
                groups.setdefault(("L", comp.sheet, lab.name),
                                  []).append(idx)
    for idxs in groups.values():
        for other in idxs[1:]:
            gdsu.union(idxs[0], other)
    # Hierarchical binding: sub-sheet hier label <-> parent sheet pin.
    sheet_name_by_file = {}
    for sd in sheets:
        for block_idx, block in enumerate(sd.sheets):
            if block.name and block.file:
                sheet_name_by_file.setdefault(block.file, block.name)
            for spin in block.pins:
                parent_idx = sheetpin_comp.get((sd.file, block_idx,
                                                spin.name))
                if parent_idx is None:
                    continue
                for sub_idx in hier_label_comps.get((block.file,
                                                     spin.name), ()):
                    gdsu.union(parent_idx, sub_idx)

    root_file = project.sch_path.name if project.sch_path else ""
    nets = []
    by_root = {}
    for idx, comp in enumerate(comps):
        by_root.setdefault(gdsu.find(idx), []).append(comp)
    pins_bus_connected = 0
    bus_named_nets = 0
    for root_comps in by_root.values():
        net = Net()
        for comp in root_comps:
            net.members.extend(comp.pins)
            net.wire_count += comp.wires
            net.label_count += len(comp.labels)
            for _bi, spin in comp.sheetpins:
                net.sheetpin_types.append(spin.type)
        if not net.members:
            continue
        power_names = sorted({n for c in root_comps
                              for n in _power_names(c)})
        if not power_names:
            # A net formed only by hidden power_in pins takes the
            # pin name as its (global, power) name, as in KiCad.
            power_names = sorted({m.name for c in root_comps
                                  for m in c.pins
                                  if m.hidden
                                  and m.type == "power_in"
                                  and not m.is_power_symbol
                                  and m.name})
        global_names = sorted({lab.name for c in root_comps
                               for lab in c.labels
                               if lab.kind == "global_label"})
        hier_names = sorted({lab.name for c in root_comps
                             for lab in c.labels
                             if lab.kind == "hierarchical_label"})
        local_names = sorted({lab.name for c in root_comps
                              for lab in c.labels
                              if lab.kind == "label"})
        if power_names:
            net.name, net.name_source = power_names[0], "power"
        elif global_names:
            net.name, net.name_source = global_names[0], "global"
        elif hier_names:
            net.name, net.name_source = hier_names[0], "hierarchical"
        elif local_names:
            net.name, net.name_source = local_names[0], "label"
        net.local_name = net.name
        if net.name:
            member_sheets = {c.sheet for c in root_comps}
            if net.name_source in ("power", "global") \
                    or member_sheets == {root_file}:
                net.canonical_name = net.name
            else:
                sheet_file = sorted(member_sheets)[0]
                sheet_name = sheet_name_by_file.get(
                    sheet_file, sheet_file.rsplit(".", 1)[0])
                net.canonical_name = f"/{sheet_name}/{net.name}"
        bus_touched = any(c.bus_touched for c in root_comps)
        if bus_touched and net.name is None:
            net.bus_excluded = True
            pins_bus_connected += len(net.members)
        elif bus_touched:
            bus_named_nets += 1
        net.members.sort(key=lambda m: (m.ref, _pin_sort_key(m.number),
                                        m.sheet))
        nets.append(net)
    nets.sort(key=lambda n: (n.display_name, n.synthetic_name()
                             if n.name is None else ""))
    stats.update({
        "nets_total": len(nets),
        "nets_named": sum(1 for n in nets if n.name),
        "hidden_power_pins_name_connected": hidden_power_pins,
        "pins_bus_connected": pins_bus_connected,
        "bus_named_nets": bus_named_nets,
        "input_only_nets": sum(
            1 for n in nets if not n.bus_excluded
            and any(m.type == "input" for m in n.members)
            and not ({m.type for m in n.members}
                     | set(n.sheetpin_types)) & DRIVER_TYPES),
        "components": len(comps),
        "nc_points_by_sheet": {k: sorted(v)
                               for k, v in nc_points_by_sheet.items()},
        "dangling_label_list": sorted(
            (sheet, lab.name) for sheet, lab in dangling_labels),
    })
    result.nets = nets
    return result


# ---------------------------------------------------------------------------
# ERC-lite
# ---------------------------------------------------------------------------


def _is_singleton(net):
    return len(net.members) == 1 and net.wire_count == 0 \
        and net.label_count == 0 and not net.sheetpin_types


def run_erc_checks(project, result):
    findings = []
    if not result.stats.get("ran"):
        return findings
    sch_name = project.sch_path.name if project.sch_path else "schematic"
    nc_by_sheet = result.stats.get("nc_points_by_sheet", {})
    _nc_grids = {}

    def _is_nc(member):
        # EPS-distance match (audit F5), not a rounded bucket.
        sheet = member.sheet
        if sheet not in _nc_grids:
            grid = _PointGrid()
            for pt in nc_by_sheet.get(sheet, ()):
                grid.add(pt[0], pt[1], True)
            _nc_grids[sheet] = grid
        return bool(_nc_grids[sheet].near(*member.point))

    # --- ERC_UNCONNECTED_PIN (CAUTION), aggregated per ref ------------
    # RT-42 fix: unit-common (_0_1) pins are ONE physical pin per
    # part, drawn once per unit instance.  Connectivity is
    # evaluated per (ref, pin number) across all unit instances --
    # wired (or NC-marked) on any instance = connected (parity
    # already deduplicates membership this way).
    connected_keys = set()
    nc_keys_connected = set()
    for net in result.nets:
        if net.bus_excluded:
            continue
        if not _is_singleton(net):
            for m in net.members:
                connected_keys.add((m.ref, m.number))
    unconnected = {}
    nc_suppressed = 0
    for net in result.nets:
        if net.bus_excluded or not _is_singleton(net):
            continue
        member = net.members[0]
        if member.type == "no_connect":
            continue
        key = (member.ref, member.number)
        if _is_nc(member):
            nc_suppressed += 1
            nc_keys_connected.add(key)
            continue
        if key in connected_keys or key in nc_keys_connected:
            continue
        unconnected.setdefault(member.ref, []).append(member)
    # A pin NC-marked on one instance and unwired on another is
    # connected (above); likewise drop any candidate whose number
    # was NC-suppressed for the same ref.
    for ref in list(unconnected):
        kept = [m for m in unconnected[ref]
                if (m.ref, m.number) not in nc_keys_connected
                and (m.ref, m.number) not in connected_keys]
        if kept:
            unconnected[ref] = kept
        else:
            del unconnected[ref]
    for ref in sorted(unconnected):
        members = sorted(unconnected[ref],
                         key=lambda m: _pin_sort_key(m.number))
        pins = ", ".join(m.number for m in members)
        findings.append(Finding(
            "ERC_UNCONNECTED_PIN", CAUTION,
            f"{ref} has {len(members)} pin(s) connected to no net "
            f"and carrying no no-connect marker (pin(s) {pins}). "
            "In KiCad ERC these are 'pin not connected' errors on "
            "the schematic as drawn — connect them, or mark them "
            "with a no-connect flag if leaving them open is "
            "intentional.",
            where=f"{sch_name} · {ref}",
            evidence=f"pins={pins}"))
    result.stats["nc_suppressed"] = nc_suppressed
    result.stats["unconnected_pins"] = sum(
        len(v) for v in unconnected.values())

    # --- ERC_POWER_NOT_DRIVEN (CAUTION), one per net ------------------
    for net in result.nets:
        if net.bus_excluded or _is_singleton(net):
            continue
        types = {m.type for m in net.members} | set(net.sheetpin_types)
        if "power_in" not in types or types & POWER_DRIVER_TYPES:
            continue
        examples = sorted({f"{m.ref}.{m.number}" for m in net.members
                           if m.type == "power_in"})[:6]
        findings.append(Finding(
            "ERC_POWER_NOT_DRIVEN", CAUTION,
            f"Net '{net.display_name}' carries power-input pin(s) "
            f"({', '.join(examples)}) but no power-output pin "
            "drives it. KiCad's power-pin ERC test accepts only a "
            "power_out pin as a driver (a regulator pin typed "
            "power_out, or a PWR_FLAG) — ordinary output, "
            "bidirectional and passive pins do not count, which is "
            "exactly the gap a PWR_FLAG fills. Add a PWR_FLAG where "
            "the net is fed, or check the regulator/connector "
            "wiring.",
            where=f"{sch_name} · {net.display_name}",
            evidence=f"power_in={len([m for m in net.members if m.type == 'power_in'])} power_out=0"))
    # --- ERC_OUTPUT_CONFLICT (BLOCK — KiCad ERC error) -----------------
    for net in result.nets:
        if net.bus_excluded:
            continue
        outs = [m for m in net.members if m.type == "output"]
        pwr_outs = [m for m in net.members if m.type == "power_out"]
        sheet_outs = [t for t in net.sheetpin_types if t == "output"]
        sheet_pwr = [t for t in net.sheetpin_types if t == "power_out"]
        n_out = len(outs) + len(sheet_outs)
        n_pwr = len(pwr_outs) + len(sheet_pwr)
        if n_out >= 2 or (n_out >= 1 and n_pwr >= 1) or n_pwr >= 2:
            pins = sorted({f"{m.ref}.{m.number}"
                           for m in outs + pwr_outs})[:8]
            if n_out == 0 and n_pwr >= 2:
                # FP-tester P2 (message quality): when ALL the
                # conflicting drivers are power_out pins, the
                # overwhelmingly likely cause is duplicate
                # PWR_FLAGs -- say so instead of the generic
                # outputs-shorted wording.  Severity stays BLOCK
                # (documented KiCad matrix).
                message = (
                    f"Net '{net.display_name}' has {n_pwr} "
                    f"power-output pins driving it together "
                    f"({', '.join(pins)}). The overwhelmingly likely "
                    "cause is duplicate PWR_FLAGs: use one "
                    "PWR_FLAG per net; remove the extra flag(s). "
                    "KiCad's documented pin-conflict matrix rates "
                    "power-output/power-output as an error.")
            else:
                message = (
                    f"Net '{net.display_name}' ties output pins "
                    f"together ({', '.join(pins)}): {n_out} output "
                    f"and {n_pwr} power-output pin(s) drive the same "
                    "net. KiCad's documented pin-conflict matrix rates "
                    "output/output, output/power-output and "
                    "power-output/power-output as errors — two drivers "
                    "shorted together fight, overheat or latch up "
                    "(a second PWR_FLAG on an already-driven rail "
                    "trips the power-output case).")
            findings.append(Finding(
                "ERC_OUTPUT_CONFLICT", BLOCK, message,
                where=f"{sch_name} · {net.display_name}",
                evidence=f"outputs={n_out} power_outputs={n_pwr}"))
    # --- ERC_PIN_CONFLICT (BLOCK — KiCad ERC error) --------------------
    # v0.6 WS3: the remaining ERROR cells of KiCad's documented
    # pin-conflict matrix that the files' pin types support. The
    # matrix used is the default ERC pin_map KiCad itself stores
    # in every corpus project's .kicad_pro (erc.pin_map, values
    # 0=OK / 1=warning / 2=error; type order input, output,
    # bidirectional, tri_state, passive, unspecified, power_in,
    # power_out, open_collector, open_emitter, no_connect, free),
    # consistent with the Schematic Editor manual's matrix as
    # reconciled in research-netlist-semantics.md. Error pairs
    # NOT already covered by ERC_OUTPUT_CONFLICT above:
    #   output+open_collector, output+open_emitter,
    #   output+no_connect, tri_state+open_collector,
    #   open_collector+open_collector,
    #   open_collector+open_emitter, open_collector+no_connect.
    # Deliberately NOT shipped: every WARNING cell (value 1).
    # Measured on the corpus, warning cells are unusable as a
    # rule — power_in paired with almost any other type is a
    # warning cell, so a warning rule would fire on essentially
    # every power net of every healthy board (the same evidence
    # bar that kept ERC_INPUT_UNDRIVEN unshipped). Also not
    # shipped: pairs involving the `free` type (matrix rates it
    # error against everything, but a free pin is explicitly
    # "no ERC" by definition in KiCad's type list) and
    # power_out+tri_state (a warning cell, not an error).
    # Corpus measurement before shipping (2026-11-10): zero nets
    # on any of the 4 corpus boards — or the netlist samples —
    # carry any of these pairs (open_collector / open_emitter /
    # tri_state pins do not occur in the corpus at all), so the
    # rule is corpus-silent by construction and fixture-verified
    # in v06-ws3/check_ws3.py.
    _CONFLICT_PAIRS = [
        ("output", "open_collector"), ("output", "open_emitter"),
        ("output", "no_connect"), ("tri_state", "open_collector"),
        ("open_collector", "open_collector"),
        ("open_collector", "open_emitter"),
        ("open_collector", "no_connect"),
    ]
    for net in result.nets:
        if net.bus_excluded:
            continue
        from collections import Counter as _Ctr2
        counts = _Ctr2([m.type for m in net.members]
                       + list(net.sheetpin_types))
        hit = None
        for type_a, type_b in _CONFLICT_PAIRS:
            if type_a == type_b:
                if counts.get(type_a, 0) >= 2:
                    hit = (type_a, type_b)
                    break
            elif counts.get(type_a) and counts.get(type_b):
                hit = (type_a, type_b)
                break
        if hit is None:
            continue
        type_a, type_b = hit
        pins = sorted({f"{m.ref}.{m.number}" for m in net.members
                       if m.type in (type_a, type_b)})[:8]
        findings.append(Finding(
            "ERC_PIN_CONFLICT", BLOCK,
            f"Net '{net.display_name}' connects pin type(s) "
            f"{type_a} and {type_b} together "
            f"({', '.join(pins)}). KiCad's documented pin-conflict "
            "matrix (the default ERC pin_map stored in the "
            "project's .kicad_pro) rates this pairing as an error "
            "— the drivers fight, or a pin marked not-connected / "
            "open-collector is driven by a push-pull output. "
            "Check the symbols on this net; one of them has the "
            "wrong electrical type or does not belong here.",
            where=f"{sch_name} · {net.display_name}",
            evidence=f"pair={type_a}+{type_b} "
                     f"count_{type_a}={counts.get(type_a, 0)} "
                     f"count_{type_b}={counts.get(type_b, 0)}"))
    # --- ERC_DANGLING_LABEL (NOTE) --------------------------------------
    for sheet, name in result.stats.get("dangling_label_list", []):
        findings.append(Finding(
            "ERC_DANGLING_LABEL", NOTE,
            f"Label '{name}' in {sheet} touches no wire and no "
            "pin — it names nothing. KiCad ERC warns on these; "
            "usually a moved wire left the label behind.",
            where=f"{sheet} · {name}"))
    return findings


# ---------------------------------------------------------------------------
# Schematic <-> PCB parity
# ---------------------------------------------------------------------------


def _local(pcb_or_canonical):
    return pcb_or_canonical.rsplit("/", 1)[-1]


def run_parity_checks(project, result):
    findings = []
    stats = result.stats
    if not stats.get("ran") or project.pcb_path is None \
            or project.pcb_failed:
        return findings
    sch_name = project.sch_path.name if project.sch_path else "schematic"
    pcb_name = project.pcb_path.name
    stats["parity_ran"] = True

    sch_parts = _checks_mod._sch_parts(project)
    pcb_part_refs = {fp.ref for fp in project.footprints if fp.is_part}
    geo = project.geometry
    modelled_by_ref = {}
    for pad in geo.pads:
        modelled_by_ref.setdefault(pad.ref, 0)
        modelled_by_ref[pad.ref] += 1
    # Audit F3: NPTH pads in pads_membership_extra also carry
    # measurable membership, so a ref measured only through them
    # is not "no modelled pad" for parity purposes.
    extra_by_ref = {}
    for pad in getattr(geo, "pads_membership_extra", ()):
        if pad.net_name:
            extra_by_ref[pad.ref] = extra_by_ref.get(pad.ref, 0) + 1
    unmeasurable = set()
    # v0.6 WS3: the abstention criterion is now membership-based,
    # not shape-based. project.py records every pad as modelled,
    # membership-only (NPTH; trapezoid; custom with a modelled
    # primitive set), or membership-missing (custom with
    # unmodelled primitives, unplaced, non-positive size).
    # Membership needs only the (ref, pad name, net) the file
    # states, so a ref abstains only when at least one of its
    # pads is membership-missing, or when it has pads but NONE of
    # them contributed any membership record at all.
    _missing_by_ref = getattr(geo, "pad_membership_missing_by_ref",
                              None)
    for ref in pcb_part_refs:
        if _missing_by_ref is not None:
            missing = _missing_by_ref.get(ref, 0)
        else:
            # Project objects built without the v0.6 accounting
            # (older test doubles): fall back to the v0.5 rule.
            missing = geo.pad_issues_by_ref.get(ref, 0)
        if missing > 0 or (geo.pad_total_by_ref.get(ref, 0) > 0
                           and modelled_by_ref.get(ref, 0) == 0
                           and extra_by_ref.get(ref, 0) == 0
                           and sum(1 for pad in
                                   getattr(geo, "pads_membership_extra",
                                           ())
                                   if pad.ref == ref) == 0):
            unmeasurable.add(ref)
    stats["parity_membership_extra_pads"] = len(
        getattr(geo, "pads_membership_extra", ()))
    unresolved_refs = set(stats.get("sch_unresolved_refs", ()))
    no_pin_refs = set(stats.get("no_pin_refs", ()))
    scope_refs = {ref for ref, sym in sch_parts.items()
                  if sym.footprint and sym.footprint.strip()
                  and ref in pcb_part_refs
                  and ref not in unmeasurable
                  and ref not in unresolved_refs
                  and ref not in no_pin_refs}
    stats["parity_scope_refs"] = len(scope_refs)
    stats["parity_unmeasurable_refs"] = sorted(
        ref for ref in (unmeasurable | unresolved_refs)
        if ref in sch_parts and ref in pcb_part_refs
        and ref not in set(stats.get("no_pin_refs", ())))

    # Schematic membership, in scope only. Singleton nets (one
    # pin, no wire, no label) are not routed nets at all — the
    # pin is simply unconnected (KiCad's netlist gives it an
    # "unconnected" pseudo-net that parity ignores; the NC-marked
    # ones are intentional, the rest are ERC_UNCONNECTED_PIN's).
    singleton_pins = 0
    sch_nets = []                # (net, {(ref, pin)})
    for net in result.nets:
        if net.bus_excluded:
            continue
        if _is_singleton(net):
            singleton_pins += len(net.members)
            continue
        membership = {(m.ref, m.number) for m in net.members
                      if not m.is_power_symbol and m.ref in scope_refs}
        if membership:
            sch_nets.append((net, membership))
    # PCB membership, in scope only. KiCad writes an
    # "unconnected-(REF-PadN)" pseudo-net for every pad with no
    # net — those declare the pad unconnected (the counterpart of
    # a singleton schematic pin), they are not board nets.
    pcb_membership = {}
    pseudo_nets = 0
    # Modelled pads plus NPTH membership-extra pads (audit F3):
    # membership needs no copper shape, and the file states it.
    for pad in list(geo.pads) \
            + list(getattr(geo, "pads_membership_extra", ())):
        if pad.net_id is None or pad.net_id == 0 or not pad.net_name:
            continue
        if pad.net_name.startswith("unconnected-"):
            pseudo_nets += 1
            continue
        if pad.ref not in scope_refs:
            continue
        pcb_membership.setdefault(pad.net_name, set()).add(
            (pad.ref, pad.name))
    stats["parity_pcb_unconnected_pseudo_pads"] = pseudo_nets
    pcb_nets = sorted(pcb_membership.items())   # (name, membership)

    # --- Matching (indexed; audit F2) --------------------------------
    # The leftover passes used to scan every remaining PCB net per
    # schematic net (O(S x P), 16.5 s at 6k x 6k).  All probes now
    # go through indexes built once: canonical/local name maps, an
    # exact-membership hash, and a member-keyed inverted index for
    # subset probes.  Examinations are counted; a (generous) work
    # budget remains as a backstop -- a breach abstains from parity
    # wholesale, named by the engine's SCAN_INCOMPLETE.
    MAX_PARITY_CHECKS = 2000000
    work = [0]

    def _tick(n=1):
        work[0] += n
        return work[0] <= MAX_PARITY_CHECKS

    matched_pcb = {}             # pcb name -> sch net
    pairs = []                   # (net, membership, pcb_name, pcb_membership)
    pcb_by_name = dict(pcb_nets)

    def _identity(net):
        # RT-03 fix: scoped identity for blame -- two schematic
        # nets can share one display name (a sheet-local label net
        # vs the global power net of the same name).
        sheets = sorted({m.sheet for m in net.members}) or ["?"]
        return (f"{net.name_source or 'unnamed'} net "
                f"'{net.local_name or net.synthetic_name()}' "
                f"in {', '.join(sheets)}")

    # Canonical-name matching.  When several schematic nets share
    # one canonical name (a sheet-local twin and a global twin,
    # RT-03), pair the PCB net with the twin whose membership
    # overlaps it most, so the messages blame the right net.
    from collections import defaultdict as _dd
    _canon_groups = _dd(list)
    for _entry in sch_nets:
        if _entry[0].canonical_name:
            _canon_groups[_entry[0].canonical_name].append(_entry)
    _paired_ids = set()
    for canon in sorted(_canon_groups):
        if canon not in pcb_by_name or canon in matched_pcb:
            continue
        group = _canon_groups[canon]
        pcb_mem = pcb_by_name[canon]
        if len(group) == 1:
            best = group[0]
        else:
            def _overlap_key(entry, _mem=pcb_mem):
                _tick(len(entry[1]))
                return (len(entry[1] & _mem),
                        -len(entry[1] ^ _mem),
                        entry[0].display_name,
                        entry[0].synthetic_name())
            best = max(group, key=_overlap_key)
        matched_pcb[canon] = best[0]
        pairs.append((best[0], best[1], canon, pcb_mem))
        _paired_ids.add(id(best))
    remaining_sch = [e for e in sch_nets if id(e) not in _paired_ids]
    remaining_pcb = {name: mem for name, mem in pcb_nets
                     if name not in matched_pcb}

    # Unique local-name matching, via a local-name index.
    _local_index = _dd(list)
    for _name in remaining_pcb:
        _local_index[_local(_name)].append(_name)
    for _lst in _local_index.values():
        _lst.sort()
    from collections import Counter as _Ctr
    _sch_local_counts = _Ctr((n.local_name or "")
                             for n, _m in remaining_sch)
    still_sch = []
    for net, membership in remaining_sch:
        candidates = [nm for nm in
                      _local_index.get(net.local_name or "", ())
                      if nm in remaining_pcb]
        _tick(len(candidates))
        if net.local_name and len(candidates) == 1 \
                and _sch_local_counts[net.local_name or ""] == 1:
            name = candidates[0]
            matched_pcb[name] = net
            pairs.append((net, membership, name,
                          remaining_pcb.pop(name)))
        else:
            still_sch.append((net, membership))
    remaining_sch = still_sch

    def _membership_index():
        idx = {}
        for nm, mem in remaining_pcb.items():
            idx.setdefault(frozenset(mem), []).append(nm)
        for lst in idx.values():
            lst.sort()
        return idx

    # Unnamed sch nets: identical membership to a PCB net = the
    # same net under a generated name — matched silently.
    _mem_index = _membership_index()
    still_sch = []
    for net, membership in remaining_sch:
        hit = None
        if net.name is None:
            _tick()
            hits = _mem_index.get(frozenset(membership), [])
            hits = [h for h in hits if h in remaining_pcb]
            if hits:
                hit = hits[0]
        if hit is not None:
            matched_pcb[hit] = net
            del remaining_pcb[hit]
        else:
            still_sch.append((net, membership))
    remaining_sch = still_sch
    # Renames: identical membership, different names, both named.
    renamed_pcb = set()
    _mem_index = _membership_index()
    still_sch = []
    for net, membership in remaining_sch:
        hit = None
        if net.name is not None:
            _tick()
            hits = [h for h in
                    _mem_index.get(frozenset(membership), [])
                    if h in remaining_pcb]
            if hits:
                hit = hits[0]
        if hit is not None:
            renamed_pcb.add(hit)
            del remaining_pcb[hit]
            findings.append(Finding(
                "PARITY_NET_RENAMED", NOTE,
                f"The schematic net '{net.display_name}' and the "
                f"PCB net '{hit}' have identical pin membership "
                f"({len(membership)} pin(s)) under different "
                "names — the board was most likely updated from a "
                "renamed net without a full re-import, or the "
                "schematic net was renamed after layout. Membership "
                "agrees; only the name differs.",
                where=f"{sch_name} · {net.display_name}",
                evidence=f"schematic={net.display_name} pcb={hit} "
                         f"pins={len(membership)}"))
        else:
            still_sch.append((net, membership))
    remaining_sch = still_sch
    # Unnamed-subset pairing (audit F12, as documented in
    # RULES.md): an unnamed schematic net whose membership is a
    # subset of EXACTLY ONE remaining PCB net pairs with that
    # net; the asymmetry then reports as PARITY_NET_MEMBERSHIP
    # naming the PCB net, not as a sch-only BLOCK.  An unnamed
    # net spanning multiple PCB nets keeps PARITY_NET_SCH_ONLY.
    _inverted = _dd(set)
    for _nm, _mem in remaining_pcb.items():
        for _member in _mem:
            _inverted[_member].add(_nm)
    still_sch = []
    for net, membership in remaining_sch:
        paired_name = None
        if net.name is None and membership:
            # Candidate PCB nets containing ALL members = the
            # intersection of the per-member candidate sets,
            # starting from the smallest set.
            cand_sets = []
            for _member in membership:
                _tick()
                cand_sets.append(_inverted.get(_member, set()))
            cand_sets.sort(key=len)
            common = set(cand_sets[0]) if cand_sets else set()
            for _s in cand_sets[1:]:
                _tick(len(_s))
                common &= _s
                if not common:
                    break
            common = {c for c in common if c in remaining_pcb}
            _tick(len(common))
            if len(common) == 1:
                paired_name = next(iter(common))
                # A superset test is still required: the
                # intersection above is exactly that test (every
                # member is in the candidate), but keep the subset
                # assertion explicit for safety.
                if not membership <= remaining_pcb[paired_name]:
                    paired_name = None
        if paired_name is not None:
            matched_pcb[paired_name] = net
            pairs.append((net, membership, paired_name,
                          remaining_pcb.pop(paired_name)))
        else:
            still_sch.append((net, membership))
    remaining_sch = still_sch
    stats["parity_pair_checks"] = work[0]
    if work[0] > MAX_PARITY_CHECKS:
        stats["parity_budget_exceeded"] = True
        stats["parity_nets_compared"] = 0
        stats["parity_sch_only"] = 0
        stats["parity_pcb_only"] = 0
        stats["parity_renamed"] = 0
        stats["parity_singleton_pins_excluded"] = singleton_pins
        return []

    # Membership differences on matched pairs.
    for net, membership, pair_pcb_name, pcb_mem in pairs:
        only_sch = sorted(membership - pcb_mem)
        only_pcb = sorted(pcb_mem - membership)
        if not only_sch and not only_pcb:
            continue
        fmt = lambda items: ", ".join(f"{r}.{p}" for r, p in items[:10])
        findings.append(Finding(
            "PARITY_NET_MEMBERSHIP", BLOCK,
            f"Net '{net.display_name}' (PCB: '{pair_pcb_name}'; "
            f"schematic identity: {_identity(net)}) does "
            f"not connect the same pins on both sides: "
            f"schematic-only: {fmt(only_sch) or '—'}; "
            f"PCB-only: {fmt(only_pcb) or '—'}. The board's copper "
            "for this net will not match the schematic — re-import "
            "the netlist / update the PCB from the schematic.",
            where=f"{sch_name} · {net.display_name}",
            evidence=f"sch_only={len(only_sch)} pcb_only={len(only_pcb)}"))
    for net, membership in remaining_sch:
        findings.append(Finding(
            "PARITY_NET_SCH_ONLY", BLOCK,
            f"Schematic net "
            f"'{net.display_name or net.synthetic_name()}' "
            f"(schematic identity: {_identity(net)}) "
            f"connects {len(membership)} in-scope pin(s) "
            f"({', '.join(f'{r}.{p}' for r, p in sorted(membership)[:6])}) "
            "but no PCB net carries them. The net never reached "
            "the board — update the PCB from the schematic.",
            where=f"{sch_name} · {net.display_name or net.synthetic_name()}",
            evidence=f"pins={len(membership)}"))
    for name, membership in sorted(remaining_pcb.items()):
        findings.append(Finding(
            "PARITY_NET_PCB_ONLY", BLOCK,
            f"PCB net '{name}' connects {len(membership)} "
            f"in-scope pad(s) "
            f"({', '.join(f'{r}.{p}' for r, p in sorted(membership)[:6])}) "
            "but no schematic net carries them. The board has "
            "copper the schematic does not describe — the "
            "schematic is the source of truth.",
            where=f"{pcb_name} · {name}",
            evidence=f"pads={len(membership)}"))
    stats["parity_singleton_pins_excluded"] = singleton_pins
    stats["parity_nets_compared"] = len(pairs)
    stats["parity_sch_only"] = len(remaining_sch)
    stats["parity_pcb_only"] = len(remaining_pcb)
    stats["parity_renamed"] = len(renamed_pcb)
    return findings


def run_netlist_checks(project):
    """The netlist layer: extract, ERC-lite, parity.

    Returns (findings, stats) in the geometry layer's shape."""
    result = extract_netlist(project)
    findings = []
    findings.extend(run_erc_checks(project, result))
    findings.extend(run_parity_checks(project, result))
    # Deterministic report order within the layer.
    order = {"PARITY_NET_MEMBERSHIP": 0, "PARITY_NET_SCH_ONLY": 1,
             "PARITY_NET_PCB_ONLY": 2, "ERC_OUTPUT_CONFLICT": 3,
             "ERC_PIN_CONFLICT": 3,
             "ERC_POWER_NOT_DRIVEN": 4, "ERC_UNCONNECTED_PIN": 5,
             "PARITY_NET_RENAMED": 6, "ERC_DANGLING_LABEL": 7}
    findings.sort(key=lambda f: (order.get(f.rule, 9), f.where,
                                 f.message))
    return findings, result.stats
