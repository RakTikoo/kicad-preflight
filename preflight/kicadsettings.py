"""Project-stated DRC/ERC settings from the ``.kicad_pro`` file (v0.6).

KiCad lets a project restate how its own design-rule violations
should be treated, and a checker that ignores those statements
reports findings the project's author has already judged. This
module reads exactly the parts of ``.kicad_pro`` that carry those
statements — nothing else — so both the kicad-cli adapter
(``kicadcli.py``) and Preflight's native checks can honour them.

Where the statements actually live (KiCad 8 files, verified against
the corpus ``.kicad_pro`` files in this repo and by re-running
kicad-cli 8.0.9 on edited copies — see
``kicad-preflight-review/v06-ws1/real-json-catalogue.md``):

  * ERC severity overrides: top-level ``erc.rule_severities``,
    a map of ERC type string -> "error" / "warning" / "ignore".
  * ERC per-instance exclusions: top-level ``erc.erc_exclusions``,
    a list of strings. Every corpus list is empty and the exact
    string grammar could not be confirmed from real files, so
    entries are kept raw and matched only on the tokens that are
    unambiguous (the leading type and any UUIDs in the string).
  * DRC severity overrides: ``board.design_settings.rule_severities``,
    same value vocabulary as ERC.
  * DRC per-instance exclusions:
    ``board.design_settings.drc_exclusions``, a list of strings of
    the form ``type|x_nm|y_nm|uuid|uuid`` (nanometre coordinates;
    the two UUIDs are the violation's items, in order). Verified:
    upsy-desky's two entries match its two excluded
    ``items_not_allowed`` report entries exactly (position and
    UUIDs), and clearing the list made kicad-cli report both as
    ordinary violations again.

There is NO ``schematic`` -> ``erc`` or ``board`` -> ``drc``
severity section in real KiCad 8 files (the schematic subtree holds
drawing/BOM settings; severity maps live as above), and exclusions
are NOT stored in the ``.kicad_pcb`` (no exclusion text occurs in
any corpus board file; the ``.kicad_pro`` experiment above proves
the project file is the DRC storage, and ERC exclusions sit beside
the ERC severities in the same file).

Severity semantics (KiCad's own manuals, PCB Editor / Schematic
Editor, "Violation Severity" sections): a rule set to "ignore" is
completely disabled — KiCad produces no marker for it at all.
"error" / "warning" set the marker severity. A type absent from a
project's map has no project override: KiCad applies its built-in
default (corpus evidence: piantor's DRC map has no
``starved_thermal`` key, yet kicad-cli reports it as an error —
its default). An "exclusion" is a different thing from a severity:
it is a per-instance dismissal of one specific violation, recorded
in the lists above and surfaced by kicad-cli (only when exclusions
are requested) as a violation entry carrying ``"excluded": true``
with its real severity intact.

Safety: the ``.kicad_pro`` is untrusted JSON. Every value is
type-checked field by field; malformed content degrades to "no
settings" (or drops the single bad entry) and is counted in
``note`` / the stats, never raised. Sizes are capped so a hostile
file cannot make callers iterate unbounded lists.
"""

import json
import re
import stat as _stat
from dataclasses import dataclass, field
from pathlib import Path

VALID_SEVERITIES = ("error", "warning", "ignore")

# KiCad's factory-default severity for the types Preflight maps,
# verified identical in the .kicad_pro files of all three corpus
# projects (KiCad writes the FULL default map into every project
# file, so a stated severity equal to the factory default is not a
# project decision and must not remap anything). Consumers that
# remap severities (engine native checks, kicad-cli adapter) must
# go through `effective_override` so the rule is applied once,
# identically, everywhere (v0.6 review findings RT-02).
FACTORY_DEFAULT_SEVERITIES = {
    ("erc", "pin_not_connected"): "error",
    ("erc", "power_pin_not_driven"): "error",
    ("erc", "pin_to_pin"): "warning",
    ("erc", "label_dangling"): "error",
    ("erc", "global_label_dangling"): "warning",
    ("drc", "net_conflict"): "warning",
}


def effective_override(settings, kind, type_):
    """The severity a project EFFECTIVELY imposes for one type:
    "ignore" always applies (never a factory default); an
    error/warning applies only when it deviates from KiCad's
    factory default for that type. None = no effective override."""
    stated = settings.severity_override(kind, type_)
    if stated == "ignore":
        return "ignore"
    if stated in ("error", "warning") and \
            FACTORY_DEFAULT_SEVERITIES.get((kind, type_)) != stated:
        return stated
    return None

