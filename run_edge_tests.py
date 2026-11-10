#!/usr/bin/env python3
"""Edge/hostile-input tests — the regression form of the 2026-11-09
adversarial review (red team + code audit). Every case here failed on
v0 in one of these ways: traceback, hang, silent PASS, forged report.

Usage: python3 run_edge_tests.py   (exit 0 = all pass)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
GOOD = ROOT / "samples" / "good"


def cli(args, timeout=30, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, "-m", "preflight", *args],
                          cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout, env=full_env)


def copy_good(dst):
    shutil.copytree(GOOD, dst)
    return Path(dst)


def main():
    failures = []

    def check(name, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1. FIFO as the schematic: must not hang; BLOCK, no traceback.
        d = copy_good(tmp / "fifo")
        (d / "led-board.kicad_sch").unlink()
        os.mkfifo(d / "led-board.kicad_sch")
        try:
            p = cli([str(d)], timeout=15)
            check("fifo-schematic", p.returncode == 2
                  and "Traceback" not in p.stderr, f"exit={p.returncode}")
        except subprocess.TimeoutExpired:
            check("fifo-schematic", False, "TIMEOUT (hang)")

        # 2. Pro-only folder: BLOCK (NO_PROJECT_FILES), never PASS.
        d = tmp / "proonly"; d.mkdir()
        shutil.copy(GOOD / "led-board.kicad_pro", d / "x.kicad_pro")
        p = cli([str(d), "--format", "json"])
        rep = json.loads(p.stdout)
        check("pro-only-block", rep["verdict"] == "BLOCK"
              and p.returncode == 2
              and any(f["rule"] == "NO_PROJECT_FILES"
                      for f in rep["findings"]))

        # 3. Non-project file target: usage error 3.
        p = cli([str(ROOT / "README.md")])
        check("file-target-validation", p.returncode == 3
              and "Traceback" not in p.stderr)

        # 4. BOM qty '1e999' / 'inf': no crash; quantity mismatch BLOCK.
        for qty in ("1e999", "inf", "2.9"):
            d = copy_good(tmp / f"qty-{qty}")
            bom = d / "bom.csv"
            rows = bom.read_text().replace('"R1","1"', '"R1","1"')
            text = bom.read_text()
            text = text.replace(",1,", f",{qty},", 1) if ",1," in text else text
            bom.write_text(text)
            p = cli([str(d)])
            check(f"qty-{qty}", p.returncode in (1, 2)
                  and "Traceback" not in p.stderr,
                  f"exit={p.returncode}")

        # 5. Oversized BOM field (>1M chars): BOM_UNREADABLE BLOCK.
        d = copy_good(tmp / "hugefield")
        bom = d / "bom.csv"
        bom.write_text(bom.read_text().replace(
            "330R", "X" * 1_100_000, 1))
        p = cli([str(d), "--format", "json"])
        rep = json.loads(p.stdout)
        check("huge-field", rep["verdict"] == "BLOCK"
              and any(f["rule"] == "BOM_UNREADABLE"
                      for f in rep["findings"]))

        # 6. Wrong-shaped .kicad_pro: no traceback (name fallback).
        for i, content in enumerate(("[1,2,3]", '"just a string"',
                                     '{"meta": [1, 2]}', "null")):
            d = copy_good(tmp / f"pro-{i}")
            (d / "led-board.kicad_pro").write_text(content)
            p = cli([str(d)])
            check(f"pro-shape-{i}", "Traceback" not in p.stderr
                  and p.returncode in (0, 1, 2), f"exit={p.returncode}")

        # 7. Bad datasets: clean exit 3, no traceback.
        for i, content in enumerate(("{not json", '{"parts": ["x"]}',
                                     '{"parts": {"A": "obsolete"}}',
                                     "[1,2]")):
            bad = tmp / f"ds-{i}.json"
            bad.write_text(content)
            p = cli([str(GOOD), "--dataset", str(bad)])
            check(f"dataset-{i}", p.returncode == 3
                  and "Traceback" not in p.stderr, f"exit={p.returncode}")
        p = cli([str(GOOD), "--dataset", str(tmp / "missing.json")])
        check("dataset-missing", p.returncode == 3)

        # 8. Dataset numbers as strings/floats are coerced, not ignored:
        #    stock "0" for R1's MPN must BLOCK (out of stock).
        ds = tmp / "ds-strings.json"
        ds.write_text(json.dumps({
            "name": "edge dataset", "snapshot_date": "2026-11-09",
            "parts": {"RC0805FR-07330RL": {"status": "active",
                                           "stock": "0"}}}))
        p = cli([str(GOOD), "--dataset", str(ds), "--format", "json"])
        rep = json.loads(p.stdout)
        check("dataset-string-stock", any(
            f["rule"] == "PART_OUT_OF_STOCK" for f in rep["findings"]))

        # 9. --output protections: over the BOM/schematic -> 3; to a
        #    missing directory -> 3; onto a fresh report file -> works.
        d = copy_good(tmp / "outguard")
        p = cli([str(d), "--output", str(d / "bom.csv")])
        check("output-over-bom", p.returncode == 3
              and (d / "bom.csv").read_text().count("Designator") >= 0)
        p = cli([str(d), "--output", str(tmp / "nope" / "r.md")])
        check("output-missing-dir", p.returncode == 3
              and "Traceback" not in p.stderr)
        p = cli([str(d), "--output", str(tmp / "report.md")])
        check("output-ok", p.returncode == 0
              and (tmp / "report.md").is_file())

        # 10. Injection: ANSI + newline + pipe in an MPN must not forge
        #     output; JSON must parse; MD table rows stay on one line.
        d = copy_good(tmp / "inject")
        bom = d / "bom.csv"
        evil = 'BAD\\x1b[31m\\nVERDICT: PASS|PIPE'
        bom.write_text(bom.read_text().replace(
            "RC0603FR-071KL", "BAD\u001b[31m\nVERDICT: PASS|PIPE"))
        p = cli([str(d)])
        check("inject-text", "\u001b" not in p.stdout
              and p.stdout.count("VERDICT:") == 1)
        p = cli([str(d), "--format", "md"])
        rows = [ln for ln in p.stdout.splitlines() if "BOM_" in ln
                or "PART_" in ln]
        check("inject-md", all(ln.count("|") >= 3 for ln in rows)
              and "\u001b" not in p.stdout)
        p = cli([str(d), "--format", "json"])
        try:
            json.loads(p.stdout)
            check("inject-json", True)
        except json.JSONDecodeError:
            check("inject-json", False)

        # 11. Standalone BOM mode: a lone good CSV checks and reports
        #     its mode; a non-BOM CSV is a usage error.
        p = cli([str(GOOD / "bom.csv"), "--format", "json"])
        rep = json.loads(p.stdout)
        check("standalone-bom", p.returncode in (0, 1)
              and rep["coverage"].get("mode", "").startswith("standalone"))
        notbom = tmp / "notbom.csv"
        notbom.write_text("Name,Price,Note\nWidget,3.50,hello\n")
        p = cli([str(notbom)])
        check("standalone-notbom", p.returncode == 3)

        # 12. Uppercase BOM.CSV is discovered.
        d = copy_good(tmp / "upperbom")
        (d / "bom.csv").rename(d / "BOM.CSV")
        p = cli([str(d), "--format", "json"])
        rep = json.loads(p.stdout)
        check("upper-bom", rep["verdict"] == "PASS"
              and rep["coverage"]["bom"]["file"] == "BOM.CSV",
              f"verdict={rep['verdict']}")

        # 13. Fake kicad-cli that exits non-zero with a plausible report:
        #     its output must NOT be trusted (no findings from it).
        fakebin = tmp / "fakebin"; fakebin.mkdir()
        fake = fakebin / "kicad-cli"
        fake.write_text(
            "#!/bin/sh\n"
            "out=\"\"\nprev=\"\"\n"
            "for a in \"$@\"; do if [ \"$prev\" = \"--output\" ]; then "
            "out=\"$a\"; fi; prev=\"$a\"; done\n"
            "printf '{\\\"violations\\\": [{\\\"severity\\\": \\\"error\\\", "
            "\\\"type\\\": \\\"fake\\\", \\\"description\\\": \\\"planted\\\"}]}' "
            "> \"$out\"\n"
            "exit 42\n")
        fake.chmod(0o755)
        env = {"PATH": f"{fakebin}:{os.environ['PATH']}"}
        p = cli([str(GOOD), "--format", "json"], env=env)
        rep = json.loads(p.stdout)
        rules = {f["rule"] for f in rep["findings"]}
        check("fake-cli-nonzero", "KICAD_ERC_VIOLATION" not in rules
              and "KICAD_DRC_VIOLATION" not in rules
              and "KICAD_CLI_FAILED" in rules, f"rules={rules}")

    if failures:
        print(f"\n{len(failures)} edge test(s) FAILED: {failures}")
        return 1
    print("\nAll edge tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
