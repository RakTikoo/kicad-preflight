"""Gerber package checks (v0.4): file-level validation of a fab
output folder — the .zip contents a user is about to upload to
JLCPCB / PCBWay — without parsing any Gerber geometry.

The question answered here is the one KiCad cannot answer: did
the export actually produce everything a standard 2-layer order
needs, under names a fab will recognise? A package is a directory
of files; each file is classified into a layer role by its name,
recognising three naming schemes:

  * KiCad's default export names (``board-F_Cu.gtl`` style layer
    tokens, plus ``board-Edge_Cuts.gm1``, ``board.drl`` and the
    ``-job.gbrjob`` sidecar);
  * Protel-style extensions (.gtl/.gbl/.gts/.gbs/.gto/.gbo/.gtp/
    .gbp/.gko/.gm1, inner copper .g1/.g2/..., legacy all-in-one
    .cmp/.sol/.stc/.sts/.crc/.crs/.smt/.smb);
  * generic naming (``top.gbr``, ``bottom_copper.gbr``, ``*.drl``,
    ``*.xln``, NC drill in .txt form, Excellon files identified by
    their M48 header when the name says nothing).

Known Gerber/drill extensions whose role cannot be determined are
reported as unrecognised (NOTE) rather than guessed. Every check
here is about presence and consistency of files; the contents of
a Gerber file are never parsed.
"""

import re
from pathlib import Path

from .finding import BLOCK, CAUTION, NOTE, Finding

GERBER_EXTS = {
    ".gbr", ".ger", ".gbl", ".gtl", ".gbs", ".gts", ".gbo", ".gto",
    ".gbp", ".gtp", ".gko", ".gm1", ".gm3", ".g1", ".g2", ".g3",
    ".g4", ".g5", ".g6", ".g7", ".g8", ".g9", ".g10", ".g11",
    ".g12", ".cmp", ".sol", ".stc", ".sts", ".crc", ".crs",
    ".smt", ".smb", ".pho", ".art", ".top", ".bot",
}
DRILL_EXTS = {".drl", ".xln", ".nc", ".tap"}
KNOWN_EXTS = GERBER_EXTS | DRILL_EXTS | {".txt", ".gbrjob", ".zip"}

# KiCad default layer tokens as they appear in exported file stems
# ("board-F_Cu.gtl" -> "f_cu"). Order matters: specific first.
_KICAD_TOKENS = (
    ("edge_cuts", "outline"),
    ("f_cu", "cu_top"),
    ("b_cu", "cu_bottom"),
    ("f_mask", "mask_top"),
    ("b_mask", "mask_bottom"),
    ("f_silkscreen", "silk_top"),
    ("b_silkscreen", "silk_bottom"),
    ("f_silk", "silk_top"),
    ("b_silk", "silk_bottom"),
    ("f_paste", "paste_top"),
    ("b_paste", "paste_bottom"),
)

# Protel / Altium extension -> role (inner copper handled separately).
_PROTEL_EXTS = {
    ".gtl": "cu_top", ".gbl": "cu_bottom",
    ".gts": "mask_top", ".gbs": "mask_bottom",
    ".gto": "silk_top", ".gbo": "silk_bottom",
    ".gtp": "paste_top", ".gbp": "paste_bottom",
    ".gko": "outline", ".gm1": "outline",
    ".cmp": "cu_top", ".sol": "cu_bottom",
    ".stc": "mask_top", ".sts": "mask_bottom",
    ".crc": "silk_top", ".crs": "silk_bottom",
    ".smt": "mask_top", ".smb": "mask_bottom",
}

_HEAD_READ = 4096


def _is_inner_ext(ext):
    if ext.startswith(".g") and ext[2:].isdigit():
        number = int(ext[2:])
        return number if number >= 1 else None
    return None


