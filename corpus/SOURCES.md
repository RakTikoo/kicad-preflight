# Open-Source Test Corpus — Sources and Licences

All real-board test data for KiCad Preflight comes from genuinely
open-source KiCad projects published on GitHub. Nothing here was written
by the Preflight team except the labelled mutations in `broken/`.

Download method (2026-11-09): `git clone` through this machine's proxy
timed out repeatedly, so each project was downloaded as a tarball of an
exact commit from codeload.github.com
(`https://codeload.github.com/<owner>/<repo>/tar.gz/<commit-sha>`).
The commit SHA and tarball SHA-256 are recorded per project, so the
corpus is byte-reproducible. Tarballs are kept in `downloads/`,
extracted trees in `projects/`.

## 1. Upsy Desky — tjhorner/upsy-desky
- URL: https://github.com/tjhorner/upsy-desky
- Commit: c64216b91ca818b0f9a1a1ec3c6ba92dcc5f7056 (2026-08-22)
- Tarball SHA-256: daf06e9ef9313cae1d301e130d13d67e3f99d4d4abe812d6890b5998fa8f63f9
- Licence: **MIT** (`LICENSE-MIT`, "Copyright 2022 TJ Horner — Permission
  is hereby granted, free of charge…") for software/PCB, plus
  `LICENSE-CC` (Creative Commons) for other content. Read in full from
  the extracted tree on 2026-11-09.
- Board used: `projects/upsy-desky/pcb/` — upsy-desky.kicad_sch +
  upsy-desky.kicad_pcb (KiCad 7/8 format: footprint references stored as
  `(property "Reference" …)`). No BOM CSV ships in the repo's pcb folder.

## 2. ESP32 Rust Board — esp-rs/esp-rust-board
- URL: https://github.com/esp-rs/esp-rust-board
- Commit: efe1e8ad5c6dbc23b4365fef35af27545bc1381d (2025-05-20)
- Tarball SHA-256: d86344e70a57527724d27caecea2e6d535c00db77b9eafd5b5061f23ecb1adc4
- Licence: **CERN Open Hardware Licence v2 — Permissive**
  (`LICENSE-CERN-OHL`, first line read from the extracted tree:
  "CERN Open Hardware Licence Version 2 - Permissive").
- Board used: `projects/esp-rust-board/hardware/esp-rust-board/` —
  esp-rust-board.kicad_sch + esp-rust-board.kicad_pcb (PCB file version
  20211014 = KiCad 6 format: footprint references stored as
  `(fp_text reference …)`) + a real BOM, `esp-rust-board.csv`.

## 3. Piantor — beekeeb/piantor
- URL: https://github.com/beekeeb/piantor
- Commit: cd847afb4f9a86e8c1c8e36f243140644013afb2 (2023-08-23)
- Tarball SHA-256: 2e1e19fb18578a24356f35002e663d42c05bf3a26bd4fab3ae821dec4b5eb07d
- Licence: **GNU GPL v3** (`LICENSE`, "GNU GENERAL PUBLIC LICENSE
  Version 3, 29 June 2007").
- Boards used: `projects/piantor/pcb/left/` and `projects/piantor/pcb/right/`
  — each keyboard_pcb.kicad_sch + keyboard_pcb.kicad_pcb (KiCad 6 format).
  No BOM CSV ships with the boards.

## Considered and rejected
- **KCORES/KCORES-CSPS-to-ATX-Converter** (commit c1355bfc…): downloaded
  and inspected, then **dropped**. Its `LICENSE` is a custom, non-standard
  "KCORES License v1.0" (Chinese text) — not a recognised open licence,
  which fails this corpus's "clear open licence" rule. Its schematic is
  also a legacy KiCad ≤5 `.sch` file, outside Preflight's supported
  formats. Deleted from the corpus 2026-11-09; recorded here so the
  rejection is auditable.
- **Hanqaqa/PCB_Business_Card** (GPL-3.0): licence is fine, but the repo
  ships only a decorative `.kicad_pcb` with no schematic, so it cannot
  exercise schematic↔PCB checks. Not used.

## Baseline results on the CLEAN projects (v0, before fixes, 2026-11-09)
Full outputs in `results-before/`.

| Project | v0 verdict | Findings |
| --- | --- | --- |
| upsy-desky | BLOCKED (exit 2) | 3 (3 BLOCK) |
| esp-rust-board | BLOCKED (exit 2) | 82 (55 BLOCK, 27 FIX-FIRST) |
| piantor-left | BLOCKED (exit 2) | 33 (33 BLOCK) |
| piantor-right | BLOCKED (exit 2) | 33 (33 BLOCK) |
| samples/good (synthetic) | READY (exit 0) | 0 |

Root cause of most clean-board BLOCKs (confirmed by direct inspection):
the v0 PCB parser only recognised KiCad 7/8-style
`(property "Reference" …)` footprint references. On KiCad 6 boards
(esp-rust-board, piantor) it parsed **0 footprints**, so every schematic
part was flagged SCH_PART_NOT_ON_PCB. On upsy-desky, non-part footprints
with unannotated references (`REF**`, e.g. logos) were flagged as
duplicates/strays. See the review folder for the adversarial work that
followed from this baseline.

## Re-run after the v0.2 fix round (2026-11-09)

The v0.1 verdicts above were the review's "before" state (124/124 BLOCK
findings on these boards were false positives — see the adversarial
review record). After the fix round (engine 0.2.0):

| Project | Verdict | Findings |
| --- | --- | --- |
| upsy-desky | CAUTION | BOM_MISSING only (the board ships no BOM — fair). |
| esp-rust-board | CAUTION | BOM_MISSING_MPN for U2 (the census's one true finding) + 1 NOTE. |
| piantor left/right | BLOCK | 8 findings, all hand-verified true: duplicate H1/H4 mounting-hole designators (upstream files really contain two H1 and two H4), 4 aggregated genuine footprint mismatches, BOM_MISSING, NOTE. |

`run_corpus_tests.py` pins these expectations; the broken corpus still
fires 6/6 labelled rules.
