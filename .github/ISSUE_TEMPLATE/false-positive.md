---
name: False positive — the checker flagged my good board
about: Preflight flagged a board you believe is correct. These reports are the highest-value issues this project gets.
title: "[false positive] "
labels: false-positive
---

**Rule that fired** (from the report, e.g. `FOOTPRINT_MISMATCH`):

**Preflight version** (`preflight/__init__.py`) and KiCad version that
produced the project files:

**What the checker said** (paste the finding lines):

**Why the board is actually fine** (what Preflight misunderstood — a
library convention, a DNP part, a mechanical footprint, a multi-sheet
layout, …):

**Can you share the project (or a minimal version of it)?**
A zipped folder that reproduces the finding is ideal. Project files are
only ever parsed as text — never opened in a fab flow. Remove anything
you consider private first; a minimal repro is better than a full board.
