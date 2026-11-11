# KiCad Preflight — Rule Reference

Every deterministic rule in v0.5, its severity, and what triggers it.
Verdicts: any BLOCK → BLOCK (exit 2); else any CAUTION → CAUTION
(exit 1); else PASS (exit 0). NOTE findings never change the verdict.
Rules marked ◆ run in standalone BOM mode too (no schematic needed).

## Project shape & coverage

| Rule | Severity | Trigger |
| --- | --- | --- |
| NO_PROJECT_FILES | BLOCK | Folder has a project file target but no schematic and no PCB — nothing could be checked. |
| PROJECT_FILE_UNPARSEABLE | BLOCK | A .kicad_sch/.kicad_pcb exists but cannot be read: bad S-expression, non-regular file (FIFO/device), over the 64 MB budget, or not valid UTF-8. Dependent checks are skipped, never guessed. |
| MISSING_PCB_FILE | BLOCK | Schematic present, no .kicad_pcb. |
| MISSING_SCHEMATIC_FILE | BLOCK | PCB present, no .kicad_sch (a legacy .sch, if present, is named in the message). |
| MISSING_SHEET_FILE | BLOCK | A hierarchical sub-sheet referenced by the root sheet is not on disk. |
| PCB_NO_FOOTPRINTS_PARSED | BLOCK | PCB parsed to zero footprints while the schematic has parts — unplaced board or unreadable format; per-part blame is withheld. |
| SCAN_INCOMPLETE | CAUTION | Extra KiCad files in the folder were not loaded (a second project), dataset entries were unusable, or a layer abstained and says why in the message (netlist extraction/parity budget breach, a sheet file instantiated by multiple sheet blocks, parity-unmeasurable references). |
| FINDINGS_TRUNCATED | CAUTION | More than 2,000 findings; the rest are counted, not listed. |
| BOM_ONLY_MODE | NOTE | Standalone BOM run: cross-checks did not run, by design of the mode. |

## Schematic ↔ PCB

| Rule | Severity | Trigger |
| --- | --- | --- |
| SCH_PART_NOT_ON_PCB | BLOCK | Schematic part has no (part) footprint on the PCB. |
| PCB_PART_NOT_IN_SCHEMATIC | BLOCK | A part footprint on the PCB has no schematic symbol. Board furniture (mounting holes with `board_only`, logos/labels with placeholder or generated refs) is never counted. |
| FOOTPRINT_MISMATCH | BLOCK | Schematic Footprint property ≠ placed PCB footprint. Aggregated per (schematic, PCB) footprint pair, listing all affected refs. |
| DUPLICATE_REFERENCE_SCHEMATIC | BLOCK | Same reference + unit placed twice in the schematic. Multi-unit symbols (same ref, different units, same library part) are correct and pass. |
| DUPLICATE_REFERENCE_PCB | BLOCK | Same designator on 2+ part footprints on the PCB. |
| MULTI_UNIT_PART_CONFLICT | BLOCK | One reference used by symbols of different library parts. |
| MULTI_UNIT_PROPERTY_DIVERGENCE | CAUTION | Units of one part disagree on the footprint. |
| UNANNOTATED_SYMBOL | CAUTION | Placed symbols have a value/footprint but no reference (aggregated). |
| SCHEMATIC_FOOTPRINT_MISSING | CAUTION | No footprint assigned in the schematic. |
| FOOTPRINT_NO_LIBRARY | CAUTION | Footprint string has no `Library:` prefix. |
| SCHEMATIC_VALUE_MISSING | CAUTION | Value empty or `~`. |
| SCHEMATIC_PLACEHOLDER_MPN | CAUTION | Schematic MPN property is a placeholder (`TBD`, `~`, `N/A`, …). |
| SCHEMATIC_DATASHEET_MISSING | NOTE | Empty Datasheet property (aggregated, lists up to 8 refs). |

## BOM ◆

