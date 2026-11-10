# KiCad Preflight

Check a KiCad project *before* you export Gerbers and order the board —
and check a BOM from any EDA tool before you order the parts.

Every maker knows the sinking feeling: the boards arrive and a footprint
is wrong, a part was never placed, or the one chip the design needs went
obsolete six months ago. KiCad Preflight reads a KiCad project (or a
standalone BOM CSV) and gives you one plain-English report — **PASS**,
**CAUTION**, or **BLOCK** — from deterministic checks: schematic ↔ PCB
agreement, BOM sanity, and offline part-risk flags. No LLM decides the
verdict: the same project always gets the same answer, and every finding
names the exact part and the exact values that disagree, so you can
verify it by hand in seconds.

Preflight is free and open source (MIT). There is no hosted tier and no
paid version — the whole tool lives in this repository.

## 30-second demo

```console
$ python3 -m preflight samples/broken
Preflight report for: LED Board (broken fixture)

Findings: 7 (5 BLOCK, 2 CAUTION, 0 NOTE)
  [BLOCK] SCH_PART_NOT_ON_PCB — led-board.kicad_sch · R3
      R3 (330R) is in the schematic but has no footprint on the PCB. ...
  [BLOCK] FOOTPRINT_MISMATCH — led-board.kicad_sch · D1
      1 part(s) (D1): the schematic asks for footprint
      'LED_SMD:LED_0805_2012Metric' but the PCB has
      'LED_SMD:LED_1206_3216Metric'. ...
  [BLOCK] BOM_QUANTITY_MISMATCH — bom.csv line 5
      BOM line 5 says quantity '3' but lists 2 reference(s) (D1, D2). ...
  [BLOCK] BOM_UNKNOWN_REFERENCE — bom.csv line 7
      The BOM lists C9, but there is no C9 in the schematic. ...
  [BLOCK] PART_OBSOLETE — bom.csv line 5 · LTST-C170KGKT
      LTST-C170KGKT (D1, D2) is marked 'obsolete' in the
      Preflight sample distributor dataset (snapshot 2026-11-09). ...
  [CAUTION] BOM_MISSING_MPN — bom.csv line 6
      BOM line 6 (SW1) has no manufacturer part number and no
      LCSC/JLCPCB part number. ...
  [CAUTION] PART_LOW_STOCK — bom.csv line 3 · RC0603FR-071KL
      ... shows only 8 in stock ... fewer than the 50-piece comfort line. ...

VERDICT: BLOCK
$ echo $?
2
$ python3 -m preflight samples/good     # the same board, fixed
VERDICT: PASS                           # exit 0, zero findings
```

(The samples are hand-authored fixtures — see "Test corpus". They are
only ever parsed, never opened in KiCad or fabricated.)

## KiCad-first, not KiCad-only

KiCad is the wedge, by design: it is the free, open tool whose users
feel these mistakes most, and its Plugin and Content Manager is a real
distribution channel. But the checks are not KiCad-only:

- The **KiCad parser** reads `.kicad_sch` / `.kicad_pcb` (KiCad 6, 7
  and 8 formats, hierarchical sheets included).
- The **BOM checks are tool-agnostic** — they live in their own module
  (`preflight/bomchecks.py`, fed by `preflight/bom.py`) and run on any
  BOM CSV. A BOM exported from Altium, Eagle/Fusion, EasyEDA or a fab
  house (JLCPCB style included) has the same classes of problems:

```console
$ python3 -m preflight production-bom.csv     # standalone BOM mode
```

- The planned **order-page browser extension** (JLCPCB/PCBWay
  checkout) is the later, any-tool front door. The full reasoning is
  in `EXTENSIONS_AND_GITHUB.md`.

## Two front doors, one engine

1. **This CLI (now)** — scriptable, testable without a GUI, runs in CI,
   works anywhere Python 3 runs.
2. **A KiCad in-app plugin (in development)** — one click inside
   KiCad at the moment before Gerber export, wrapping this same
   engine, distributed through KiCad's **Plugin and Content Manager
   (PCM)**. A skeleton package (identifier
   `com.github.RakTikoo.kicad-preflight`) lives in `pcm/`; it has not
   been run inside a real KiCad yet, and it says so.

## Install / usage

Pure Python 3 standard library (3.10+). No `pip install`, no
dependencies. KiCad itself is **not** required — see "Official checks".

```bash
python3 -m preflight <project-folder>                 # text report
python3 -m preflight <project-folder> --format md     # one-page Markdown
python3 -m preflight <project-folder> --format json   # machine-readable
python3 -m preflight <project-folder> --output report.md --format md
python3 -m preflight <bom.csv>                        # standalone BOM mode
python3 preflight.py <project-folder>                 # shim, same thing
```

