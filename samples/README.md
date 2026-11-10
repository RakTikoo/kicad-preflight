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

## `real/` — optional, not asserted

If real open-source KiCad projects were fetched during development, they
live here with their source and licence noted. They are smoke tests only
(the CLI must produce a report and a defined exit code); their findings are
third-party projects' real state and are **not** hand-verified, so
`run_tests.py` does not assert their verdicts.
