# KiCad Preflight — Coverage Map

What this tool checks, what it partially checks, and what it does not
check, class by class — compared with the open-source tools a KiCad
user is most likely to reach for instead. Written 2026-11-10 for
v0.3.0, after a competitive study of the tools below (concepts and
gaps only — no code, rule text, or test corpora were taken from any
of them) and the official JLCPCB/PCBWay capability pages.

Status key:

- **Covered** — a deterministic rule checks this class and is pinned
  by the test suites.
- **Partial** — a rule touches the class but with a stated limit
  (property-level instead of geometric, offline data instead of
  live, syntactic instead of semantic).
- **Gap** — Preflight does not check this class. Gaps are published
  plainly; several exist because they need net/connectivity parsing,
  geometric computation, live catalogue data, or KiCad itself — all
  outside this tool's stated architecture (pure-stdlib text parsing,
  no install, deterministic).

No "first / only / best" claims are made anywhere in this document.

## Failure classes

| # | Pre-order failure class | Preflight v0.3 | Also covered by |
| --- | --- | --- | --- |
| 1 | Schematic part missing from PCB (or PCB part not in schematic) | **Covered** | KiCad (parity), KiBot |
| 2 | Schematic↔PCB footprint disagreement | **Covered** | KiCad (parity) |
| 3 | Duplicate reference designators (schematic or PCB) | **Covered** | KiCad (annotation/ERC, partially) |
| 4 | Multi-unit part conflicts / divergent units | **Covered** | — |
| 5 | Unannotated symbols (no reference) | **Covered** | KiCad ERC |
| 6 | Hierarchical sub-sheet missing or unloadable | **Covered** | KiBot |
| 7 | Project file unparseable / unsupported legacy format | **Covered** | — |
| 8 | File written by an untested KiCad version (9/10, pre-6, malformed stamp) | **Covered** (warns; newer constructs not fully parsed) | KiCad itself |
| 9 | BOM quantity ≠ reference count | **Covered** | — |
| 10 | BOM reference not in the schematic | **Covered** | KiBot (regenerated BOMs only) |
| 11 | Schematic part missing from the BOM | **Covered** | KiBot |
| 12 | DNP / not-in-BOM part present in the BOM | **Covered** | — |
| 13 | Duplicate BOM references | **Covered** | — |
| 14 | Missing or placeholder MPN; missing footprint on a BOM row | **Covered** | KiCost (partial) |
| 15 | BOM row with data but no references | **Covered** | — |
| 16 | BOM↔schematic value/footprint disagreement | **Covered** | — |
| 17 | One part number (MPN/LCSC) naming two different parts | **Covered** (syntactic: value/footprint differ under one number) | VelaTrace (semantic form) |
| 18 | Part lifecycle risk (obsolete, NRND, stock, lead time) | **Partial** — offline bundled SAMPLE dataset, or a user-supplied snapshot; never live | KiCost, Bouni kicad-jlcpcb-tools, VelaTrace, MakeIRL (all live) |
| 19 | Position file (CPL) lists designators not on the board | **Covered** | bennymeg Fabrication Toolkit, Bouni, PCBWay Toolkit (generate, don't validate a supplied file) |
| 20 | Board parts missing from the position file | **Covered** (per-side files merged; `exclude_from_pos_files` honoured) | — |
| 21 | Position file unreadable, empty, or duplicated rows | **Covered** | — |
| 22 | BOM mistaken for a position file (or vice versa) at discovery | **Covered** (header-shape discrimination; part-number columns make a file a BOM) | — |
| 23 | Missing board outline (no root-level Edge.Cuts item) | **Covered** (presence only — a closed/watertight outline is class 31) | KiCad DRC, FabGate, MakeIRL |
| 24 | Unfilled / stale copper zones | **Covered** (stored-fill presence per declared layer; keepouts excluded) | KiCad DRC (refill), KiBot, bennymeg (auto-fill) |
| 25 | Track width below fab standard minimums (0.10 mm) | **Covered** (segments + arcs, measured widths only — per-netclass minima are class 27) | KiCad DRC, FabGate, MakeIRL |
| 26 | Drill / via / annular ring / plated slot below fab minimums | **Covered** (0.15–0.20 mm drill tiers, 0.25 mm via diameter, 0.15 mm ring, 0.50 mm slot; plated pads only) | KiCad DRC, FabGate, MakeIRL |
| 27 | Geometric DRC: clearances, shorts, copper-to-edge, netclass rules, custom `.kicad_dru` rules | **Partial** (v0.4 geometry-lite: exact-shape clearance between different-net pads/tracks/arcs/vias, copper-to-edge, against the board's own stated clearances/netclasses or the published fab minimums. NOT covered: zone copper, custom/trapezoid pads, footprint graphics, mask/courtyard/silk clearances, hole clearances, `.kicad_dru` custom rules; full DRC remains **delegated** to the kicad-cli layer when KiCad is installed) | KiCad DRC, KiBot (via KiCad), FabGate (via kicad-cli), VelaTrace (via KiCad DRC), MakeIRL |
| 28 | ERC: pin-type matrix, undriven pins, dangling labels | **Gap** natively — **delegated** via kicad-cli ERC when installed, same honesty rule as 27 | KiCad ERC, KiBot, FabGate, MakeIRL |
| 29 | Netlist-level schematic parity (pin/net level, IPC netlist) | **Gap** — pins/nets are not parsed, by design | KiCad `--schematic-parity`, KiBot, PCBWay Toolkit |
| 30 | Solder-mask / silkscreen / courtyard manufacturability (mask bridge, silk over copper, text size, courtyard overlap) | **Gap** — needs geometry | KiCad DRC, FabGate (silk via DRC) |
| 31 | Outline quality: closed/watertight, self-intersecting, duplicated outlines | **Partial** (v0.4: closure is checked by endpoint chaining of root-level Edge.Cuts lines/arcs/curves within 0.01 mm, circles/rects/polys are self-closed, lone elements are treated as routed slots; self-intersection and duplicate-outline detection are NOT checked; presence remains class 23) | KiCad DRC, FabGate, MakeIRL |
| 32 | Gerber/drill package completeness (layer set, drill format/origin, stray files in the upload zip) | **Partial** (v0.4: file-level package checks — required layer set for a 2-layer order, empty files, duplicate layers, stack consistency, mixed naming schemes, unrecognised fab files; KiCad/Protel/generic naming. NOT covered: Gerber/drill CONTENT — drill format and origin, aperture validity — and packages still zipped) | FabGate, MakeIRL |
| 33 | Live stock, price breaks, total board cost | **Gap** — by design (offline, deterministic, no accounts) | KiCost, Bouni, VelaTrace, MakeIRL |
| 34 | CPL rotation/position correction and fab-specific export formatting | **Gap** — Preflight validates a supplied CPL against the board; it does not generate or correct one | bennymeg Fabrication Toolkit, Bouni, PCBWay Toolkit |
| 35 | Value↔part-number SEMANTIC mismatch (the named part really is a 10 kΩ 0603, not just consistently labelled) | **Gap** — the syntactic half is class 17; the semantic half needs a live catalogue | VelaTrace, Bouni (assignment-time, partial) |
| 36 | Circuit-topology sanity (missing LED resistor, I2C pull-ups, should-connect nets) | **Gap** — needs net parsing + circuit knowledge | VelaTrace |
| 37 | Physics-based layout analysis | **Gap** | FabGate |
| 38 | Visual placement verification (rendered board, BOM highlighting) | **Gap** — a human-eyeball tool, not a rule engine | Interactive HTML BOM, Tracespace, Gerbv |

## Compared tools — one paragraph each

- **KiCad DRC/ERC + kicad-cli** (GPLv3+). The reference implementation:
  full geometric DRC, pin-level ERC, netlist parity, zone refill.
  Preflight's answer is delegation, not imitation — when KiCad is
  installed, its DRC/ERC results are folded into the same report and
  its failures are reported, never trusted blindly. Without KiCad,
  classes 27–31 stay open and Coverage says so.
- **KiBot** (GPL-3.0). A KiCad automation/output pipeline: regenerates
  BOMs, Gerbers, reports and runs KiCad's checks in CI. It gates on
  regenerated outputs; Preflight validates the files you actually
  have, including a hand-edited BOM.
- **KiCost** (MIT). BOM costing against live distributor data. Covers
  class 33 properly; does no schematic/PCB agreement checking.
- **Interactive HTML BOM** (MIT). A visual assembly aid (class 38);
  performs no automated validation.
- **JLCPCB Fabrication Toolkit — bennymeg** (Apache-2.0) and
  **kicad-jlcpcb-tools — Bouni** (MIT). In-KiCad plugins that generate
  JLCPCB-format BOM/CPL, correct CPL rotations, auto-fill zones, and
  (Bouni) look parts up live at assignment time. Generators, not
  validators of supplied files.
- **PCBWay Fabrication Toolkit / KiCad plug-in** (licence not
  verified from public sources in this study). Same generator shape
  for PCBWay, plus IPC netlist export.
- **VelaTrace** (MIT, KiCad 10 plug-in). The closest conceptual
  neighbour found: it checks value↔part-number semantic agreement
  (class 35) and circuit topology (class 36) on top of KiCad DRC.
  It requires KiCad 10 and live data; Preflight requires neither,
  and covers neither of those two classes.
- **FabGate** (PyPI package; licence not verified in this study).
  A gate over the exported Gerber package, including physics-based
  checks (classes 32, 37) — it validates the export, where
  Preflight validates the design sources before export.
- **MakeIRL** (commercial, not open source). A hosted preflight
  service spanning DRC/ERC, live data and package checks; listed
  for completeness of the landscape only.

Two same-name projects reported in earlier notes — a GitHub project
called "pcb-preflight" and a site called "ratemypcb" — could not be
found in the 2026-11-10 search and are not cited as existing.

## KiCad 9 / 10 file formats

Format stamps observed from KiCad-written files: schematic
6 = 20211123, 7 = 20230121, 8 = 20231120, 9 = 20250114,
10 = 20260306; PCB 6 = 20211014, 7 = 20221018, 8 = 20240108,
9 = 20241229, 10 = 20260206. KiCad 9 added embedded files, tables
and pad stacks; KiCad 10 added design variants (per-variant
BOM/DNP overrides — a silently-wrong-BOM risk for any parser that
ignores them), jumpers, back-drilling and barcodes.

Preflight's stance: the core symbol/footprint/BOM model is stable
across 6–10, so 9/10 files are parsed and cross-checked — but every
file stamped outside the tested 6–8 range (root sheet, sub-sheets,
PCB), and every stamp too malformed to read, raises
`KICAD_VERSION_UNTESTED` rather than passing silently. KiCad ≤5
legacy `.sch` files are reported as unsupported.

## Fab thresholds used (classes 25–26)

Standard-process minimums, 2-layer 1 oz, from the official
capability pages of JLCPCB and PCBWay, checked 2026-11-10: trace
width 0.10 mm (both); mechanical drill 0.15 mm (JLCPCB) / 0.20 mm
(PCBWay); via drill 0.15 mm and via diameter 0.25 mm (JLCPCB);
plated-hole annular ring 0.15–0.20 mm across the two fabs' tiers;
plated slot width 0.50 mm (both). Because some JLCPCB detail rows
were only reachable via a mirror of the official table, every
threshold is a CAUTION (engineering question / advanced process),
never a BLOCK. Sources: jlcpcb.com/capabilities/pcb-capabilities,
pcbway.com/capabilities.html, m.pcbway.com/capabilities.html.

## The honest gaps, in one place

Native geometric DRC (27), pin-level ERC (28), netlist parity (29),
mask/silk/courtyard geometry (30), outline closure (31) — delegated
to kicad-cli when KiCad exists, open when it doesn't. Gerber-package
validation (32), live stock/price (33), CPL generation/correction
(34), semantic value↔part-number verification (35), topology (36),
physics (37) and visual verification (38) are not attempted:
they need geometry computation, connectivity, live data or a
renderer, and bolting approximations of them onto a text parser
would produce exactly the confident-but-wrong findings this
project's review process exists to eliminate.
