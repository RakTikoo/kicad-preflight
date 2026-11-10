# Rakshit's KiCad Learning Plan

For: a chip design engineer who has made CPUs, knows electronics deeply, codes in
C / C++ / Python — but has never used KiCad. So this plan teaches the *tool*, not
electronics. Every new KiCad term gets mapped to something he already knows.
Pace: his own — no deadline ("not like we have a product meeting tomorrow").
Golden rule: every practice board doubles as test data for KiCad Preflight.

## Stage 0 — Setup (30 minutes, one evening)
- Install KiCad (current stable) on the Windows 11 PC — free, from kicad.org.
- Tour the 4 windows: Project Manager, Schematic Editor, PCB Editor, Symbol/Footprint libraries.
  Analogy: schematic = the RTL/netlist intent; PCB = physical design / layout;
  DRC = design rule check (same idea as in chip PD, friendlier); ERC = electrical rule check.
- Set units to mm, grid, and turn on the netlist view. Nothing to design yet.

## Stage 1 — Board 1: the full loop in one evening
LED + resistor + battery (coin cell), start to finish:
1. Schematic: place symbols, wire them, annotate references (R1, D1, BT1).
2. Assign footprints — the moment "symbol vs footprint" clicks (logical pin vs physical pad).
3. PCB: place, route two copper layers, add board outline.
4. Run DRC until clean. Export Gerbers + drill files — the exact files a fab house takes.
Milestone: a Gerber zip he could upload to JLCPCB. (Ordering it is optional, his say-so only.)
Preflight tie-in: run Preflight on this board — it should PASS. His first clean test case.

## Stage 2 — Reading boards, libraries, and breaking things on purpose
- Open 2–3 small open-source KiCad projects (the same corpus Preflight is tested on)
  and read them: how other people organise sheets, power symbols, net labels.
- Libraries properly: built-in symbol/footprint libraries, and how to add one part
  from a datasheet (pinout → symbol, package drawing → footprint pads).
- Board 2: copy Board 1 and deliberately break it, one fault at a time:
  wrong footprint, unconnected pin, missing part number in the BOM, swapped pins.
Milestone: Preflight must flag each planted fault. Anything it misses is a bug report
he just authored — this is how the test corpus grows.

## Stage 3 — A real board in his style
A small Ben Eater-flavoured module — e.g. a 555 timer board, a TTL logic breakout,
or an ESP32 breakout:
- Multi-part schematic with net labels and a power flag done properly.
- 2-layer routing with ground pour, silkscreen that actually helps assembly.
- Generate the BOM (CSV) and placement file — the two files an assembly service needs.
Milestone: a board he'd genuinely order. Preflight's BOM checks run on a real BOM now.

## Stage 4 — Ordering literacy (no order required)
- Walk the JLCPCB and PCBWay upload flow with the Stage 1/3 Gerbers up to — not past —
  the payment step: how fab DRC differs from KiCad DRC, stencil/assembly options,
  what LCSC part numbers are and why the BOM needs them.
- This is the exact moment Preflight (and later its order-page browser extension)
  is designed to sit in — he'll feel where the checks belong.

## Stage 5 — Under the hood (feeds the product, optional depth)
- KiCad files are text s-expressions: read his own .kicad_sch / .kicad_pcb in an editor
  alongside the Preflight parser code (pure Python — his language).
- What kicad-cli automates (DRC/ERC/Gerber/BOM from the command line) and what the
  KiCad Action Plugin API can do inside the app — the two front doors of Preflight.
Milestone: he can read a Preflight finding and trace it to the line in his board file.

## How Spark teaches along the way
- Each stage: Spark prepares the step list + the "what this button means in electronics
  terms" notes *before* he sits down, and reviews his exported files after.
- No stage gates the product build — the engine work runs in parallel in this chat;
  his boards join the test corpus as they appear.
- Rough rhythm if he wants one: one stage per week alongside the spike, ~1 evening each
  for Stages 0–2. Faster or slower both fine.
