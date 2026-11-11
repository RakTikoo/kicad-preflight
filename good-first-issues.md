# Good first issues — seed list

Each draft has a one-line title and the pointer a new contributor needs.
(Repo hygiene, not promotion: under the H-1B gate
in ~/workspace/agentic-spikes/H1B_GATE.md, growth is organic only.)

Status (2026-11-11): the code push landed long ago, so the drafts are
postable. Item 1 is live as issue #3 and item 2 as issue #4. Item 6
(hierarchical-sheet sample) is done in substance by
`samples/netlist-hier/` (v0.5) and is kept below for the record.
Remaining drafts stay on file until there is a reason to post them.

1. **Add a KiCad 6 sample project to `samples/`**
   The shipped fixtures are KiCad 7/8-style. Add a minimal clean board
   whose PCB uses `(fp_text reference …)` footprints, with test
   expectations in `run_tests.py`.

2. **EasyEDA BOM header aliases**
   `preflight` BOM parsing maps header aliases in one table. Add the
   EasyEDA BOM export spellings (with a sample CSV proving a standalone
   BOM scan works) — the BOM checks are tool-agnostic by design.

3. **Altium BOM header aliases**
   Same table, Altium Designer BOM export spellings + sample CSV.

4. **`--format sarif` renderer**
   Reports already render text / md / json. Add SARIF output so
   findings can land in GitHub code-scanning. Keep other formats
   byte-identical.

5. **A `--quiet` flag**
   Print only the verdict line (exit codes unchanged) for CI scripts.

6. **Hierarchical-sheet sample project**
   A two-sheet synthetic project (root sheet + one sub-sheet file) that
   must scan as one project — fixture plus expectations.

7. **Translate finding messages (i18n scaffolding)**
   Finding texts are plain-English strings. Extract them into one
   module so translations can be added without touching check logic.

8. **Footprint-library allowlist file**
   Projects trust different footprint libraries. Add a
   `.preflight-allow` style config listing extra library prefixes that
   should not raise UNKNOWN_FOOTPRINT_LIBRARY, with tests both ways.