def _classify(path):
    """(role, scheme) for one file. role None = not a fab file
    (or not recognisable as one). scheme: 'kicad' / 'protel' /
    'generic' / None. Drill sidecar files end in -PTH.drl /
    -NPTH.drl; both map to role 'drill'."""
    name = path.name
    lower = name.lower()
    ext = path.suffix.lower()
    stem = lower[: -len(path.suffix)] if path.suffix else lower
    # Dot-separated stems ("board.B_Cu.gbr", produced by several
    # exporters) get the same token treatment as -/_ separators
    # (v0.4 review RT-07/RT-21).
    stem = stem.replace(".", "_")
    # Multi-dot KiCad drill names: board-PTH.drl / board-NPTH.drl.
    if ext in DRILL_EXTS and ext != ".txt":
        return "drill", "protel" if ext in (".drl", ".xln") else "generic"
    if ext == ".gbrjob" or lower.endswith(".gbrjob"):
        return "jobfile", "kicad"
    # KiCad layer token in the stem.
    for token, role in _KICAD_TOKENS:
        if stem.endswith("-" + token) or stem.endswith("_" + token) \
                or token == stem:
            return role, "kicad"
    # KiCad inner copper: In1_Cu .. In32_Cu (v0.4 review RT-06 —
    # the marker list originally stopped at In6 and In7/In8 fell
    # through to the generic branch, which defaulted them to
    # top). Checked before the Protel extension map: an explicit
    # InN_Cu stem beats a generic .gtl extension.
    inner_match = re.search(r"in(\d+)_cu", stem)
    if inner_match:
        number = int(inner_match.group(1))
        if number < 1:
            return None, None
        return f"cu_inner{number}", "kicad"
    if ext in _PROTEL_EXTS:
        return _PROTEL_EXTS[ext], "protel"
    inner = _is_inner_ext(ext)
    if inner is not None:
        return f"cu_inner{inner}", "protel"
    if ext == ".gm3":
        return "outline", "protel"
    # Generic naming applies only to files carrying a fab
    # extension (audit F6): without this gate, words in ANY file
    # name — "outline.txt", "bottom_copper_notes.md" — satisfied
    # layer roles and suppressed missing-layer BLOCKs.
    if ext not in GERBER_EXTS and ext not in DRILL_EXTS:
        return None, None
    # Generic naming: words in the stem.
    words = stem.replace("-", "_").split("_")
    joined = "_".join(words)
    is_bottom = any(w in words for w in ("bot", "bottom", "b", "back")) \
        or "bottom" in joined
    is_top = any(w in words for w in ("top", "f", "front")) \
        or "top" in joined
    side = "bottom" if is_bottom else "top"
    if "outline" in joined or "edge" in joined:
        return "outline", "generic"
    # Drill before copper (v0.4 review RT-19): "drill_layer.gbr"
    # used to satisfy the loose copper test below and hide a
    # missing top copper BLOCK behind a mere drill caution.
    if "drill" in joined:
        return "drill", "generic"
    if "mask" in joined:
        return f"mask_{side}", "generic"
    if "silk" in joined:
        return f"silk_{side}", "generic"
    if "paste" in joined or "stencil" in joined:
        return f"paste_{side}", "generic"
    generic_inner = re.search(r"inner(\d+)", joined)
    if generic_inner:
        return f"cu_inner{int(generic_inner.group(1))}", "generic"
    if "inner" in joined:
        return None, None     # an inner layer, but which one?
    # "layer" alone no longer implies copper (RT-19): it needs a
    # copper word or an explicit side ("top_layer.gbr" is a real
    # generic name; bare "layer.gbr" is unrecognisable).
    if "cu" in words or "copper" in joined \
            or ("layer" in joined and (is_top or is_bottom)):
        return f"cu_{side}", "generic"
    if ext == ".gbr" and stem in ("top",):
        return "cu_top", "generic"
    if ext == ".gbr" and stem in ("bottom",):
        return "cu_bottom", "generic"
    return None, None


