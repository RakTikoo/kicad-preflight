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
        ("CAUTION", 1, {"BOM_MISSING": 1,
                        # v0.4, hand-verified: the USB-C shell pad
                        # sits 0.071 mm from the routed edge (the
                        # connector straddles the edge by design);
                        # the board states no edge clearance, so
                        # the published 0.20 mm fab minimum applies.
                        "GEOMETRY_COPPER_TO_EDGE": 1,
                        # v0.5, hand-verified (v05-CENSUS.md): +5V,
                        # GND and VBUS carry only power_in/passive
                        # pins -- no driver pin exists in the file
                        # data (contrast +3V3, driven by U2 pin 2
                        # power_out, which stays silent). TRUE per
                        # KiCad's pin-type matrix.
                        "ERC_POWER_NOT_DRIVEN": 3}),
    "projects/esp-rust-board/hardware/esp-rust-board":
        ("CAUTION", 1, {"BOM_MISSING_MPN": 1,
                        "SCHEMATIC_DATASHEET_MISSING": 1,
                        # v0.5, hand-verified (v05-CENSUS.md): six
                        # power nets (+3V3 via passive inductor L1,
                        # +5V, VBUS, GND, the unnamed USB-C VBUS and
                        # shield segments) have no power_out pin in
                        # the file data -- KiCad's power-pin test
                        # accepts only power_out as a driver.
                        "ERC_POWER_NOT_DRIVEN": 6}),
    # v0.6 notes for esp (no further pin entries):
    # * Parity: U3/U4 are no longer unmeasurable -- custom pads
    #   whose primitive set is modelled now carry membership
    #   (WS3 section 1a), the widened parity run (23 nets) finds
    #   zero differences, and the v0.5 SCAN_INCOMPLETE for this
    #   board is gone.
    # * Zones: six zone pairs on esp measure 0.1219 mm
    #   chord-to-chord against the 0.127 mm Default clearance
    #   (the +BATT/GND pair among them, brute-force verified).
    #   KiCad's true-geometry DRC flags exactly two of the six
    #   (GND/+3V3 on F.Cu and B.Cu, 0.1231/0.1251 mm actual); the
    #   other four are at or above threshold in true geometry --
    #   stored chord distance undershoots by up to ~0.005 mm and
    #   cannot tell the pairs apart. Zone pairs are therefore
    #   judged with a documented 0.006 mm certainty tolerance
    #   (flag only certain violations, the geometry layer's
    #   standing doctrine), so these two true violations are a
    #   DOCUMENTED MISS, named here and in COVERAGE.md class 27
    #   -- not a silent pass. Evidence: REVIEW.md section 12 and
    #   v06-ws3/INTEGRATION.md section 1b.
    "projects/piantor/pcb/left":
        ("BLOCK", 2, {"DUPLICATE_REFERENCE_PCB": 2,
                      "FOOTPRINT_MISMATCH": 4,
                      "BOM_MISSING": 1,
                      "SCHEMATIC_DATASHEET_MISSING": 1,
                      # v0.4, hand-verified per KiCad's documented
                      # pad semantics: the Pico module's elongated
                      # castellated SMD tabs graze the neighbouring
                      # fan-out tracks by up to 0.125 mm against the
                      # board's stated 0.3 mm class (the fan-out was
                      # routed against the 1.7 mm thru-hole pads).
                      "GEOMETRY_CLEARANCE_BELOW_MIN": 1,
                      # v0.5, hand-verified (v05-CENSUS.md): VBUS,
                      # VCC and GND are fed through the TRRS
                      # connector / resistors; no power_out pin
                      # exists on them in the file data. TRUE per
                      # KiCad's documented power-pin test.
                      "ERC_POWER_NOT_DRIVEN": 3}),
    "projects/piantor/pcb/right":
        ("BLOCK", 2, {"DUPLICATE_REFERENCE_PCB": 2,
                      "FOOTPRINT_MISMATCH": 4,
                      "BOM_MISSING": 1,
                      "SCHEMATIC_DATASHEET_MISSING": 1,
                      "GEOMETRY_CLEARANCE_BELOW_MIN": 1,
                      # v0.5: same hand-verified trio as the left
                      # half (VBUS, VCC, GND undriven).
                      "ERC_POWER_NOT_DRIVEN": 3}),
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
