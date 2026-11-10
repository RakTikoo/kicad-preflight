# KiCad Preflight — Rule Reference

Every deterministic rule in v0.2, its severity, and what triggers it.
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

BOM parsing notes: header aliases cover KiCad, JLCPCB (`Comment /
Designator / Footprint / LCSC Part #`) and generic spellings; reference
ranges `R1-R3`, `R1-3` and `R1 - R3` are expanded (≤200 per range).

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
