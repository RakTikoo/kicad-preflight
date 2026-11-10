"""Load a KiCad project from disk: schematic(s), PCB, and BOM.

Everything is parsed from KiCad's own text formats — .kicad_sch and
.kicad_pcb are S-expressions, the BOM is CSV (see bom.py), .kicad_pro
is JSON (only the project name is read from it). No KiCad installation
is required for this layer; the official DRC/ERC checks live in
kicadcli.py and are optional.

Formats: KiCad 6, 7 and 8 project files. On the PCB side, KiCad 6
stores footprint references as (fp_text reference "R1" ...) while
KiCad 7/8 use (property "Reference" "R1" ...) — both are read.
Hierarchical schematics are followed: (sheet ...) blocks in the root
sheet name sub-sheet files, which are loaded and merged. Legacy
KiCad <=5 .sch files are reported, not silently misread.

Safety: every file is stat-checked before reading — it must be a
regular file (a FIFO or device would block forever) and within the
size budget — and decoded as strict UTF-8 (undecodable bytes become a
parse error, never silently rewritten characters).
"""

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .bom import BomRow, parse_bom  # noqa: F401  (re-exported)
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

    @property
    def is_part(self):
        """A real component position vs board furniture (mounting holes
        marked board-only, silkscreen logos/labels with generated or
        placeholder refs). Membership and duplicate checks only ever
        consider parts."""
        return is_designator(self.ref) and not self.board_only


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


def parse_schematic(text, source_name=""):
    """Return (symbols, no_connects, title, sheet_files, skipped_no_ref)
    from .kicad_sch text. Every (kicad_sch ...) root in the file is
    read; placed symbols are the root's direct (symbol ...) children
    (definitions inside (lib_symbols ...) share the shape and are
    correctly ignored)."""
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
    return symbols, no_connects, title, sheet_files, skipped_no_ref


def parse_pcb(text):
    """Return a list of FootprintInst from .kicad_pcb text.

    References/values are read from BOTH storage styles: KiCad 7/8
    (property "Reference" ...) and KiCad 6 (fp_text reference ...).
    (attr ...) flags are captured (board_only, exclude_from_bom).
    Footprints with no reference at all are counted by the caller via
    the second return value."""
    forms = sexpr.parse(text)
    root = next((f for f in forms if sexpr.head(f) == "kicad_pcb"), None)
    if root is None:
        raise SexprError("no (kicad_pcb ...) root form")
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
            exclude_from_bom="exclude_from_bom" in attr_tokens))
    return footprints, skipped_no_ref


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
            parse_bom(read_project_file(cand))
            return cand, ("auto-detected" if len(ordered) > 1
                          else "found in project folder")
        except (ValueError, SexprError):
            if cand in bom_named and first_bom_named_failure is None:
                first_bom_named_failure = cand
            continue
    if first_bom_named_failure is not None:
        return first_bom_named_failure, "named like a BOM but unparseable"
    return None, ("CSV files present but none parses as a BOM"
                  if ordered else "no CSV found")


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
            symbols, no_connects, title, sheet_files, skipped = \
                parse_schematic(read_project_file(sch_path),
                                source_name=sch_path.name)
            project.symbols.extend(symbols)
            project.no_connects += no_connects
            project.sch_symbols_no_ref += skipped
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
            project.footprints, project.pcb_footprints_no_ref = parse_pcb(
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
    return project