The target can be a project folder or a single `.kicad_pro` /
`.kicad_sch` / `.kicad_pcb` file inside one. A BOM CSV in the folder is
auto-detected — candidates are tried in name order and the first whose
header actually parses as a BOM wins, so a pick-and-place CSV is never
mistaken for the BOM (or pass `--bom`).

Exit codes: `0` PASS · `1` CAUTION · `2` BLOCK · `3` usage error.
The CLI never prints a traceback; bad inputs exit 3 with a message.

## What it checks

The full rule list with severities is in `docs/RULES.md`. In short:

**Schematic ↔ PCB** (parsed from KiCad's S-expression files, KiCad 6
`fp_text` and KiCad 7/8 `property` styles alike): parts in the
schematic with no footprint on the PCB and vice versa, footprint
mismatches (aggregated per footprint pair), duplicate references,
two different parts sharing one reference, parts with no footprint /
no value / a placeholder MPN, unannotated symbols. Board furniture —
mounting holes marked `board_only`, silkscreen logos and labels with
generated references — is recognised as furniture, not flagged as
parts. Preflight does not check pins or nets itself; that is what
DRC/ERC are for (below).

**BOM ↔ schematic** (KiCad, JLCPCB and generic header spellings):
references that don't exist, schematic parts missing from the BOM,
DNP / not-in-BOM parts that snuck into the BOM, duplicate references,
quantity cells that disagree with the designator list (`"R1,R2,R5"`
lists and `R1-R3` / `R1-3` ranges are expanded), missing or placeholder
MPNs (an LCSC/JLCPCB part number counts as identified), blank-reference
rows, BOM footprint/value disagreements with the schematic, and one
part number used for two different parts.

**Fabrication readiness** (v0.3): a missing board outline (BLOCK —
nothing to cut), copper zones with no fill stored, and measured board
values against the published standard-process minimums of JLCPCB and
PCBWay — track width, drill and via sizes, annular rings, plated slot
widths (CAUTION: below-standard means an engineering question or an
advanced process, not an automatic rejection). If a position/CPL file
sits in the folder, its designators are checked against the board
(assembly orders). Files written by KiCad 9/10 are detected from
their format stamps and flagged honestly: newer constructs
(variants, pad stacks, embedded files) are not fully parsed, so those
files never pass in silence.

**Geometry-lite** (v0.4): a conservative distance-checking subset —
not DRC. Exact pad/track/arc/via shapes are measured for
different-net clearance and copper-to-edge distance against the
board's own stated design rules (`.kicad_pro` clearances and
netclasses) or, when the board states none, the published fab
minimums; and the Edge.Cuts outline is chained to prove it closes.
What cannot be measured exactly is never approximated into a
finding: zones, custom/trapezoid pads and objects with no stated
net are excluded and listed in Coverage instead.

**Gerber package** (v0.4): point Preflight at the folder you are
about to upload to the fab and it checks the package itself —
top/bottom copper, masks, silkscreen, outline and drill file
present, no zero-byte or duplicated layers, a consistent layer
stack, one naming scheme (KiCad defaults, Protel extensions, or
generic names). Auto-detected for fab-output folders, or forced
with `--gerber`. File-level only: Gerber contents are never parsed.

**Part risk (offline)**: obsolete/EOL, out-of-stock, low stock, long
lead time, single-source, not-recommended-for-new-designs — per BOM
MPN against the bundled dataset (see the honesty note). Numbers in a
user dataset may be strings or floats, as real exports produce; they
are coerced, not ignored.

## Coverage: a PASS only covers what was checked

Every report ends with a Coverage section: files parsed (all sheets of
a hierarchical design), parts and footprints counted, board furniture
excluded, BOM rows, MPNs checked, parse errors, and the kicad-cli
status. If something that exists could not be checked — an unloaded
extra project file, a failed kicad-cli run, a missing BOM — the report
carries a `SCAN_INCOMPLETE`, `BOM_MISSING` or stronger finding, so the
verdict reflects it. There are no silent skips.

## Official checks (kicad-cli)

If KiCad's `kicad-cli` is on your PATH, Preflight also runs the
official **DRC** (PCB) and **ERC** (schematic) and folds their
violations in (error → BLOCK, warning → CAUTION; a failed run's output
is never trusted, and user-excluded/unknown severities are counted in
Coverage, not silently mapped). If kicad-cli is absent, those checks
are skipped **and the report says so**. The built-in text checks work
either way.

## Budgets and untrusted input

