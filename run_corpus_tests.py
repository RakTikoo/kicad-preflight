#!/usr/bin/env python3
"""Corpus tests: real open-source boards + labelled broken mutations.

Clean-corpus expectations come from the 2026-11-09 adversarial review
(see the review folder's FALSEPOS.md): after the v0.2 fixes, no clean
board may carry a BLOCK finding that the census showed to be false.
Piantor is the deliberate exception — its schematic genuinely
disagrees with its PCB (verified by hand), so it is expected to BLOCK
with exactly the true findings, and this test pins that set so parser
regressions can't quietly change it.

The corpus is downloaded separately (see corpus/SOURCES.md for URLs,
commits and licences); when it is absent this suite SKIPS cleanly.

Usage: python3 run_corpus_tests.py   (exit 0 = pass/skip, 1 = fail)
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CORPUS = ROOT / "corpus"

# target -> (verdict, exit, {rule: count}) — exact, from hand-verified runs
CLEAN = {
    "projects/upsy-desky/pcb":
        ("CAUTION", 1, {"BOM_MISSING": 1}),
    "projects/esp-rust-board/hardware/esp-rust-board":
        ("CAUTION", 1, {"BOM_MISSING_MPN": 1,
                        "SCHEMATIC_DATASHEET_MISSING": 1}),
    "projects/piantor/pcb/left":
        ("BLOCK", 2, {"DUPLICATE_REFERENCE_PCB": 2,
                      "FOOTPRINT_MISMATCH": 4,
                      "BOM_MISSING": 1,
                      "SCHEMATIC_DATASHEET_MISSING": 1}),
    "projects/piantor/pcb/right":
        ("BLOCK", 2, {"DUPLICATE_REFERENCE_PCB": 2,
                      "FOOTPRINT_MISMATCH": 4,
                      "BOM_MISSING": 1,
                      "SCHEMATIC_DATASHEET_MISSING": 1}),
}

BROKEN = {
    "broken/esp-bom-row-deleted": "SCHEMATIC_PART_MISSING_FROM_BOM",
    "broken/esp-bom-qty-changed": "BOM_QUANTITY_MISMATCH",
    "broken/esp-bom-unknown-ref": "BOM_UNKNOWN_REFERENCE",
    "broken/upsy-sch-footprint-blanked": "SCHEMATIC_FOOTPRINT_MISSING",
    "broken/upsy-pcb-footprint-swapped": "FOOTPRINT_MISMATCH",
    "broken/upsy-pcb-footprint-deleted": "SCH_PART_NOT_ON_PCB",
}


def run_json(target):
    proc = subprocess.run(
        [sys.executable, "-m", "preflight", str(target),
         "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    return proc, json.loads(proc.stdout)


def main():
    if not (CORPUS / "projects").is_dir():
        print("SKIP: open-source corpus not downloaded "
              "(see corpus/SOURCES.md) — corpus tests not run.")
        return 0
    failures = 0
    from collections import Counter
    for rel, (verdict, code, rules) in CLEAN.items():
        target = CORPUS / rel
        if not target.is_dir():
            print(f"FAIL {rel}: missing from corpus")
            failures += 1
            continue
        proc, report = run_json(target)
        got = Counter(f["rule"] for f in report["findings"])
        ok = (report["verdict"] == verdict and proc.returncode == code
              and got == Counter(rules))
        print(f"{'PASS' if ok else 'FAIL'} clean {rel}: "
              f"verdict={report['verdict']} exit={proc.returncode} "
              f"rules={dict(got)}")
        failures += 0 if ok else 1
    for rel, rule in BROKEN.items():
        target = CORPUS / rel
        if not target.is_dir():
            print(f"FAIL {rel}: missing (run corpus/build_broken.py)")
            failures += 1
            continue
        proc, report = run_json(target)
        fired = {f["rule"] for f in report["findings"]}
        ok = rule in fired and proc.returncode in (1, 2)
        print(f"{'PASS' if ok else 'FAIL'} broken {rel}: "
              f"expected {rule}, verdict={report['verdict']}")
        failures += 0 if ok else 1
    if failures:
        print(f"\n{failures} corpus test(s) FAILED")
        return 1
    print("\nAll corpus tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
