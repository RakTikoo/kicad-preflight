# Sample projects

These are **hand-authored fixture projects**, written as text in the KiCad 8
S-expression formats (`.kicad_sch` / `.kicad_pcb`) with the same structure
KiCad itself writes — placed symbols with Reference/Value/Footprint
properties, footprint instances with pads, a `.kicad_pro`, and a KiCad-style
BOM CSV. They are *structurally real, graphically minimal*: the electrical
content (pins, nets, connectivity) is illustrative, not a board you should
fabricate. They exist so Preflight's checks can be tested end-to-end with
hand-verifiable expectations, the same discipline as SkillScan's sample
skills. Nothing here is executed; the files are only parsed.

## `good/` — expected PASS (exit 0, zero findings)

A tiny battery LED board: J1 (battery connector), R1 (330R), D1/D2 (red
LEDs), SW1 (slide switch). Schematic, PCB, and BOM agree exactly, and every
BOM part is a healthy entry in the sample distributor dataset
(active, well stocked, multi-source, short lead).

## `kicad6/` — expected PASS (exit 0, zero findings)

The same tiny board written in the KiCad 6 file format: PCB footprints
carry `fp_text` references instead of `property` nodes, and schematic
symbols use `property` nodes without per-instance `instances` blocks.
Added after the adversarial review showed v0.1 could not read KiCad 6
boards at all (it flagged every part on them as missing).

## `broken/` — expected BLOCK (exit 2, exactly 7 findings)

The same style of board with seven deliberate, hand-verifiable errors:

| # | Planted error | Expected rule | Severity |
| --- | --- | --- | --- |
| 1 | R3 is in the schematic (and BOM) but has no footprint on the PCB | `SCH_PART_NOT_ON_PCB` | BLOCK |
| 2 | D1's schematic footprint is `LED_0805_2012Metric` but the PCB has `LED_1206_3216Metric` | `FOOTPRINT_MISMATCH` | BLOCK |
| 3 | The BOM lists C9, which does not exist in the schematic | `BOM_UNKNOWN_REFERENCE` | BLOCK |
| 4 | The D1,D2 BOM row says quantity 3 but lists 2 references | `BOM_QUANTITY_MISMATCH` | BLOCK |
| 5 | The LED's MPN (`LTST-C170KGKT`) is obsolete in the sample dataset | `PART_OBSOLETE` | BLOCK |
| 6 | SW1's BOM row has a blank MPN | `BOM_MISSING_MPN` | CAUTION |
| 7 | R1's MPN (`RC0603FR-071KL`) has only 8 in stock in the sample dataset | `PART_LOW_STOCK` | CAUTION |

If `run_tests.py` reports anything else for this folder, either the fixture
or the checker changed — that is what the test is for.

## `v9/` — expected CAUTION (exit 1, exactly 2 findings)

The `good/` board with its file-format stamps changed to KiCad 9
(schematic format 20250114, board format 20241229, generator_version
"9.0"). The design itself is unchanged and healthy; the two expected
findings are both `KICAD_VERSION_UNTESTED`, one per file — v0.3's
honest handling of KiCad 9/10 files, whose newer constructs (embedded
files, tables, pad stacks, design variants) the parser does not fully
understand. A silent PASS here would be a lie; a BLOCK would be one
too.

## `geo-ok/` — v0.4 geometry-lite: silence expected from the geometry rules

A PCB-only fixture (other layers' findings are not asserted): two
1×1 mm pads of different nets exactly 0.200 mm apart — the clearance
the fixture's own `.kicad_pro` states — a closed rectangular
outline, and one lone Edge.Cuts line (a routed slot). No
GEOMETRY_* finding and no BOARD_OUTLINE_OPEN may fire.

## `geo-violation/` — v0.4: GEOMETRY_CLEARANCE_BELOW_MIN + GEOMETRY_COPPER_TO_EDGE

Same board with the pads 0.05 mm apart, plus a third pad 0.10 mm
from the west edge (below the 0.20 mm published minimum the board
falls back to, stating no edge clearance of its own).

## `geo-open/` — v0.4: BOARD_OUTLINE_OPEN (BLOCK)

The geo-ok board with the west side of the outline deleted: three
chained lines whose endpoints do not pair up.

## `gerber-good/` — v0.4 package mode: PASS, zero findings

A complete KiCad-style fabrication package (top/bottom copper,
masks, silkscreens, outline, drill file, job file) with dummy
one-line contents — package checks are file-level, so contents are
never read. Auto-detected as a package (no KiCad files present).

## `gerber-incomplete/` — v0.4: exactly 4 CAUTIONs

Top copper, outline and drill present; the top mask file is
zero-byte (GERBER_EMPTY_FILE), and bottom copper, bottom mask and
silkscreen are absent (GERBER_BOTTOM_COPPER_MISSING,
GERBER_MASK_MISSING, GERBER_SILKSCREEN_MISSING).

## `gerber-inner-broken/` — v0.4: BLOCK

Protel-style package with inner copper (.g1/.g2) but no bottom
copper and no outline: GERBER_INNER_WITHOUT_BOTTOM +
GERBER_OUTLINE_MISSING (BLOCKs) plus drill/mask/silkscreen
cautions.

## `netlist-clean/` — v0.5 netlist: PASS, zero findings

A small matched schematic/PCB pair: connector → regulator → load,
GND power symbols plus a PWR_FLAG, local labels, and PCB pads whose
net membership matches the schematic exactly. Exercises ERC-lite
and parity silence on a healthy design.

## `netlist-hier/` — v0.5 netlist: PASS, zero findings

A root sheet with one sub-sheet: a sheet pin bound to a
hierarchical label inside, a global label crossing both sheets, a
power symbol in the sub-sheet, and PCB nets named the way KiCad
writes them (`/Sub/FEED`, `GND`, `SENSE_G`). Proves the hierarchy
plumbing end to end.

## `netlist-broken/` — v0.5 netlist: BLOCK, each fault exactly once

Planted faults: two drivers shorted on VOUT
(ERC_OUTPUT_CONFLICT), a power net with no power-output driver
(ERC_POWER_NOT_DRIVEN), one unconnected pin (ERC_UNCONNECTED_PIN;
a second pin carries a no-connect marker and stays silent), one
pad moved to a different net on the PCB
(PARITY_NET_MEMBERSHIP on both affected nets), one schematic-only
net (PARITY_NET_SCH_ONLY), and one renamed net with identical
membership (PARITY_NET_RENAMED, NOTE). One unrelated
PART_NOT_IN_SAMPLE_DATA NOTE comes from the sample parts dataset.

## `real/` — optional, not asserted

If real open-source KiCad projects were fetched during development, they
live here with their source and licence noted. They are smoke tests only
(the CLI must produce a report and a defined exit code); their findings are
third-party projects' real state and are **not** hand-verified, so
`run_tests.py` does not assert their verdicts.