| Rule | Severity | Trigger |
| --- | --- | --- |
| BOM_UNREADABLE | BLOCK | A BOM file exists but won't parse (bad header, CSV error, over row/field budgets). |
| BOM_UNKNOWN_REFERENCE ◆(needs schematic) | BLOCK | BOM ref not in the schematic. |
| BOM_QUANTITY_MISMATCH ◆ | BLOCK | Quantity cell is not a whole number equal to the row's reference count (`2.9`, `1e999`, blank all count as not-a-whole-number). |
| BOM_MISSING | CAUTION | Project has a schematic but no usable BOM CSV — BOM and part-risk checks did not run. |
| BOM_PART_NOT_ORDERABLE | CAUTION | BOM lists a part the schematic marks DNP / not-in-BOM. |
| BOM_DUPLICATE_REFERENCE ◆ | CAUTION | Same ref on 2+ BOM rows. |
| BOM_MISSING_MPN ◆ | CAUTION | Row has neither an MPN nor an LCSC/JLCPCB part number. (LCSC-only rows pass and are counted in Coverage.) |
| BOM_PLACEHOLDER_MPN ◆ | CAUTION | MPN cell is a placeholder (`TBD`, `~`, `N/A`, `none`, …). |
| BOM_MISSING_FOOTPRINT ◆ | CAUTION | Row has no footprint/package cell. |
| BOM_FOOTPRINT_MISMATCH | CAUTION | BOM footprint disagrees with the schematic footprint for the row's part. |
| BOM_ROW_WITHOUT_REFERENCES ◆ | CAUTION | Row carries quantity/part data but no designators. |
| SCHEMATIC_PART_MISSING_FROM_BOM | CAUTION | A BOM-expected schematic part appears on no BOM row. |
| BOM_VALUE_DIFFERS | NOTE | BOM value and schematic value share no containment either way (values are often normalised, so this informs, never blocks). |
| BOM_PART_NUMBER_CONFLICT ◆ | CAUTION | The same MPN (or LCSC number) appears on 2+ BOM rows describing different parts (different value or footprint) — one part number cannot be two parts; a copy-paste error would order the wrong part. Identical part split across rows is fine. Part numbers are grouped ignoring dashes and spaces (distributor spellings vary), and values/footprints compare whitespace-insensitively (`10k` = `10 k`). (v0.3) |

BOM parsing notes: header aliases cover KiCad, JLCPCB (`Comment /
Designator / Footprint / LCSC Part #`) and generic spellings; reference
ranges `R1-R3`, `R1-3` and `R1 - R3` are expanded (≤200 per range).
A position/CPL file is never mistaken for the BOM: a CSV whose header
is a position header (designator + X/Y columns) is skipped during
BOM discovery — unless it also carries BOM-only columns (quantity,
MPN, LCSC), in which case it is treated as a BOM that happens to
include coordinates, because position files never carry part
numbers.

## KiCad file-format version (v0.3)

| Rule | Severity | Trigger |
| --- | --- | --- |
| KICAD_VERSION_UNTESTED | CAUTION | A schematic/PCB file's format stamp is newer than the tested range (KiCad 6–8): schematic format > 20231120, PCB format > 20240108, or generator_version 9+. Checked on the root sheet, every loaded sub-sheet, and the PCB. The file is still parsed, but KiCad 9's embedded files/tables/pad stacks and KiCad 10's design variants, jumpers and back-drilling are not fully understood, so a PASS deserves a hand check. Also fires for pre-KiCad-6 format stamps, and for a stamp that is present but unreadable (malformed) — unreadable format evidence warns rather than passing silently. Detected versions are always listed in Coverage. |

## Fabrication readiness (v0.3)

Thresholds are the published standard-process minimums of JLCPCB and
PCBWay (official capability pages, checked 2026-11-10; sources in
docs/COVERAGE.md). A value below a *standard* minimum is a CAUTION —
fabs raise an engineering question or offer an advanced process; it
is not an automatic rejection. Every rule fires only on a measured
value: a board with no tracks (or no vias, no drilled pads) abstains,
and Coverage reports the zero counts.