MAX_SEVERITY_ENTRIES = 512      # real KiCad 8 maps hold ~30-50 keys
MAX_EXCLUSIONS = 4096           # per kind; real projects hold a handful
MAX_PRO_BYTES = 16 * 1024 * 1024

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@dataclass
class Exclusion:
    """One per-instance exclusion string from a .kicad_pro list.

    ``x_nm`` / ``y_nm`` are set only for the verified DRC form
    (``type|x|y|uuid|uuid`` with integer nanometres); otherwise the
    entry is matched on type + UUID tokens only and ``raw`` is kept
    verbatim for Coverage/debugging."""

    raw: str = ""
    type: str = ""
    uuids: tuple = ()
    x_nm: int = None
    y_nm: int = None

    @property
    def pos_mm(self):
        if self.x_nm is None or self.y_nm is None:
            return None
        return (self.x_nm / 1_000_000.0, self.y_nm / 1_000_000.0)


@dataclass
class ProjectSettings:
    """Everything Preflight honours from one .kicad_pro file."""

    source_path: str = ""          # file the settings came from ("" = none)
    present: bool = False          # a .kicad_pro was found and parsed
    note: str = ""                 # human line for Coverage (malformed etc.)
    drc_severities: dict = field(default_factory=dict)   # type -> severity
    erc_severities: dict = field(default_factory=dict)   # type -> severity
    drc_exclusions: list = field(default_factory=list)   # [Exclusion]
    erc_exclusions: list = field(default_factory=list)   # [Exclusion]
    invalid_entries: int = 0       # severity/exclusion entries dropped
    # Waivers actually applied through this object, counted per
    # (kind, type, reason) so Coverage can disclose them:
    # {kind: {type: {"ignore": n, "exclusion": n}}}
    applied_waivers: dict = field(default_factory=dict)

    # -- severity overrides ------------------------------------------------

    def severity_override(self, kind, type_):
        """The project's stated severity for one DRC/ERC type, or None.

        ``kind`` is "drc" or "erc". Only "error" / "warning" /
        "ignore" are ever returned; anything else stored in the file
        was dropped at load time and counted in ``invalid_entries``."""
        table = self.drc_severities if kind == "drc" else \
            self.erc_severities if kind == "erc" else None
        if table is None or not isinstance(type_, str):
            return None
        return table.get(type_)

    def is_ignored(self, kind, type_):
        """True when the project set this rule type to 'ignore'."""
        return self.severity_override(kind, type_) == "ignore"

    # -- per-instance exclusions -------------------------------------------

    def exclusion_matches(self, kind, type_, item_uuids=(),
                          item_positions_mm=(),
                          positions_authoritative=False):
        """True when a stored exclusion names this specific violation.

        Matching is deliberately strict: the type must match, at
        least one of the violation's item UUIDs must appear in the
        exclusion string, and when the exclusion carries a verified
        DRC position, at least one item position must equal it
        (within 0.001 mm). With no item UUIDs there is nothing that
        identifies one instance, so this returns False — a
        type-only match would waive every instance of the rule,
        which is what 'ignore' is for, not exclusions."""
        uuids = {str(u) for u in item_uuids if u}
        if not uuids or not isinstance(type_, str):
            return False
        entries = self.drc_exclusions if kind == "drc" else \
            self.erc_exclusions if kind == "erc" else []
        positions = [(float(x), float(y)) for x, y in item_positions_mm
                     if isinstance(x, (int, float))
                     and isinstance(y, (int, float))]
        for entry in entries:
            if entry.type != type_:
                continue
            if not uuids.intersection(entry.uuids):
                continue
            pos = entry.pos_mm
            if pos is not None:
                # The exclusion is instance-specific via position.
                # When the caller HAS item data (the kicad-cli
                # adapter: positions_authoritative) but none of it
                # parsed into a position, the match cannot be
                # verified and fails closed (v0.6 review RT-06:
                # string/null coordinates must not reduce the
                # guard to UUID-only). Native findings matched via
                # where_key text carry no positions by construction,
                # so for them the UUID match stands.
                if not positions and positions_authoritative:
                    continue
                if positions and not any(
                        abs(px - pos[0]) <= 0.001
                        and abs(py - pos[1]) <= 0.001
                        for px, py in positions):
                    continue
            return True
        return False

    # -- the one-call consumer API ------------------------------------------

    def waived(self, settings_kind, rule_or_type, where_key="",
               item_uuids=(), item_positions_mm=(), aliases=(),
               positions_authoritative=False):
        """True when the project waives this finding; counts the waiver.

        The single entry point for both consumers:

        * the kicad-cli adapter calls it per violation with
          kind="drc"/"erc", rule_or_type=<KiCad type string>, and
          the violation's item UUIDs/positions taken from the report;
        * native checks call it with kind="erc" (ERC-lite /
          parity-adjacent rules) or "drc" (geometry-lite rules),
          rule_or_type=<the KiCad type the native rule corresponds
          to, or the Preflight rule id>, plus ``aliases`` naming the
          other vocabulary — e.g. a native ERC_UNCONNECTED_PIN
          finding passes rule_or_type="ERC_UNCONNECTED_PIN" and
          aliases=("pin_not_connected",). ``where_key`` is the
          finding's ``where``/evidence text: any UUID appearing in
          it is used for exclusion matching when ``item_uuids`` is
          not given, so native callers that only have text still get
          exact per-instance matching when a UUID is present.

        A finding is waived when any candidate type is set to
        "ignore" in the project's severity map, or when a stored
        exclusion matches it exactly. Every True result is counted
        in ``applied_waivers`` (and therefore in Coverage)."""
        kind = settings_kind
        candidates = [rule_or_type] + [a for a in aliases if a]
        uuids = tuple(item_uuids) or tuple(
            _UUID_RE.findall(where_key or ""))
        for cand in candidates:
            if self.is_ignored(kind, cand):
                self.record_waiver(kind, cand, "ignore")
                return True
        for cand in candidates:
            if self.exclusion_matches(kind, cand, uuids,
                                      item_positions_mm,
                                      positions_authoritative):
                self.record_waiver(kind, cand, "exclusion")
                return True
        return False

    def record_waiver(self, kind, type_, reason):
        """Count one applied waiver (called by ``waived``; public so
        consumers with their own matching can still count honestly)."""
        by_type = self.applied_waivers.setdefault(kind, {})
        counts = by_type.setdefault(type_, {"ignore": 0, "exclusion": 0})
        if reason in counts:
            counts[reason] += 1

    def waivers_applied_total(self):
        return sum(c["ignore"] + c["exclusion"]
                   for by_type in self.applied_waivers.values()
                   for c in by_type.values())

    # -- Coverage ------------------------------------------------------------

    def coverage_stats(self):
        """A JSON-safe dict for the report's Coverage section."""
        return {
            "present": self.present,
            "source": self.source_path or None,
            "note": self.note or None,
            "drc_severity_overrides": len(self.drc_severities),
            "erc_severity_overrides": len(self.erc_severities),
            "drc_types_ignored": sorted(
                t for t, s in self.drc_severities.items()
                if s == "ignore"),
            "erc_types_ignored": sorted(
                t for t, s in self.erc_severities.items()
                if s == "ignore"),
            "drc_exclusions_stored": len(self.drc_exclusions),
            "erc_exclusions_stored": len(self.erc_exclusions),
            "invalid_entries_dropped": self.invalid_entries,
            "waivers_applied": self.applied_waivers,
            "waivers_applied_total": self.waivers_applied_total(),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _severity_map(node):
    """(map, invalid_count) from a rule_severities subtree."""
    out, invalid = {}, 0
    if not isinstance(node, dict):
        return out, (1 if node is not None else 0)
    for key, value in list(node.items())[:MAX_SEVERITY_ENTRIES]:
        if isinstance(key, str) and isinstance(value, str) \
                and value.strip().lower() in VALID_SEVERITIES:
            out[key] = value.strip().lower()
        else:
            invalid += 1
    if len(node) > MAX_SEVERITY_ENTRIES:
        invalid += len(node) - MAX_SEVERITY_ENTRIES
    return out, invalid


def _parse_exclusion(raw):
    """Parse one exclusion string; None when it is not a string at all.

    The verified DRC form ``type|x_nm|y_nm|uuid|uuid`` yields a
    structured entry. Any other string is kept raw: its type is the
    text before the first '|' and its UUIDs are every UUID-shaped
    token in it — enough for exact matching without asserting a
    grammar KiCad 8 ERC files have not demonstrated."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = raw.split("|")
    entry = Exclusion(raw=raw, type=parts[0].strip(),
                      uuids=tuple(_UUID_RE.findall(raw)))
    if len(parts) >= 5:
        try:
            x_nm = int(parts[1])
            y_nm = int(parts[2])
        except ValueError:
            x_nm = y_nm = None
        if x_nm is not None and abs(x_nm) < 10**12 \
                and abs(y_nm) < 10**12:
            entry.x_nm, entry.y_nm = x_nm, y_nm
    return entry


def _exclusion_list(node):
    """([Exclusion], invalid_count) from an exclusions subtree."""
    out, invalid = [], 0
    if node is None:
        return out, 0
    if not isinstance(node, list):
        return out, 1
    for raw in node[:MAX_EXCLUSIONS]:
        entry = _parse_exclusion(raw)
        if entry is None:
            invalid += 1
        else:
            out.append(entry)
    if len(node) > MAX_EXCLUSIONS:
        invalid += len(node) - MAX_EXCLUSIONS
    return out, invalid


def settings_from_data(data, source_path=""):
    """Build ProjectSettings from an already-parsed .kicad_pro dict.

    Never raises: a non-dict root, or any malformed subtree, degrades
    field by field and is described in ``note``."""
    settings = ProjectSettings(source_path=str(source_path))
    if not isinstance(data, dict):
        settings.note = (".kicad_pro did not contain a JSON object — "
                         "project settings not applied")
        return settings
    settings.present = True
    notes = []

    erc = data.get("erc")
    if isinstance(erc, dict):
        settings.erc_severities, bad = _severity_map(
            erc.get("rule_severities"))
        settings.invalid_entries += bad
        settings.erc_exclusions, bad = _exclusion_list(
            erc.get("erc_exclusions"))
        settings.invalid_entries += bad
    elif erc is not None:
        settings.invalid_entries += 1
        notes.append("erc section was not an object")

    board = data.get("board")
    design = board.get("design_settings") if isinstance(board, dict) \
        else None
    if isinstance(design, dict):
        settings.drc_severities, bad = _severity_map(
            design.get("rule_severities"))
        settings.invalid_entries += bad
        settings.drc_exclusions, bad = _exclusion_list(
            design.get("drc_exclusions"))
        settings.invalid_entries += bad
    elif board is not None:
        settings.invalid_entries += 1
        notes.append("board.design_settings was not an object")

    if settings.invalid_entries:
        notes.append(
            f"{settings.invalid_entries} malformed setting "
            "entr(ies) dropped")
    settings.note = "; ".join(notes)
    return settings


def load_settings(pro_path):
    """Load ProjectSettings from a .kicad_pro path. Never raises.

    ``pro_path`` may be None (no project file): the result is an
    absent settings object, which waives nothing. A file that cannot
    be read, is oversize, or is not valid JSON degrades to "no
    settings" with the reason in ``note`` — a malformed .kicad_pro
    must never crash a run or silently pretend to be settings."""
    if pro_path is None:
        return ProjectSettings(note="no .kicad_pro found")
    path = Path(pro_path)
    settings = ProjectSettings(source_path=str(path))
    try:
        st = path.stat()
    except OSError as exc:
        settings.note = f".kicad_pro could not be read ({exc}) — " \
                        "project settings not applied"
        return settings
    if not _stat.S_ISREG(st.st_mode):
        # A FIFO/device would block read forever (v0.6 audit M1);
        # project.read_project_file refuses these for every other
        # project file, and so does this reader.
        settings.note = (".kicad_pro is not a regular file — "
                         "project settings not applied")
        return settings
    if st.st_size > MAX_PRO_BYTES:
        settings.note = (f".kicad_pro is {st.st_size} bytes, over the "
                         f"{MAX_PRO_BYTES}-byte budget — project "
                         "settings not applied")
        return settings
    try:
        # Bounded read: a file that grows after the stat cannot
        # exceed the budget either (stat->read TOCTOU, audit M1).
        with open(path, "rb") as fh:
            raw = fh.read(MAX_PRO_BYTES + 1)
        if len(raw) > MAX_PRO_BYTES:
            settings.note = (".kicad_pro grew past the size budget "
                             "while reading — project settings not "
                             "applied")
            return settings
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError,
            RecursionError) as exc:
        # RecursionError: pathologically nested JSON must degrade
        # like any other malformed file (audit M2 / RT-03) — this
        # function's contract is "never raises".
        settings.note = (f".kicad_pro was not valid JSON "
                         f"({type(exc).__name__}) — "
                         "project settings not applied")
        return settings
    loaded = settings_from_data(data, source_path=str(path))
    return loaded


def load_for_project(project):
    """Load settings for a loaded Project (uses its ``pro_path``).

    Convenience for the engine: tolerates a project object without a
    ``pro_path`` attribute (older shims in standalone modes)."""
    pro_path = getattr(project, "pro_path", None)
    return load_settings(pro_path)
