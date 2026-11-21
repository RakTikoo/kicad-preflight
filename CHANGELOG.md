# Changelog

All notable changes to KiCad Preflight, newest first. Versions are the
`__version__` in `preflight/__init__.py` and the PCM package metadata.

## Unreleased

- Standalone BOM parsing now recognises Altium Designer's primary supplier
  columns (`Supplier 1` / `Supplier Part Number 1`) and keeps the per-row
  supplier rule intact, with a synthetic sample and focused tests.

## 0.7.1 — 2026-11-19

- **GitHub Marketplace:** the Action's `action.yml` description is
  shortened to fit the Marketplace 125-character limit, so the
  Action can be listed on the GitHub Marketplace. No check
  behaviour changes.

- **Installable with pip from GitHub.** A `pyproject.toml` makes
  `pip install git+https://github.com/RakTikoo/kicad-preflight` work
  and provides the `kicad-preflight` command; the README's Install
  section now leads with the clone / pip routes it previously
  skipped.
- **GitHub Action.** A composite `action.yml` at the repo root runs
  Preflight in CI (`path`, `format`, `fail-on-block`,
  `python-version` inputs; `verdict` / `exit-code` outputs) — the
  last of the workshop's public tools to gain one. CAUTION warns
  but never fails; a run that could not execute fails loudly.
- **PCM install walkthrough.** The README and `pcm/README.md` now
  tell a KiCad user exactly how to install the release PCM zip
  (Plugin and Content Manager → Install from File…), with the
  stock-install discovery check still honestly marked pending.
- **Public library API.** `preflight/__init__.py` now re-exports
  `run`, `run_bom_only`, `run_gerber_package` and `Report`, with a
  "Use as a library" section in the README.

## 0.7.0 — 2026-11-14

- **SARIF output** (`--format sarif`): SARIF 2.1.0 for GitHub code
  scanning and other SARIF consumers. Results cross-check 1:1 against
  the `--format json` findings (same order, same rule ids; severity
  maps BLOCK → error, CAUTION → warning, NOTE → note), rule metadata
  (documented severity + trigger text) comes from
  `preflight/ruleinfo.py`, generated from `docs/RULES.md` and pinned
  to it by the test suite, and locations are resolved from each
  finding's `where` (artifact URI, plus a line region for BOM rows)
  when it names a file — never invented when it doesn't. The text,
  Markdown and JSON renderers are byte-identical to before.

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