| Rule | Severity | Trigger |
| --- | --- | --- |
| BOARD_OUTLINE_MISSING | BLOCK | PCB has footprints but no root-level drawing item on the Edge.Cuts layer (graphics or segments; footprint-internal graphics are not counted). No outline = nothing to cut; the most common cause of a held order. |
| ZONE_UNFILLED | CAUTION | A root-level copper zone has no usable filled polygon stored in the file (no fill node, an empty one, or a declared layer left unfilled) — it exports as missing copper. Refill zones and save before exporting. Keepout/rule-area zones are not pours and are never counted. |
| TRACK_WIDTH_BELOW_FAB_MIN | CAUTION | A copper track (straight segment or arc) is narrower than 0.10 mm, both fabs' standard minimum trace width (1–2 layer, 1 oz). |
| DRILL_BELOW_FAB_MIN | CAUTION | Smallest plated pad drill < 0.20 mm (PCBWay's standard minimum); the message says so when it is also < 0.15 mm (JLCPCB's minimum). Non-plated holes excluded. |
| VIA_BELOW_FAB_MIN | CAUTION | Smallest via drill < 0.20 mm (tiers as above), or smallest via diameter < 0.25 mm (JLCPCB). |
| ANNULAR_RING_BELOW_FAB_MIN | CAUTION | A plated pad's annular ring (pad min dimension − drill) / 2 is < 0.15 mm — below every published standard/tight-tier minimum (0.15–0.20 mm). Non-plated holes excluded (their ring is zero by design). |
| SLOT_BELOW_FAB_MIN | CAUTION | A plated oval slot's narrow dimension is < 0.50 mm, both fabs' minimum plated slot width. |
| DIMENSION_UNREADABLE | CAUTION | Dimension values are present in the PCB (track widths, via/pad drills) but could not be read as numbers, so those items were not measured against the minimums. Nothing unmeasured passes silently. |
| CPL_UNREADABLE | CAUTION | A position/CPL file was found but could not be parsed, so CPL agreement was not checked for it. Discovery records failures for position-named files and for any file with a position-shaped header, whatever its name. |
| CPL_UNKNOWN_REFERENCE | CAUTION | The position file lists designator(s) that do not exist on the PCB (any footprint, furniture included). A standard cause of a held assembly order. |
| CPL_PART_MISSING | CAUTION | Board part(s) have no row in the position file — unplaced if assembly is ordered. Footprints flagged `exclude_from_pos_files` are not expected and never blamed. An existing but empty position file blames every part. Only matters for assembly orders. |
| CPL_DUPLICATE_REFERENCE | CAUTION | The position file lists the same designator more than once (across all merged position files — per-side files are merged before checking). |

## Geometry-lite (v0.4)

A conservative distance-checking subset over the copper objects the
parser extracts (pads, tracks, arcs, vias, and — since v0.6 — the
stored filled polygons of copper zones) — explicitly NOT KiCad
DRC. Shapes are exact (circles, rectangles at their real rotation,
ovals, arcs subdivided with a bounded error that is only ever
counted against the measurement, roundrects as their true core
rectangle + corner radius). Zone fills: a zone's stored
filled_polygon rings are measured against different-net objects
and other zones on the same layer, and (only when the project
states its own edge clearance) against the outline. Two
evidence-based scopings, both established against kicad-cli DRC
as the reference implementation (experiments in the project's
review record): zone-involving pairs are judged with a 0.006 mm
certainty tolerance, because stored fill boundaries are chord
approximations whose distance undershoots true clearance by up
to ~0.005 mm — encroachments inside that band are a documented
miss, never a coin-flip finding; and pads written as multiple
padstack entries are not measured against zone fills (KiCad's
filler clears around one entry's shape and legitimately leaves
sibling-entry copper inside the stored fill), those pairs are
skipped and counted in Coverage. A zone with no readable filled
polygon abstains individually and is counted. Thresholds come first from the board
itself — the `.kicad_pro` design-rule `min_clearance` /
`min_copper_edge_clearance` and per-netclass clearances with KiCad's
pattern assignments (the pattern list is priority-ordered: the
FIRST pattern matching a net name assigns its class; net names are
resolved for every object from the board's net table, since tracks
and vias store only net ids) — and only when the board states
nothing, from
the published fab minimums above: 0.10 mm trace spacing and 0.20 mm
copper-to-edge (JLCPCB/PCBWay standard process; sources in
docs/COVERAGE.md). A pair is reported only when it is below its
threshold by more than 0.002 mm, so boards routed exactly at their
design rule (whose file coordinates are decimal roundings) do not
drown in sub-micron noise.

