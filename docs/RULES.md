# KiCad Preflight — Rule Reference

Every deterministic rule in v0.3, its severity, and what triggers it.
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
| SCAN_INCOMPLETE | CAUTION | Extra KiCad files in the folder were not loaded (a second project), or dataset entries were unusable. |
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
parser extracts (pads, tracks, arcs, vias) — explicitly NOT KiCad
DRC. Shapes are exact (circles, rectangles at their real rotation,
ovals, arcs subdivided with a bounded error that is only ever
counted against the measurement, roundrects as their true core
rectangle + corner radius). Thresholds come first from the board
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

Never measured, always stated in Coverage: zones (their clearance
is set by KiCad's zone filler, not netclass rules), custom and
trapezoid pads (no exact outline is derivable), footprint-internal
graphics, and any pair where either object's net is unknown — an
unstated net or net 0 is never guessed, so same-footprint pins with
no assigned net cannot produce a finding. Pad positions and
orientations use KiCad's own conventions as written in the file
(pad orientation is stored board-absolute; back-side footprints are
saved pre-mirrored) — both verified against same-net track
endpoints and design-clearance landings on real boards.

| Rule | Severity | Trigger |
| --- | --- | --- |
| GEOMETRY_CLEARANCE_BELOW_MIN | CAUTION | Copper objects on the same layer belonging to different nets sit closer than the applicable clearance (board rules first, else the 0.10 mm fab minimum). One aggregated finding naming the worst pair. |
| GEOMETRY_COPPER_TO_EDGE | CAUTION | A copper object sits closer to the Edge.Cuts outline than the applicable minimum (board's stated edge clearance, else 0.20 mm). Edge-mounted connectors are the classic intentional case — the finding asks for a look, it does not claim an error. |
| BOARD_OUTLINE_OPEN | BLOCK | Root-level Edge.Cuts lines/arcs/curves whose endpoints do not pair up within 0.01 mm, with no closed loop anywhere on the layer — the outline does not close and cannot be cut as drawn (this includes an outline shattered at every corner into near-miss fragments). Self-closed items (circles, rects, polys) always pass; open chains ALONGSIDE a closed loop never fire (they are routed slots, a legal construct), nor does a lone open element; an endpoint pattern too close to the tolerance to judge abstains instead of firing. Legacy `(angle ...)` arcs are chained under every reading real files use (centre-in-`(start)`, both sweep directions, and endpoints-in-`(start)`); conflicting readings abstain. |
| GEOMETRY_SKIPPED | CAUTION | The geometry layer abstained: more than 40,000 copper objects, or the pair-measurement budget (2,000,000) was exceeded. The other layers are unaffected; KiCad DRC covers the board. |

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

## Official checks (kicad-cli, when installed)

| Rule | Severity | Trigger |
| --- | --- | --- |
| KICAD_ERC_VIOLATION | BLOCK/CAUTION | `kicad-cli sch erc` violation: error → BLOCK, warning → CAUTION. |
| KICAD_DRC_VIOLATION | BLOCK/CAUTION | `kicad-cli pcb drc` violation, same mapping. |
| KICAD_CLI_FAILED | CAUTION | kicad-cli is installed but a run failed (non-zero exit, no/invalid report, timeout). Its output is never trusted. Excluded/unrecognised severities are counted in Coverage, not mapped to findings. |
