> **PAUSED — H-1B GATE (decided 2026-11-09, ~/workspace/agentic-spikes/H1B_GATE.md).**
> This draft is CANCELLED as a deliverable. It is not ready to go and must not be
> posted. It stays on file only so the launch can be re-dated and revisited after
> Rakshit is back in India (Feb–Mar 2027) or an immigration attorney gives a clear
> opinion. Growth until then is organic only.

# DRAFT — Show HN
# Target date: Sun Jan 10, 2027. DO NOT POST early.
# Rakshit posts; Spark drafts comment replies during the window.

Title: Show HN: KiCad Preflight – check your board before you pay to have it made

Text:

I kept nearly ordering boards with dumb mistakes in them — a resistor
in the schematic that never got placed on the PCB, a footprint that
disagreed between schematic and board, a BOM whose quantity didn't
match its own reference list, a chip that had gone obsolete. KiCad's
DRC/ERC catch none of that; they check the design, not the *order*.

KiCad Preflight is a CLI + KiCad plugin (MIT, pure Python stdlib) that
checks the order: schematic↔PCB agreement, BOM sanity, and offline
part-risk flags. Verdicts are PASS / CAUTION / BLOCK from deterministic
rules only — no LLM — and every finding names the part and the two
values that disagree, so it's verifiable by hand.

Honest parts:

- The part-risk data in v0.x is a small bundled SAMPLE snapshot, not
  live distributor data. The report says so on its face.
- It can't see inside your design the way ERC can; when kicad-cli is
  installed it shells out to the official DRC/ERC and folds those
  results in instead of reimplementing them.
- It was tested against real open-source boards (Upsy Desky, the
  ESP32 Rust board, Piantor — corpus with licences in the repo) plus a
  deliberately broken board. The first version flagged the clean
  boards badly (KiCad 6 footprint format, mostly); the review that
  caught that, with before/after numbers, is in the repo's REVIEW
  docs. I'd rather show that than pretend version one was right.

The BOM checks work on a CSV from any EDA tool — KiCad is the wedge,
not the limit. The CLI is also the testable form; the plugin is the
same engine with a dialog.

Repo: https://github.com/RakTikoo/kicad-preflight

What I'd love from HN: boards it flags unfairly, and faults it
misses. Both are issue templates in the repo.
