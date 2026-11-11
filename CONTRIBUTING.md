# Contributing to KiCad Preflight

Thanks for helping. The most valuable contributions are **false-positive
reports with the real board context**, **missed-fault reports**, and
**new checks that ship with projects proving both sides**.

## Ground rules

- **Projects are only ever parsed, never fabricated.** Everything in
  `samples/` and `corpus/` exists to be read as text. Nobody orders a
  board to "test" a change.
- **The verdict comes from deterministic rules, never from an LLM.**
  A change that makes a verdict depend on a model call will not be
  merged.
- **Every check ships with two projects:** one it must catch and one
  clean look-alike it must not BLOCK. A check without both is a guess.
- Pure Python standard library only. No new dependencies.
- **Interop facts come from KiCad's official public documentation
  only** (file-format structure, label scoping, the published ERC
  pin-type matrix), each semantic decision cited in a research note.
  KiCad is GPL and this project is MIT: no KiCad code or text may be
  copied into this repository, ever.
- **Offline by default.** The shipped tool makes no network calls
  unless the user passes `--online` and supplies their own provider
  API keys via environment variables. Keys are never accepted as CLI
  arguments, never written to files, logs, reports, or the cache.
- **Open-licence test data only.** Any real board added to `corpus/`
  must come from a public repository with a clear open licence, recorded
  in `corpus/SOURCES.md` with URL, commit, and the licence file read.
  No clear licence → not in the corpus, no exceptions.

## How a check works (orientation)

- Checks are deterministic rules over KiCad's text formats
  (`.kicad_sch` / `.kicad_pcb` S-expressions — both the KiCad 6
  `fp_text` style and the KiCad 7+ `property` style), BOM CSVs
  (KiCad, JLCPCB and generic header spellings), and an offline,
  honestly-labelled sample part-risk dataset.
- The BOM checks live in their own module and work on any tool's BOM
  CSV — KiCad-first is the product wedge, not a KiCad-only limit.
- Severity/verdict: any BLOCK finding → verdict BLOCK; else any
  CAUTION → CAUTION; else PASS — **unless** something that exists
  could not be checked, in which case a `SCAN_INCOMPLETE` finding
  prevents a clean PASS. A silent skip is a bug.
- Exit codes: 0 PASS, 1 CAUTION, 2 BLOCK, 3 usage / IO error.

## Adding a check

1. Add the check with a plain-English finding message a maker can
   verify by hand in seconds (name the part and the exact values that
   disagree).
2. Add a broken project (or a labelled corpus mutation) that the check
   catches at the expected severity.
3. Add (or point to) a clean project that stays PASS / CAUTION as
   appropriate — check the whole clean corpus, not just your sample.
4. Run all three suites (below) and paste the results in your PR.

## Running the tests

```console
$ python3 run_tests.py          # shipped samples
$ python3 run_corpus_tests.py   # open-source corpus + broken mutations
$ python3 run_edge_tests.py     # hostile / malformed inputs
```

All three must pass. If you changed parser behaviour, also run the
clean corpus by hand and look at the per-rule histograms — a new BLOCK
on a clean real board needs a justification in the PR.

## Style

- Python 3.10+ standard library, type-light, docstrings that say *why*.
- Finding messages are user interface. Write them for a maker at
  midnight before an order deadline, not for a compiler.