def _looks_like_excellon(path):
    """True when a file with an uninformative name (e.g. .txt)
    carries an Excellon drill header. Reads a bounded head only —
    refits the rung-11 file-safety rule: a symlink target is what
    open() reads, sizes come from os.stat on the entry itself."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_HEAD_READ)
    except OSError:
        return False
    # Excellon files open with M48, but a UTF-8 BOM or leading ';'
    # comment lines (both produced by real exporters) come first
    # (v0.4 review RT-09).
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    text = head.decode("latin-1").lstrip()
    for line in text.splitlines()[:8]:
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        return line.startswith("M48")
    return False


def scan_gerber_package(folder):
    """Classify a fab-output folder.

    Returns (roles, unrecognised, sizes, schemes):
      roles: {role: [file names]}
      unrecognised: [file names] with known fab extensions but no
        determinable role
      sizes: {file name: byte size} for every classified file
      schemes: {'kicad' / 'protel' / 'generic'} seen across files"""
    roles = {}
    unrecognised = []
    sizes = {}
    schemes = set()
    try:
        entries = sorted(Path(folder).iterdir(), key=lambda p: p.name)
    except OSError:
        return roles, unrecognised, sizes, schemes
    for path in entries:
        name = path.name
        if name.startswith("."):
            continue
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        ext = path.suffix.lower()
        role, scheme = _classify(path)
        if role is None and ext in (".txt", "") and size > 0:
            if "drill" in name.lower() or _looks_like_excellon(path):
                role, scheme = "drill", "generic"
        if role is None:
            if ext in KNOWN_EXTS:
                unrecognised.append(name)
            continue
        roles.setdefault(role, []).append(name)
        sizes[name] = size
        if scheme:
            schemes.add(scheme)
    return roles, unrecognised, sizes, schemes


def is_gerber_package(folder):
    """Auto-detection: a folder holding fab outputs rather than a
    KiCad project — no KiCad board/schematic files, and a
    package-shaped set of classified fab files (see below)."""
    try:
        names = [p.name for p in Path(folder).iterdir() if p.is_file()]
    except OSError:
        return False
    lows = [n.lower() for n in names]
    # A lone .kicad_pro does not make a project (audit F8): only
    # an actual board or schematic file does — an export folder
    # can legitimately carry a stray project-settings file.
    if any(n.endswith((".kicad_pcb", ".kicad_sch"))
           for n in lows):
        return False
    roles, _, _, _ = scan_gerber_package(folder)
    layers = list(roles)
    # Package shape: at least one classified LAYER file and at
    # least two classified fab files in total (RT-20: a two-file
    # top-copper + outline export deserves the missing-layer
    # findings, not an exit-3 shrug; an outline + drill folder
    # with one unrecognisable .gbr is a partial export, ditto).
    # A lone stray file never triggers package mode.
    layer_roles = [r for r in layers
                   if r not in ("drill", "jobfile")]
    counted = [r for r in layers if r != "jobfile"]
    return len(layer_roles) >= 1 and len(counted) >= 2


def run_gerber_checks(folder):
    """All package findings + a coverage dict for the report."""
    folder = Path(folder)
    roles, unrecognised, sizes, _schemes = scan_gerber_package(folder)
    findings = []
    layers = sorted(roles)
    # Naming-scheme consistency is judged on LAYER files only:
    # drill files and the .gbrjob sidecar are scheme-agnostic (a
    # KiCad export pairs KiCad-token layer names with a plain .drl,
    # and that is one coherent export, not a mix).
    schemes = set()
    for role, names in roles.items():
        if role in ("drill", "jobfile"):
            continue
        for name in names:
            _, scheme = _classify(folder / name)
            if scheme:
                schemes.add(scheme)
    inners = sorted(r for r in layers
                    if r.startswith("cu_inner"))
    inner_numbers = []
    for role in inners:
        try:
            inner_numbers.append(int(role[len("cu_inner"):]))
        except ValueError:
            pass
    inner_numbers.sort()    # numeric: [1, 10, 2] sorted as roles
                            # are strings — RT-05 false gap report

    def has(role):
        return role in roles and bool(roles[role])

    coverage = {
        "files_classified": sum(len(v) for v in roles.values()),
        "layers": layers,
        "schemes": sorted(schemes),
        "unrecognised": unrecognised,
        "package": True,
    }

    if not has("cu_top"):
        findings.append(Finding(
            "GERBER_TOP_COPPER_MISSING", BLOCK,
            "No top copper layer file was recognised in this "
            "package. A fab cannot make the board without it — "
            "re-export the fabrication outputs and check the "
            "top copper (F.Cu / .GTL) file is included.",
            where=folder.name))
    if not has("outline"):
        findings.append(Finding(
            "GERBER_OUTLINE_MISSING", BLOCK,
            "No board outline file was recognised in this package "
            "(Edge.Cuts / .GKO / outline). Without it the fab does "
            "not know the board's shape and will hold the order.",
            where=folder.name))
    if inners and not has("cu_bottom"):
        findings.append(Finding(
            "GERBER_INNER_WITHOUT_BOTTOM", BLOCK,
            "Inner copper layers are present "
            f"({', '.join(inners)}) but no bottom copper layer "
            "was recognised — the layer stack is inconsistent "
            "(inners count from the top, so a stack with inners "
            "must end in a bottom layer). Check for a missing or "
            "misnamed bottom copper file.",
            where=folder.name))
    if not inners and has("cu_top") and not has("cu_bottom"):
        findings.append(Finding(
            "GERBER_BOTTOM_COPPER_MISSING", CAUTION,
            "No bottom copper layer file was recognised. That is "
            "correct only for a single-sided board; a standard "
            "2-layer order needs bottom copper (B.Cu / .GBL).",
            where=folder.name))
    if has("cu_top") and not has("drill"):
        findings.append(Finding(
            "GERBER_DRILL_MISSING", CAUTION,
            "No Excellon drill file was recognised (.drl / .xln / "
            "-PTH.drl). Plated holes cannot be drilled without "
            "it; a board with no holes at all is the only case "
            "where this is fine.",
            where=folder.name))
    if has("drill") and not has("cu_top") and not inners:
        findings.append(Finding(
            "GERBER_DRILL_WITHOUT_COPPER", CAUTION,
            "A drill file is present but no copper layer was "
            "recognised at all — the package looks like a partial "
            "export (drill output without the Gerbers).",
            where=folder.name))
    for side, label in (("top", "top"), ("bottom", "bottom")):
        if has("cu_top") and not has(f"mask_{side}"):
            findings.append(Finding(
                "GERBER_MASK_MISSING", CAUTION,
                f"No {label} solder mask file was recognised. "
                "Ordering without a mask file the fab applies its "
                "own default (or holds the order); the exported "
                f"{label} mask (.GTS/.GBS, *_Mask) should be in "
                "the package.",
                where=folder.name,
                evidence=f"side={side}"))
    if has("cu_top") and not has("silk_top"):
        findings.append(Finding(
            "GERBER_SILKSCREEN_MISSING", CAUTION,
            "No top silkscreen file was recognised. Boards still "
            "work without legends, but reference designators and "
            "polarity marks will not be printed — fatal for "
            "hand-assembly sanity on dense boards.",
            where=folder.name))
    empty = sorted(name for name, size in sizes.items() if size == 0)
    for name in empty:
        findings.append(Finding(
            "GERBER_EMPTY_FILE", CAUTION,
            f"{name} is zero bytes. An empty layer file usually "
            "means the export step failed or the layer was empty "
            "in a way the fab will flag — re-export and compare.",
            where=name))
    for role in layers:
        names = roles[role]
        if len(names) <= 1:
            continue
        if role == "drill":
            # KiCad's standard drill split — one PTH file + one
            # NPTH file — is ONE drill deliverable in two files,
            # not a duplicate (v0.4 review FP-2). Only files
            # sharing the same PTH/NPTH/plain marker duplicate.
            groups = {}
            for name in names:
                low = name.lower()
                marker = "npth" if "npth" in low else \
                    "pth" if "pth" in low else "plain"
                groups.setdefault(marker, []).append(name)
            names = sorted(n for group in groups.values()
                           if len(group) > 1 for n in group)
            if not names:
                continue
        findings.append(Finding(
            "GERBER_DUPLICATE_LAYER", CAUTION,
            f"{len(names)} files claim the {role} role "
            f"({', '.join(sorted(names))}). A fab "
            "viewer will pick one — possibly the stale one. "
            "Keep exactly one file per layer in the package.",
            where=folder.name,
            evidence=f"role={role}"))
    if inner_numbers:
        top = max(inner_numbers)
        if top > 64:
            # A file name is not a stackup (audit F5): never build
            # range(1, top) from an untrusted number — one
            # ".g20000000" file produced a 189 MB report.
            findings.append(Finding(
                "GERBER_INNER_LAYER_GAP", CAUTION,
                f"Inner copper layer numbers run to {top}, far "
                "beyond any real board stack (KiCad supports at "
                "most 32 copper layers). A file is almost "
                "certainly misnamed — check the inner-layer "
                "exports before ordering.",
                where=folder.name))
        else:
            expected = list(range(1, top + 1))
            if inner_numbers != expected:
                missing = [n for n in expected
                           if n not in inner_numbers]
                findings.append(Finding(
                    "GERBER_INNER_LAYER_GAP", CAUTION,
                    "Inner copper layers are not consecutive: "
                    f"found {inners}, missing inner {missing}. "
                    "A gap in the stack usually means one inner "
                    "layer failed to export or was misnamed.",
                    where=folder.name))
    real_schemes = schemes
    if len(real_schemes) > 1:
        findings.append(Finding(
            "GERBER_MIXED_SCHEMES", NOTE,
            "Files from more than one naming scheme are mixed in "
            f"this package ({', '.join(sorted(schemes))}). That "
            "often means exports from two runs were merged — "
            "stale layers are the usual result. Prefer one clean "
            "export.",
            where=folder.name))
    for name in unrecognised:
        findings.append(Finding(
            "GERBER_UNRECOGNISED_FILE", NOTE,
            f"{name} has a Gerber/drill-style extension but its "
            "layer role could not be determined from its name, "
            "so it was not checked. Rename it to a standard "
            "scheme (KiCad defaults or .gtl/.gbl-style) if it is "
            "a layer the fab needs.",
            where=name))
    return findings, coverage