Project folders are untrusted input, so scanning is budgeted: files
must be regular files (a FIFO will not hang the tool) and ≤ 64 MB;
BOMs and position files ≤ 20,000 rows and ≤ 1M characters per field;
reports cap at 2,000 findings with an explicit truncation note. Report output is
sanitised — crafted part values cannot forge a verdict line or break
the Markdown table. Files are decoded as strict UTF-8; corrupt bytes
are a parse error, never silently rewritten.

## An honest note on part data

The part-risk layer ships with a small **sample distributor dataset**
(`preflight/data/parts.json`, snapshot 2026-11-09) so it works offline
with no API keys. Its statuses and stock numbers are **illustrative,
not live distributor data** — every part-risk finding cites the dataset
and snapshot date, and an MPN absent from the dataset is reported as
"could not check", never guessed at. Supply your own snapshot with
`--dataset` (shape-validated; a bad dataset is a usage error, exit 3).

## Test corpus

- `samples/` — hand-authored fixtures in real KiCad formats:
  `good/` (PASS, zero findings), `kicad6/` (the same board in KiCad 6
  file format; PASS), `v9/` (the good board re-stamped as KiCad 9;
  CAUTION with exactly the two honest version warnings), `broken/`
  (seven deliberate, hand-verifiable errors; BLOCK with exactly the
  seven documented findings), and the v0.4 fixtures: `geo-ok/`
  (pads exactly at the stated clearance; geometry silent),
  `geo-violation/` (clearance + copper-to-edge findings),
  `geo-open/` (an unclosed outline; BLOCK), `gerber-good/`
  (a complete fab package; PASS), `gerber-incomplete/` and
  `gerber-inner-broken/` (package caution/BLOCK sets).
- `corpus/` — **real open-source boards** (Upsy Desky, the ESP32 Rust
  board, Piantor) with sources, commits and licences recorded in
  `corpus/SOURCES.md`, plus a labelled broken corpus built by mutating
  copies (`corpus/build_broken.py`). The corpus is downloaded
  separately; `run_corpus_tests.py` skips cleanly without it.

```bash
python3 run_tests.py           # samples
python3 run_corpus_tests.py    # open-source corpus + mutations
python3 run_edge_tests.py      # hostile inputs (from the adversarial review)
```

## How this version was reviewed

v0.1 was put through an independent multi-agent adversarial review —
a red team (hostile inputs), a false-positive tester (clean real
boards), and a code/security auditor — and failed it instructively
(100% of its BLOCKs on clean boards were false alarms; 7 crashes and
a FIFO hang on hostile inputs). v0.2 is the fix round: every reviewer
finding is either fixed or documented as an accepted gap, and the
reviewers' own test sets are the regression suites above. The full
record, with before/after numbers, is kept with the project.
v0.3 (the coverage wave) went through the same three-reviewer
discipline for its new rules; `docs/COVERAGE.md` maps every pre-order
failure class we know of against this tool and the open-source
alternatives, gaps included.
v0.4 (geometry-lite + Gerber package checks) went through the same
discipline again: the three reviewers produced 21 red-team cases, a
clean-corpus verdict that failed on exactly two real bugs, and a
worst-case runtime audit that refuted the first build's budget
claims on crafted inputs. Every confirmed finding was fixed or
documented as an accepted limit; the reviewers' inputs are now
regression tests in the edge suite (§17).

## Known limits

- **No live distributor data** — part risk is only as real as the
  dataset supplied (see above).
- **kicad-cli is optional** — without it there is no official DRC/ERC;
  Coverage always says which mode you got.
- **Geometry is a subset, honestly labelled** — v0.4 measures
  clearances and copper-to-edge for pads/tracks/arcs/vias with
  exact shapes, and proves outline closure; it does not check
  zones, custom/trapezoid pads, mask/silk/courtyard geometry,
  holes, or connectivity. Full spatial DRC remains kicad-cli's
  job (above), and `docs/COVERAGE.md` maps exactly which failure
  classes the subset leaves open. Gerber package checks are
  file-level only — a zipped package must be extracted first, and
  Gerber/drill contents are never parsed.
- **One project per folder** — hierarchical sheets of one project are
  followed; a second project sharing a folder is reported via
  `SCAN_INCOMPLETE`, not merged.
- **Legacy formats** — KiCad ≤5 `.sch` files are reported as
  unsupported, not silently misread.
- **Plugin untested in KiCad** — the `pcm/` skeleton has not run
  inside a real KiCad; its test plan is in `pcm/README.md`.

## Licence

MIT — see `LICENSE`. Copyright (c) 2026 RakTikoo.
