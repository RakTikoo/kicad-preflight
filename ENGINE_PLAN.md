# KiCad Preflight — Engine Starting Plan

Chat: this work lives in the "KiCad Preflight" side chat (designated 2026-11-09).
Master plan: ~/workspace/agentic-spikes/SPIKE_PLAN.md (Week 2: Mon Nov 16 – Sun Nov 22, 2026,
started early at Rakshit's say-so on Nov 9 — no deadline pressure).

## Architecture (Rakshit's call, 2026-11-09)
One shared checks engine, two front doors:
1. CLI first — testable on sample projects without a GUI.
2. KiCad plugin/extension second — one click inside KiCad before Gerber export / ordering,
   later listed in KiCad's Plugin and Content Manager (the discovery channel).
Later expansion only: browser extension on the JLCPCB/PCBWay order page. Not spike scope.

## Environment check (2026-11-09, this VM)
- Python 3.12 available. No kicad-cli on this VM.
- KiCad project files (.kicad_sch / .kicad_pcb) are text s-expressions, and BOMs export as CSV,
  so the engine can start as pure-Python parsing + BOM checks with no KiCad install.
- DRC/ERC via the official kicad-cli plugs in as an optional step when KiCad is present
  (Rakshit's Windows 11 PC, where his practice ladder runs, or an install here later).

## First build steps
1. S-expression parser for .kicad_sch / .kicad_pcb; extract components, footprints, nets.
2. BOM CSV parser; checks: missing footprint, missing/placeholder part numbers,
   quantity mismatches between schematic and BOM.
3. Sample corpus: one clean mini project + one deliberately broken project
   (wrong footprint, unconnected pin, obsolete part) — same discipline as SkillScan.
4. One-page report (terminal + Markdown): PASS / CAUTION / BLOCK style verdicts.
5. Then: kicad-cli DRC/ERC integration + part-risk checks, plugin wrapper.

## Rakshit's practice ladder (his pace, no deadline)
Board 1: LED + resistor + battery, full schematic -> PCB -> DRC clean -> Gerbers.
Board 2: copy of a simple board, deliberately broken = Preflight test corpus.
Board 3: a real-ish DIY board (555 / TTL module or ESP32 breakout).
Ordering a real fab board only on his explicit say-so.