Never measured, always stated in Coverage: custom and
trapezoid pad SHAPES (no exact outline is derivable; their net
membership IS used by parity), footprint-internal
graphics, and any pair where either object's net is unknown — an
unstated net or net 0 is never guessed, so same-footprint pins with
no assigned net cannot produce a finding. Pad positions and
orientations use KiCad's own conventions as written in the file
(pad orientation is stored board-absolute; back-side footprints are
saved pre-mirrored) — both verified against same-net track
endpoints and design-clearance landings on real boards.

| Rule | Severity | Trigger |
| --- | --- | --- |
| GEOMETRY_CLEARANCE_BELOW_MIN | CAUTION | Copper objects on the same layer belonging to different nets sit closer than the applicable clearance (board rules first, else the 0.10 mm fab minimum). Measured objects: pads, tracks, arcs, vias and filled zone polygons (zone pairs judged with the 0.006 mm certainty tolerance described above). One aggregated finding naming the worst pair. |
| GEOMETRY_COPPER_TO_EDGE | CAUTION | A copper object sits closer to the Edge.Cuts outline than the applicable minimum (board's stated edge clearance, else 0.20 mm). Zone polygons are edge-checked only when the board states its own edge clearance (their fills are generated under that rule; against a fallback they would false-fire). Edge-mounted connectors are the classic intentional case — the finding asks for a look, it does not claim an error. |
| BOARD_OUTLINE_OPEN | BLOCK | Root-level Edge.Cuts lines/arcs/curves whose endpoints do not pair up within 0.01 mm and whose chain is not a routed slot — the outline does not close and cannot be cut as drawn (this includes an outline shattered at every corner into near-miss fragments). An open chain counts as a routed slot only when every subdivided point of it lies strictly inside a closed loop's interior (a self-closed circle/rect/poly, or a chained loop); a point on or within 0.01 mm of a loop boundary is not inside, so a chain touching the outline fires as a notch. Self-closed items always pass as elements, but they never mask an unrelated open chain: deleting one side of an outer outline fires even when mounting-hole circles share the layer. With no closed loop anywhere, a lone open element still abstains rather than guessing, and an endpoint pattern too close to the tolerance to judge abstains instead of firing. Legacy `(angle ...)` arcs are chained under every reading real files use (centre-in-`(start)`, both sweep directions, and endpoints-in-`(start)`); conflicting readings abstain. |
| GEOMETRY_SKIPPED | CAUTION | The geometry layer abstained: more than 40,000 copper objects, or the pair-measurement budget (2,000,000) was exceeded; the zone pass has its own budgets (200,000 fill edges, 5,000,000 candidate examinations, 2,000,000 pair checks, 2,000,000 containment tests) and abstains separately, counted in Coverage. The other layers are unaffected; KiCad DRC covers the board. |

## Gerber package (v0.4)

File-level checks on a folder of fabrication outputs — the moment
after export, before upload. Auto-detected when a folder holds
Gerber/drill files and no KiCad project; forced with `--gerber`.
File contents are never parsed (one exception: an uninformative
`.txt` name is checked for an Excellon `M48` header, reading at
most 4 KB). Naming schemes recognised: KiCad default exports
(layer tokens in the file stem), Protel extensions (.gtl/.gbl/
.gts/.gbs/.gto/.gbo/.gtp/.gbp/.gko/.gm1, inners .g1…, legacy
.cmp/.sol/.stc/.sts/.crc/.crs/.smt/.smb), and generic word naming
(`top_copper.gbr`, `bottom.gbr`, …). A file with a fab extension
whose role cannot be determined is reported, never guessed.

| Rule | Severity | Trigger |
| --- | --- | --- |
| GERBER_TOP_COPPER_MISSING | BLOCK | No file classifies as top copper — the package cannot be fabricated. |
| GERBER_OUTLINE_MISSING | BLOCK | No board-outline file (Edge.Cuts / .gko / outline) is recognised. |
| GERBER_INNER_WITHOUT_BOTTOM | BLOCK | Inner copper layers exist but no bottom copper — the stack is inconsistent (inners count from the top). |
| GERBER_BOTTOM_COPPER_MISSING | CAUTION | No bottom copper and no inners — correct only for a genuinely single-sided board. |
| GERBER_MASK_MISSING | CAUTION | Top (or bottom) solder mask not recognised while copper exists (one finding per side). |
| GERBER_SILKSCREEN_MISSING | CAUTION | No top silkscreen recognised. |
| GERBER_DRILL_MISSING | CAUTION | Copper present but no Excellon drill file recognised. |
| GERBER_DRILL_WITHOUT_COPPER | CAUTION | A drill file is present but no copper layer at all — a partial export. |
| GERBER_EMPTY_FILE | CAUTION | A classified layer file is zero bytes (one finding per file). |
| GERBER_DUPLICATE_LAYER | CAUTION | Two or more files claim the same layer role — a fab viewer will pick one, possibly the stale one. (KiCad's standard drill split — one `-PTH` file plus one `-NPTH` file — is one deliverable in two files and does NOT trigger this.) |
| GERBER_INNER_LAYER_GAP | CAUTION | Inner copper layers are not consecutive (e.g. .g1 and .g3 but no .g2). |
| GERBER_MIXED_SCHEMES | NOTE | Layer files were classified via more than one naming scheme (drill/job files do not count) — usually two exports merged. |
| GERBER_UNRECOGNISED_FILE | NOTE | A file has a Gerber/drill-style extension but its layer role could not be determined from its name (one per file). |

## Netlist: ERC-lite + schematic<->PCB parity (v0.5)

The schematic netlist is extracted natively: library pin
definitions and placed-symbol transforms give every pin's
connection point (verified empirically on real boards — 92.9–100%
of computed pin points land on a wire, label, junction, no-connect
marker or another pin), and union-find over wires, junctions,
labels and sheet pins builds the nets. KiCad semantics throughout
(Schematic Editor manual, ERC / labels / hierarchy sections;
Schematic File Format spec, dev-docs.kicad.org): coincident points
connect, a point on a wire segment connects, crossing wires
connect only at a junction, labels connect by name within a sheet
regardless of label type (local and hierarchical labels share one
per-sheet namespace, and a global label's name also joins its own
sheet's namespace; a global label additionally exports the name
to every sheet), hierarchical labels additionally bind to the
same-named sheet pin in the parent, and a hidden power_in pin on
an ordinary symbol connects globally to the net named by the pin
name — by name only: the pin is detached from whatever geometric
component it happens to sit on. Coincidence is a distance test
(EPS 0.0005 mm), not a rounded-bucket test. Whether a symbol is a
power symbol follows the FILE: an embedded library definition's
(power) flag decides; the `power:` lib_id prefix and `#` ref
heuristics apply only when no definition is embedded. Power net
names are exact (no whitespace stripping), like label names.
When several power symbols land on one wire, ALL their names
group that wire globally. Where KiCad's documentation and an
earlier design assumption disagreed, the documentation won (see
the power rules below). Abstentions: a schematic with no
connectivity constructs at all stays silent; pins whose library
definition is missing are counted as unresolved and excluded; a
pin whose only connection is via a bus segment is bus-connected,
counted and excluded; a sheet FILE instantiated by two or more
sheet blocks abstains the whole layer (the loader resolves sheets
per file, not per instance); extraction budgets (pins / wires /
bus segments / labels / union-find work, including bus-touch
point tests and parity pair examinations) turn a breach into
SCAN_INCOMPLETE rather than a partial guess.

| Rule | Severity | Trigger |
| --- | --- | --- |
| ERC_UNCONNECTED_PIN | CAUTION | A resolved pin joins no net and carries no no-connect marker at its point (aggregated per reference, pin numbers listed). Unit-common pins (drawn in every unit, `_0_1`) are ONE physical pin: connectivity is evaluated per (reference, pin number) across all unit instances, so a common pin wired or NC-marked on any instance counts as connected. KiCad ERC rates "pin not connected" an error; Preflight reports CAUTION — no-connect discipline in real libraries is inconsistent, and the pin may be intentionally open. No-connect-type pins never fire; pins of power symbols do (a floating power symbol is a real defect). Pins with no pin number in the library definition are unresolved (counted in Coverage), never reported here. |
| ERC_POWER_NOT_DRIVEN | CAUTION | A net carries at least one power_in pin but no power_out pin. KiCad's documented power-pin test accepts ONLY a power_out pin as a driver — a regulator pin typed power_out, or a PWR_FLAG — never passive, output or bidirectional pins ("Input Power pin not driven by any Output Power pins", Schematic Editor manual, ERC section). KiCad rates it an error; CAUTION here, because real boards routinely feed rails through connectors/passives. Since v0.6 the project's own `.kicad_pro` ERC settings apply to this rule like the others (see "Project KiCad settings" below): a project that ignores or re-maps `power_pin_not_driven` is honoured. One finding per net. |
| ERC_PIN_CONFLICT | BLOCK | A net carries one of the remaining ERROR pairings of KiCad's documented pin-conflict matrix not covered by ERC_OUTPUT_CONFLICT: output+open_collector, output+open_emitter, output+no_connect, tri_state+open_collector, open_collector+open_collector, open_collector+open_emitter, open_collector+no_connect. (Matrix source: the Schematic Editor manual's ERC pin table, identical to the default `erc.pin_map` KiCad stores in project files.) The matrix's WARNING cells are deliberately not reported — power_in pairs warn against almost everything, so a warning rule would fire on nearly every power net of every healthy board (the same evidence bar that rejected ERC_INPUT_UNDRIVEN); `free` pins are "no ERC" by definition, and power_out+tri_state is a warning cell. One finding per net. |
| ERC_OUTPUT_CONFLICT | BLOCK | A net ties together two output pins, an output and a power_out pin, or two power_out pins. All three pairings are errors in KiCad's documented pin-conflict matrix (Schematic Editor manual, ERC section) — including the two-PWR_FLAG case, since a PWR_FLAG's pin is power_out. When every conflicting driver is a power_out pin, the message names the overwhelmingly likely cause — duplicate PWR_FLAGs ("use one PWR_FLAG per net; remove the extra flag(s)") — instead of the outputs-shorted wording. BLOCK mirrors the severity of KICAD_ERC_VIOLATION for the same KiCad ERC error. One finding per net. |
| ERC_DANGLING_LABEL | NOTE | A label (local, global or hierarchical) whose point touches no wire and no pin — it names nothing. KiCad ERC warns on these ("label not connected to anything"). One finding per label. |
| PARITY_NET_MEMBERSHIP | BLOCK | A net matched between schematic and PCB (by name, or by sheet-qualified name for sub-sheet nets) whose in-scope pin/pad membership differs: pins present on one side only are listed (up to ~10 per side). Refs outside the shared part scope and unmeasurable refs (below) are suppressed, never blamed. |
| PARITY_NET_SCH_ONLY | BLOCK | A named schematic net with in-scope members has no PCB counterpart — suppressed when every in-scope member ref is already missing from the PCB (the part-level rules own that blame) or when its membership exactly equals a PCB-only net's (a rename). Unnamed wire nets: if the net's membership is a subset of exactly ONE unmatched PCB net's membership, the two are paired and any membership asymmetry is reported as PARITY_NET_MEMBERSHIP naming the PCB net (no sch-only BLOCK); if the net spans several PCB nets, this rule stands. When two schematic nets share one display name (a sheet-local label net vs the global power net of the same name), name matching pairs by membership overlap first and findings carry the scoped schematic identity (name source and sheet files), so blame lands on the right twin. |
| PARITY_NET_PCB_ONLY | BLOCK | Symmetric: a named PCB net with in-scope pads is absent from the schematic, with the same suppressions. |
| PARITY_NET_RENAMED | NOTE | A schematic-only net and a PCB-only net with identical in-scope membership: the net was renamed between schematic and PCB. Membership is truth, so neither *_ONLY BLOCK fires. Unnamed schematic nets that match a PCB net's membership exactly are treated as matched silently. |

Deliberately NOT shipped: ERC_INPUT_UNDRIVEN. KiCad warns/errors
on input pins with no driver, but the pin-type matrix cannot
judge nets driven through bidirectional or passive arrangements,
and the measured counts on working corpus boards (12 / 7 / 4 / 4
input-only nets on upsy-desky / esp-rust-board / piantor halves)
show even a NOTE would flag every healthy board. The counts are
reported in Coverage (`input_only_nets`) instead.

Parity scope and abstentions: parts must be non-power, on-board,
carry a footprint, and exist on both sides (reference presence is
owned by SCH_PART_NOT_ON_PCB / PCB_PART_NOT_IN_SCHEMATIC, never
re-blamed here). Membership needs only the stated (reference, pad
name, net), so it covers modelled pads, non-plated pads,
trapezoid pads and custom pads whose primitive set is modelled
(v0.6); a footprint containing a custom pad with UNMODELLED
primitives, or an unplaced/non-positive pad, makes that
reference unmeasurable: parity abstains for it and the engine
reports SCAN_INCOMPLETE with the ref list. (Clearance GEOMETRY
for trapezoid/custom pads is still not modelled — membership is
exact, shapes are not.)
KiCad's per-pad `unconnected-(REF-PadN)` pseudo-nets are not
nets and are excluded (counted). Single-pin schematic nets are
excluded from parity membership, and symbols whose library
definition has no pins at all (e.g. mounting holes) are not
parity refs. Limits: bus nets are excluded, a sheet file
instantiated by multiple sheet blocks abstains the whole
netlist layer, and IPC-format netlist import is not supported
(docs/COVERAGE.md, class 29).

## Part risk ◆ (offline sample dataset — findings cite it)

| Rule | Severity | Trigger |
| --- | --- | --- |
| PART_OBSOLETE | BLOCK | Dataset status is obsolete/EOL/discontinued/last-time-buy. |
| PART_OUT_OF_STOCK | BLOCK | Dataset stock is 0 (string/float counts are coerced first). |
| PART_NOT_RECOMMENDED | CAUTION | Dataset status is NRND. |
| PART_LOW_STOCK | CAUTION | Stock < 50 (and > 0). |
| PART_LONG_LEAD_TIME | CAUTION | Lead time > 16 weeks. |
| PART_SINGLE_SOURCE | NOTE | Exactly 1 source in the dataset. |
| PART_NOT_IN_SAMPLE_DATA | NOTE | MPN absent from the dataset — "could not check", never a guess about the part. |
| PART_DATASET_ENTRY_INVALID | NOTE | MPN present in the dataset but its entry is empty/unusable — a dataset bug, not a board bug. |

MPN lookup is exact first, then case-insensitive; a case-insensitive
hit is disclosed in the finding's evidence. One finding per MPN,
aggregating every BOM row that uses it.

## Part risk — online (opt-in, `--online`) ◆ (v0.6)

With `--online`, BOM part numbers are looked up at a live
distributor (DigiKey or Mouser) and the live result SUPERSEDES the
sample-dataset result for that MPN — as far as the live answer
actually speaks. An answer stating usable lifecycle/stock/lead
data replaces the whole offline part-risk family for the MPN
(including when the live part is healthy and the dataset flagged
it); an answer that states none of those fields replaces only
PART_NOT_IN_SAMPLE_DATA — silence about a field is not evidence
about it. Per-run budgets apply (200 MPNs, 120 s) and are counted
in Coverage when they bite. API keys come from
environment variables ONLY (`PREFLIGHT_DIGIKEY_CLIENT_ID`,
`PREFLIGHT_DIGIKEY_CLIENT_SECRET`, `PREFLIGHT_MOUSER_API_KEY`);
they are never accepted as flags, written to disk, logged or
printed. Responses are cached on disk (parsed fields only, 24 h)
and findings cite the data age. If the provider cannot answer
(no key, network failure, auth, rate limit), Coverage says
"online checks unavailable" with the reason class and the
offline verdict stands untouched. Without `--online` the online
module is never imported and a run makes zero network calls —
offline output is byte-identical.

| Rule | Severity | Trigger |
| --- | --- | --- |
| PART_OBSOLETE / PART_OUT_OF_STOCK / PART_NOT_RECOMMENDED / PART_LOW_STOCK / PART_LONG_LEAD_TIME | as offline | **Online variants:** the same rules and thresholds as the offline family, computed from live provider status/stock/lead time for the MPN (lead times stated in days are converted to weeks). Each supersedes the dataset finding for the same MPN. |
| ONLINE_PRICE | NOTE | Live unit price at the BOM order quantity; evidence gives the price, currency, the price break used and the quantity. A snapshot, not a quote. |
| ONLINE_PART_NOT_FOUND | NOTE | The MPN is not in the chosen provider's catalogue. Supersedes nothing — the offline dataset result for that MPN stands unchanged. |
| SEMANTIC_VALUE_MISMATCH | CAUTION | For a passive (R/C/L) BOM row, an attribute stated by BOTH the BOM and the live catalogue disagrees: the value (resistance/capacitance/inductance), tolerance, or chip package. Attributes stated on only one side are never compared — they are counted in Coverage as unstated, never inferred. |

## Official checks (kicad-cli, when installed)

| Rule | Severity | Trigger |
| --- | --- | --- |
| KICAD_ERC_VIOLATION | BLOCK/CAUTION | `kicad-cli sch erc` violation: error → BLOCK, warning → CAUTION. |
| KICAD_DRC_VIOLATION | BLOCK/CAUTION | `kicad-cli pcb drc` violation (including unconnected items / missing connections), same mapping. |
| KICAD_PARITY_CONFLICT | BLOCK/CAUTION | `kicad-cli pcb drc --schematic-parity` net-conflict: error → BLOCK, warning → CAUTION. Distinct from the native PARITY_* rules: KiCad's parity also fires on renamed nets, which the native layer reports separately as PARITY_NET_RENAMED (NOTE). If the parity run itself fails, Coverage + SCAN_INCOMPLETE say so. |
| KICAD_CLI_FAILED | CAUTION | kicad-cli is installed but a run failed (non-zero exit, no/invalid report, timeout). Its output is never trusted. Excluded/unrecognised severities are counted in Coverage, not mapped to findings. |

## Project KiCad settings (.kicad_pro) (v0.6)

Preflight reads the project's own KiCad settings — the ERC/DRC
severity maps and the per-instance exclusions stored in
`.kicad_pro` — and honours them in BOTH the official-checks layer
above and the corresponding native checks (ERC-lite via their
KiCad types: `pin_not_connected`, `power_pin_not_driven`,
`pin_to_pin`, `label_dangling`; geometry-lite via `clearance`,
`copper_edge_clearance`, `invalid_outline`; parity, severity
half only, via `net_conflict` — a project `net_conflict: ignore`
drops ALL native parity findings including PARITY_NET_RENAMED
notes: the project has turned KiCad's parity reporting off, and
the note is part of that reporting). A type the project set to
`ignore`, and an exclusion that matches a violation's item UUID
(and position, when the exclusion records one), waive the
finding; exclusions are never reinterpreted onto native findings
without item identity. Severity overrides remap native ERC-lite
severities (error → BLOCK, warning → CAUTION) — but ONLY where
the stated severity deviates from KiCad's factory default for
that type: KiCad writes the full default map into every project
file, and a default is not a project decision (the factory
`power_pin_not_driven = error`, for example, does not override
ERC_POWER_NOT_DRIVEN's documented CAUTION mapping). Every waiver
actually applied is counted and disclosed in Coverage
(`project_settings`); a `.kicad_pro` that exists but cannot be
read or parsed is reported, never silently treated as settings.
