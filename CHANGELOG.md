# Changelog

All notable changes to KiCad Preflight, newest first. Versions are the
`__version__` in `preflight/__init__.py` and the PCM package metadata.

## 0.6.0 — 2026-11-10

- KiCad bridge hardened against real `kicad-cli` 8.0.9 output,
  including schematic-parity findings as a distinct rule
  (`KICAD_PARITY_CONFLICT`).
- Project settings: `.kicad_pro` ERC/DRC severities and waivers are
  honoured by both the adapter and the native checks, and disclosed in
  the Coverage section. Remapping applies only where a project
  deviates from KiCad's factory defaults.
- Opt-in online mode (`--online`, off by default): live stock,
  lead-time, lifecycle and price notes from DigiKey and Mouser using
  your own API keys (environment variables only), plus semantic
  value↔MPN checks for passives. Failures degrade to one honest
  Coverage note; the offline verdict never changes. Without
  `--online` the tool makes zero network calls.
- Engine depth: filled zones join the geometry checks; schematic↔PCB
  parity covers trapezoid and modelled custom pads; the ERC pin-type
  matrix is complete (`ERC_PIN_CONFLICT`). 88 rules.

## 0.5.0 — 2026-11-10

- Netlist extraction from schematics (wires, labels, power symbols,
  hierarchical sheets) powering ERC-lite checks: unconnected pins,
  power nets with no driver, output-to-output conflicts, dangling
  labels.
- Schematic↔PCB net parity: membership differences BLOCK, renames are
  noted. 83 rules.
- Labelled-mutation benchmark: 16/16 planted faults caught at the
  expected severity, zero false firings on clean controls.

## 0.4.0 — 2026-11-10

- Geometry-lite: different-net copper clearance and copper-to-edge
  distance measured against the board's own netclasses or published
  fab minimums, with honest abstention when a shape can't be measured
  exactly.
- Gerber package completeness: required layers, drill files, and
  empty/placeholder files checked at the package level before upload
  to a fab. 75 rules.

## 0.3.0 — 2026-11-10

- `docs/COVERAGE.md`: a map of 38 pre-order failure classes — what is
  covered, what is partial, and what is deliberately left to KiCad —
  kept honest by the benchmark results.
- KiCad 9/10 format stamps are detected and warned about rather than
  silently claimed as supported. 58 rules.

## 0.2.0 — 2026-11-09

- First public release. Deterministic pre-order checker for KiCad
  6–8 projects and for BOM CSVs from any EDA tool: schematic↔PCB
  agreement, BOM sanity, offline part-risk flags from a labelled
  sample dataset.
- One verdict — PASS / CAUTION / BLOCK — with a Coverage section in
  every report stating what was not checked. Text, Markdown and JSON
  reports; exit codes for CI.
- Optional `kicad-cli` adapter folds in KiCad's official DRC/ERC when
  KiCad is installed; the tool runs fine without it.
- PCM plugin package for in-KiCad use; pure Python standard library,
  no dependencies.
