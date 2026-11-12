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
    "samples/v9": ("CAUTION", 1),
}

# samples/v9 is the good board re-stamped as KiCad 9: the ONLY findings
# may be the two honest version warnings (one per file).
V9_RULES = Counter({"KICAD_VERSION_UNTESTED": 2})

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

    pro_tests = subprocess.run(
        [sys.executable, "-m", "unittest", "test_easyeda_bom"], cwd=ROOT)
    failures += int(pro_tests.returncode != 0)

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
        if rel == "samples/v9":
            got = Counter(f["rule"] for f in report["findings"])
            if got != V9_RULES:
                problems.append(f"rules {dict(got)} != {dict(V9_RULES)}")
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

    # --- v0.4: geometry-lite fixtures --------------------------------
    # geo-ok: pads exactly AT the stated 0.2 mm clearance, a closed
    # outline, and a lone Edge.Cuts line (a routed slot) — geometry
    # must stay completely silent.
    proc = run_cli(ROOT / "samples/geo-ok")
    try:
        report = parse_report(proc)
        rules = {f["rule"] for f in report["findings"]}
        geo_rules = {r for r in rules
                     if r.startswith("GEOMETRY")
                     or r == "BOARD_OUTLINE_OPEN"}
        if geo_rules:
            print(f"FAIL geo-ok: unexpected geometry rules {geo_rules}")
            failures += 1
        else:
            print("PASS geo-ok: exact-clearance pads + slot line + "
                  "closed outline produce no geometry findings")
    except json.JSONDecodeError:
        print("FAIL geo-ok: output was not JSON")
        failures += 1

    # geo-violation: pads 0.05 mm apart + a pad 0.1 mm from the edge.
    proc = run_cli(ROOT / "samples/geo-violation")
    try:
        report = parse_report(proc)
        rules = {f["rule"] for f in report["findings"]}
        want = {"GEOMETRY_CLEARANCE_BELOW_MIN",
                "GEOMETRY_COPPER_TO_EDGE"}
        if not want <= rules:
            print(f"FAIL geo-violation: missing {want - rules} "
                  f"in {rules}")
            failures += 1
        else:
            print("PASS geo-violation: clearance + copper-to-edge fire")
    except json.JSONDecodeError:
        print("FAIL geo-violation: output was not JSON")
        failures += 1

    # geo-open: a U-shaped outline (3 sides) must not pass as closed.
    proc = run_cli(ROOT / "samples/geo-open")
    try:
        report = parse_report(proc)
        rules = {f["rule"] for f in report["findings"]}
        if "BOARD_OUTLINE_OPEN" not in rules:
            print(f"FAIL geo-open: BOARD_OUTLINE_OPEN missing in {rules}")
            failures += 1
        else:
            print("PASS geo-open: open outline is BLOCKed")
    except json.JSONDecodeError:
        print("FAIL geo-open: output was not JSON")
        failures += 1

    # --- v0.5: netlist fixtures -------------------------------------
    NETLIST_RULES = {"ERC_UNCONNECTED_PIN", "ERC_POWER_NOT_DRIVEN",
                     "ERC_OUTPUT_CONFLICT", "ERC_DANGLING_LABEL",
                     "PARITY_NET_MEMBERSHIP", "PARITY_NET_SCH_ONLY",
                     "PARITY_NET_PCB_ONLY", "PARITY_NET_RENAMED"}
    # netlist-clean: a fully wired regulator circuit whose PCB pads
    # match the schematic membership exactly — no findings at all.
    proc = run_cli(ROOT / "samples/netlist-clean")
    try:
        report = parse_report(proc)
        if report["verdict"] != "PASS" or report["findings"]:
            print(f"FAIL netlist-clean: verdict={report['verdict']} "
                  f"findings={[f['rule'] for f in report['findings']]}")
            failures += 1
        else:
            print("PASS netlist-clean: wired circuit + matching PCB "
                  "produce zero findings")
    except json.JSONDecodeError:
        print("FAIL netlist-clean: output was not JSON")
        failures += 1

    # netlist-hier: sub-sheet via sheet pin <-> hierarchical label,
    # a global label crossing sheets, a power symbol in the
    # sub-sheet — clean, no findings.
    proc = run_cli(ROOT / "samples/netlist-hier")
    try:
        report = parse_report(proc)
        if report["verdict"] != "PASS" or report["findings"]:
            print(f"FAIL netlist-hier: verdict={report['verdict']} "
                  f"findings={[f['rule'] for f in report['findings']]}")
            failures += 1
        else:
            print("PASS netlist-hier: hierarchy plumbing is clean")
    except json.JSONDecodeError:
        print("FAIL netlist-hier: output was not JSON")
        failures += 1

    # netlist-broken: each planted fault exactly once — (a) two
    # outputs on VOUT, (b) VBAT_IN power_in with only a passive
    # connector, (c) R2 pin 2 open with no NC marker (pin 2 of C1
    # is the NC-marked control and stays silent), (d) R1 pad 2
    # moved GND->VOUT on the PCB (membership diff on BOTH shared
    # nets), (e) AUX in the schematic only, (f) CLK_IN renamed
    # CLK_OUT on the PCB with identical membership (NOTE only).
    # The one non-netlist finding is the expected NOTE for U3's
    # out-of-dataset MPN.
    proc = run_cli(ROOT / "samples/netlist-broken")
    try:
        report = parse_report(proc)
        got_net = Counter(f["rule"] for f in report["findings"]
                          if f["rule"] in NETLIST_RULES)
        want_net = Counter({
            "ERC_OUTPUT_CONFLICT": 1,
            "ERC_POWER_NOT_DRIVEN": 1,
            "ERC_UNCONNECTED_PIN": 1,
            "PARITY_NET_MEMBERSHIP": 2,
            "PARITY_NET_SCH_ONLY": 1,
            "PARITY_NET_RENAMED": 1,
        })
        got_other = Counter(f["rule"] for f in report["findings"]
                            if f["rule"] not in NETLIST_RULES)
        if got_net != want_net \
                or got_other != Counter({"PART_NOT_IN_SAMPLE_DATA": 1}) \
                or report["verdict"] != "BLOCK":
            print(f"FAIL netlist-broken: netlist={dict(got_net)} "
                  f"other={dict(got_other)} "
                  f"verdict={report['verdict']}")
            failures += 1
        else:
            print("PASS netlist-broken: every planted netlist fault "
                  "fires exactly once, nothing else")
    except json.JSONDecodeError:
        print("FAIL netlist-broken: output was not JSON")
        failures += 1

    # --- v0.4: Gerber package fixtures -------------------------------
    proc = run_cli(ROOT / "samples/gerber-good")
    try:
        report = parse_report(proc)
        if report["verdict"] != "PASS" or report["findings"]:
            print(f"FAIL gerber-good: verdict={report['verdict']} "
                  f"findings={[f['rule'] for f in report['findings']]}")
            failures += 1
        else:
            print("PASS gerber-good: complete package passes clean "
                  "(auto-detected)")
    except json.JSONDecodeError:
        print("FAIL gerber-good: output was not JSON")
        failures += 1

    proc = run_cli(ROOT / "samples/gerber-incomplete")
    try:
        report = parse_report(proc)
        got = Counter(f["rule"] for f in report["findings"])
        want = Counter({"GERBER_BOTTOM_COPPER_MISSING": 1,
                        "GERBER_MASK_MISSING": 1,
                        "GERBER_SILKSCREEN_MISSING": 1,
                        "GERBER_EMPTY_FILE": 1})
        if got != want or report["verdict"] != "CAUTION":
            print(f"FAIL gerber-incomplete: rules {dict(got)} "
                  f"verdict={report['verdict']}")
            failures += 1
        else:
            print("PASS gerber-incomplete: exact caution set")
    except json.JSONDecodeError:
        print("FAIL gerber-incomplete: output was not JSON")
        failures += 1

    proc = run_cli(ROOT / "samples/gerber-inner-broken")
    try:
        report = parse_report(proc)
        rules = {f["rule"] for f in report["findings"]}
        want = {"GERBER_INNER_WITHOUT_BOTTOM", "GERBER_OUTLINE_MISSING"}
        if not want <= rules or report["verdict"] != "BLOCK":
            print(f"FAIL gerber-inner-broken: rules={rules} "
                  f"verdict={report['verdict']}")
            failures += 1
        else:
            print("PASS gerber-inner-broken: inner-without-bottom "
                  "is BLOCKed")
    except json.JSONDecodeError:
        print("FAIL gerber-inner-broken: output was not JSON")
        failures += 1

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
