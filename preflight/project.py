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

PCB geometry is captured at the property level only — segment widths,
via sizes/drills, pad sizes/drills, zone fill presence, board-outline
presence — the values fab capability checks need. Pins, nets and
connectivity are NOT parsed (see checks.py).

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
    cpl_path: Path = None             # position/CPL CSV, when one was found
    cpl_refs: list = field(default_factory=list)
    cpl_error: str = ""          # position-named file exists but won't parse
    cpl_auto_note: str = ""
    cpl_files: list = field(default_factory=list)  # all CPL files merged
    cpl_broken: list = field(default_factory=list)
    # (name, error) position-like files that failed while another parsed
    no_connects: int = 0
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
        try:
            version = int(str(ver_node[1]).strip())
        except (TypeError, ValueError):
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


def parse_schematic(text, source_name=""):
    """Return (symbols, no_connects, title, sheet_files, skipped_no_ref,
    format_version, generator_version, stamp_raw) from .kicad_sch text.
    Every (kicad_sch ...) root in the file is read; placed symbols are
    the root's direct (symbol ...) children (definitions inside
    (lib_symbols ...) share the shape and are correctly ignored)."""
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
    for root in roots:
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
                try:
                    unit = int(unit_node[1])
                except (TypeError, ValueError):
                    unit = 1
            value = props.get("Value", "")
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
            ))
        no_connects += sum(1 for _ in sexpr.children(root, "no_connect"))
        for sheet in sexpr.children(root, "sheet"):
            sprops = _properties(sheet)
            if sprops.get("Sheetfile"):
                sheet_files.append(sprops["Sheetfile"])
        if not title:
            title_block = sexpr.child(root, "title_block")
            if title_block is not None:
                title_node = sexpr.child(title_block, "title")
                if title_node is not None and len(title_node) >= 2 \
                        and isinstance(title_node[1], str):
                    title = title_node[1]
    return symbols, no_connects, title, sheet_files, skipped_no_ref, \
        format_version, generator, stamp_raw


def _layer_of(node):
    layer = sexpr.child(node, "layer")
    if layer is not None and len(layer) >= 2 and isinstance(layer[1], str):
        return layer[1]
    return ""


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
    if declared and not all(
            any(fnmatch.fnmatchcase(filled, layer)
                for filled in filled_layers)
            for layer in declared):
        # Declared layers may be wildcards ('*.Cu' is what KiCad
        # writes for all-copper zones) — match them as globs.
        return "unfilled"
    return "filled"


def parse_pcb(text):
    """Return (footprints, skipped_no_ref, geometry, format_version,
    generator_version, stamp_raw) from .kicad_pcb text.

    References/values are read from BOTH storage styles: KiCad 7/8
    (property "Reference" ...) and KiCad 6 (fp_text reference ...).
    (attr ...) flags are captured (board_only, exclude_from_bom,
    exclude_from_pos_files). Footprints with no reference at all are
    counted by the caller via the second return value. Geometry
    (segment/arc widths, via/pad drills, annular rings, slots, zone
    fills, outline presence) is collected for the fab-capability
    checks in fabchecks.py."""
    forms = sexpr.parse(text)
    root = next((f for f in forms if sexpr.head(f) == "kicad_pcb"), None)
    if root is None:
        raise SexprError("no (kicad_pcb ...) root form")
    format_version, generator, stamp_raw = _file_versions(root)
    geo = PcbGeometry()
    footprints = []
    skipped_no_ref = 0
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
        for pad in sexpr.children(node, "pad"):
            _pad_geometry(pad, ref, geo)
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
    # canonical case, but the file is untrusted input.
    for node in root[1:]:
        if not isinstance(node, list):
            continue
        node_head = sexpr.head(node)
        layer = _layer_of(node)
        if node_head in ("segment", "arc"):
            # Copper tracks: straight segments and (arc ...) curves.
            if layer.lower().endswith(".cu"):
                width = sexpr.child(node, "width")
                if width is not None and len(width) >= 2:
                    w = _num(width[1])
                    if w is not None:
                        geo.segment_widths.append(w)
                    else:
                        geo.unparsed_dims += 1
            elif layer.lower() == "edge.cuts":
                geo.edge_items += 1
        elif node_head == "via":
            size = sexpr.child(node, "size")
            if size is not None and len(size) >= 2:
                v = _num(size[1])
                if v is not None:
                    geo.via_sizes.append(v)
                else:
                    geo.unparsed_dims += 1
            drill = sexpr.child(node, "drill")
            if drill is not None and len(drill) >= 2:
                v = _drill_diameter(drill)
                if v is not None:
                    geo.via_drills.append(v)
                else:
                    geo.unparsed_dims += 1
        elif node_head == "zone":
            state = _zone_fill_state(node)
            if state is not None:
                geo.zones_total += 1
                if state == "unfilled":
                    geo.zones_unfilled += 1
        elif node_head in ("gr_line", "gr_arc", "gr_circle", "gr_rect",
                           "gr_poly", "gr_curve"):
            if layer.lower() == "edge.cuts":
                geo.edge_items += 1
    return footprints, skipped_no_ref, geo, format_version, generator, \
        stamp_raw


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
        pro = _first(folder, "*.kicad_pro")
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
                fmt_version, generator, stamp_raw = \
                parse_schematic(read_project_file(sch_path),
                                source_name=sch_path.name)
            project.symbols.extend(symbols)
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
             project.pcb_generator, project.pcb_stamp_raw) = parse_pcb(
                read_project_file(project.pcb_path))
        except SexprError as exc:
            project.parse_errors.append((project.pcb_path, str(exc)))

    # --- project name: schematic title > .kicad_pro > file stem --------
    if not name and pro is not None:
        try:
            meta = json.loads(read_project_file(pro))
            filename = ""
            if isinstance(meta, dict):
                inner = meta.get("meta")
                if isinstance(inner, dict):
                    candidate = inner.get("filename", "")
                    if isinstance(candidate, str):
                        filename = candidate
            name = Path(filename).stem if filename else pro.stem
        except (SexprError, ValueError):
            name = pro.stem
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
