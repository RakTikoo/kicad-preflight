#!/usr/bin/env python3
"""Run KiCad Preflight over the sample projects and assert the results.

Samples are only ever PARSED (read as text). They are never opened in
KiCad, executed, or fabricated.

Usage: python3 run_tests.py
Exit code: 0 if every expectation holds, 1 otherwise.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent

# sample path -> (expected verdict, expected exit code)
EXPECTED = {
    "samples/good": ("PASS", 0),
    "samples/kicad6": ("PASS", 0),
    "samples/broken": ("BLOCK", 2),
}

# Rules that must fire for the broken fixture — and ONLY these rules.
BROKEN_RULES = Counter({
    "SCH_PART_NOT_ON_PCB": 1,       # R3 missing from PCB
    "FOOTPRINT_MISMATCH": 1,        # D1 0805 vs 1206
    "BOM_UNKNOWN_REFERENCE": 1,     # C9 not in schematic
    "BOM_QUANTITY_MISMATCH": 1,     # D1,D2 qty 3 vs 2 refs
    "PART_OBSOLETE": 1,             # LTST-C170KGKT obsolete in sample data
    "BOM_MISSING_MPN": 1,           # SW1 blank MPN
    "PART_LOW_STOCK": 1,            # RC0603FR-071KL stock 8 in sample data
})

# samples/real/* projects are smoke tests only (see samples/README.md):
# the CLI must produce parseable JSON and a defined exit code.
SMOKE_ONLY_GLOB = "samples/real/*"


def run_cli(target, *extra):
    return subprocess.run(
        [sys.executable, "-m", "preflight", str(target),
         "--format", "json", *extra],
        cwd=ROOT, capture_output=True, text=True)


def parse_report(proc):
    return json.loads(proc.stdout)


def main():
    failures = 0

    for rel, (want_verdict, want_code) in EXPECTED.items():
        proc = run_cli(ROOT / rel)
        try:
            report = parse_report(proc)
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"FAIL {rel}: could not parse CLI output ({exc})")
            print(proc.stdout)
            print(proc.stderr, file=sys.stderr)
            failures += 1
            continue
        problems = []
        if report["verdict"] != want_verdict:
            problems.append(f"verdict {report['verdict']} != {want_verdict}")
        if proc.returncode != want_code:
            problems.append(f"exit code {proc.returncode} != {want_code}")
        if rel == "samples/good" and report["findings"]:
            problems.append(
                f"expected 0 findings, got "
                f"{[f['rule'] for f in report['findings']]}")
        if rel == "samples/broken":
            got = Counter(f["rule"] for f in report["findings"])
            if got != BROKEN_RULES:
                problems.append(f"rules {dict(got)} != {dict(BROKEN_RULES)}")
        if problems:
            print(f"FAIL {rel}: " + "; ".join(problems))
            failures += 1
        else:
            print(f"PASS {rel}: verdict={report['verdict']} "
                  f"exit={proc.returncode} "
                  f"findings={len(report['findings'])}")

    # Single-file invocation must resolve the sibling PCB + BOM.
    proc = run_cli(ROOT / "samples/good/led-board.kicad_sch")
    try:
        report = parse_report(proc)
        if report["verdict"] != "PASS" or proc.returncode != 0:
            print(f"FAIL single-file: verdict={report['verdict']} "
                  f"exit={proc.returncode} (expected PASS/0)")
            failures += 1
        else:
            print("PASS single-file: schematic path resolves the project")
    except json.JSONDecodeError:
        print("FAIL single-file: output was not JSON")
        failures += 1

    # Schematic-only folder: cross-checks cannot run -> BLOCK.
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(ROOT / "samples/good/led-board.kicad_sch", tmp)
        proc = run_cli(tmp)
        try:
            report = parse_report(proc)
            rules = {f["rule"] for f in report["findings"]}
            if report["verdict"] != "BLOCK" or proc.returncode != 2 \
                    or "MISSING_PCB_FILE" not in rules:
                print(f"FAIL sch-only: verdict={report['verdict']} "
                      f"exit={proc.returncode} rules={rules}")
                failures += 1
            else:
                print("PASS sch-only: verdict=BLOCK exit=2 "
                      "rules include MISSING_PCB_FILE")
        except json.JSONDecodeError:
            print("FAIL sch-only: output was not JSON")
            failures += 1

    # Usage errors exit 3.
    proc = run_cli(ROOT / "samples/does-not-exist")
    if proc.returncode != 3:
        print(f"FAIL usage: exit={proc.returncode} (expected 3)")
        failures += 1
    else:
        print("PASS usage: nonexistent target exits 3")

    # Coverage honesty: when kicad-cli is absent (as in CI/sandbox), the
    # report must SAY the official checks were skipped, not hide it.
    proc = run_cli(ROOT / "samples/good")
    report = parse_report(proc)
    if shutil.which("kicad-cli") is None:
        if report["coverage"]["kicad_cli"]["available"]:
            print("FAIL coverage: kicad-cli absent but coverage claims it ran")
            failures += 1
        else:
            print("PASS coverage: kicad-cli skip is stated in the report")
    else:
        print("SKIP coverage: kicad-cli is installed here, skip-path "
              "assertion not applicable")

    # Real third-party projects: smoke only, no verdict assertions.
    for folder in sorted(ROOT.glob(SMOKE_ONLY_GLOB)):
        if not folder.is_dir():
            continue
        proc = run_cli(folder)
        try:
            report = parse_report(proc)
            ok = proc.returncode in (0, 1, 2)
            print(f"{'PASS' if ok else 'FAIL'} smoke {folder.name}: "
                  f"verdict={report['verdict']} exit={proc.returncode} "
                  f"findings={len(report['findings'])} (not asserted)")
            failures += 0 if ok else 1
        except json.JSONDecodeError:
            print(f"FAIL smoke {folder.name}: output was not JSON")
            failures += 1

    if failures:
        print(f"\n{failures} test(s) FAILED")
        return 1
    print("\nAll Preflight tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
