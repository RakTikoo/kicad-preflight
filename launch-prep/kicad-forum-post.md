> **PAUSED — H-1B GATE (decided 2026-11-09, ~/workspace/agentic-spikes/H1B_GATE.md).**
> This draft is CANCELLED as a deliverable. It is not ready to go and must not be
> posted. It stays on file only so the launch can be re-dated and revisited after
> Rakshit is back in India (Feb–Mar 2027) or an immigration attorney gives a clear
> opinion. Growth until then is organic only.

# DRAFT — KiCad forum, External Plugins category
# Target date: Tue Jan 5, 2027 (launch stack day 2). DO NOT POST early.
# Forum conventions: what it does, screenshots/GIF, where to get it,
# how to report bugs (GitHub — the thread is not an issue tracker), licence.
# Rakshit posts from his own warmed-up account (account made Dec 2026).

Title: KiCad Preflight — check your board before you pay to have it made

Hi all — I built a plugin + CLI that runs a "preflight" pass over a
KiCad project at the moment you're about to export Gerbers and order,
and flags the things DRC/ERC don't look at:

- schematic ↔ PCB disagreement: a part in the schematic with no
  footprint on the board, a footprint whose library name doesn't match
  the schematic, duplicate references, stray footprints
- BOM problems: references that don't exist, quantity that doesn't
  match the reference list, missing or placeholder manufacturer part
  numbers, DNP parts that snuck into the order
- part risk (offline sample data in v0.x, labelled as such): obsolete
  parts, low stock, long lead times
- when `kicad-cli` is installed, the official DRC/ERC results are
  folded into the same one-page report

Every check is a deterministic rule — no AI decides anything — and
every finding names the exact part and the exact values that disagree,
so you can verify it by hand in seconds. The report ends with a
coverage section stating what was actually checked; if a file couldn't
be parsed, the verdict says so instead of passing quietly.

[demo GIF: broken board → report → fix → clean re-run]

**Get it**
- In KiCad: add this PCM repository URL in the Plugin and Content
  Manager → Manage: [self-hosted repo URL] (official PCM submission
  follows later in January)
- GitHub: https://github.com/RakTikoo/kicad-preflight (CLI included —
  it also scans a standalone BOM CSV from any EDA tool)

**Licence:** MIT. **Bugs / false positives:** please use GitHub issues
— there's a template specifically for "the checker flagged my good
board", which is the report I most want. This thread is for questions
and discussion, not an issue tracker.

Feedback I especially want: boards it flags unfairly, and real faults
it missed. Both make the rule set better.

— Rakshit
