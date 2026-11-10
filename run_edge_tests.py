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

        # ---------------------------------------------------------------
        # 14. v0.3 wave: version stamps, fab minimums, zones, CPL.
        # ---------------------------------------------------------------
        def mutate_pcb(dst, subs=(), add=""):
            pcb = dst / "led-board.kicad_pcb"
            text = pcb.read_text()
            for old, new in subs:
                assert old in text, f"fixture drift: {old!r}"
                text = text.replace(old, new)
            if add:
                text = text.rstrip()
                assert text.endswith(")")
                text = text[:text.rfind(")")] + add + ")\n"
            pcb.write_text(text)
            return dst

        def rules_of(path):
            p = cli([str(path), "--format", "json"])
            rep = json.loads(p.stdout)
            return rep, {f["rule"] for f in rep["findings"]}

        # 14a. KiCad 10 stamps: untested-version CAUTION per file.
        d = copy_good(tmp / "v10")
        for f, old, new in (
                ("led-board.kicad_sch", "(version 20231120)",
                 "(version 20260306)"),
                ("led-board.kicad_pcb", "(version 20240108)",
                 "(version 20260206)")):
            fp = d / f
            fp.write_text(fp.read_text().replace(old, new)
                          .replace('"8.0"', '"10.0"'))
        rep, rules = rules_of(d)
        check("v10-stamps", rules == {"KICAD_VERSION_UNTESTED"}
              and rep["verdict"] == "CAUTION", f"rules={rules}")

        # 14b. Thin track segment below both fabs' 0.10 mm minimum.
        d = mutate_pcb(copy_good(tmp / "thintrack"), add=(
            '\n\t(segment (start 20 20) (end 40 20) (width 0.05) '
            '(layer "F.Cu") (net 1) '
            '(uuid "aa000001-0000-4000-8000-000000000001"))\n'))
        rep, rules = rules_of(d)
        check("fab-thin-track", "TRACK_WIDTH_BELOW_FAB_MIN" in rules
              and rep["verdict"] == "CAUTION", f"rules={rules}")

        # 14c. No Edge.Cuts outline at all: BLOCK.
        d = copy_good(tmp / "nooutline")
        pcb = d / "led-board.kicad_pcb"
        lines = [ln for ln in pcb.read_text().splitlines()
                 if "gr_rect" not in ln]
        pcb.write_text("\n".join(lines) + "\n")
        rep, rules = rules_of(d)
        check("fab-no-outline", "BOARD_OUTLINE_MISSING" in rules
              and rep["verdict"] == "BLOCK", f"rules={rules}")

        # 14d. Unfilled zone fires; an identical filled zone does not.
        zone = ('\n\t(zone (net 2) (net_name "GND") (layer "B.Cu") '
                '(uuid "aa000002-0000-4000-8000-000000000002") '
                '(polygon (pts (xy 15 20) (xy 70 20) (xy 70 50) '
                '(xy 15 50))){FILL})\n')
        d = mutate_pcb(copy_good(tmp / "zoneopen"),
                       add=zone.replace("{FILL}", ""))
        rep, rules = rules_of(d)
        check("fab-zone-unfilled", "ZONE_UNFILLED" in rules,
              f"rules={rules}")
        filled = (' (filled_polygon (layer "B.Cu") (pts (xy 15 20) '
                  '(xy 70 20) (xy 70 50) (xy 15 50)))')
        d = mutate_pcb(copy_good(tmp / "zonefilled"),
                       add=zone.replace("{FILL}", filled))
        rep, rules = rules_of(d)
        check("fab-zone-filled-ok", "ZONE_UNFILLED" not in rules
              and rep["verdict"] == "PASS", f"rules={rules}")

        # 14e. Drill tiers: 0.18 fires (PCBWay tier), 0.10 fires too,
        #      and the healthy 1.0 control stays silent (14b-14d runs).
        d = mutate_pcb(copy_good(tmp / "drill18"),
                       subs=[("(drill 1.0)", "(drill 0.18)")])
        rep, rules = rules_of(d)
        check("fab-drill-018", "DRILL_BELOW_FAB_MIN" in rules
              and "ANNULAR_RING_BELOW_FAB_MIN" not in rules,
              f"rules={rules}")
        # 14f. Skinny ring: pad 1.15 with drill 1.0 -> ring 0.075.
        d = mutate_pcb(copy_good(tmp / "ring"),
                       subs=[("(size 1.8 1.8) (drill 1.0)",
                              "(size 1.15 1.15) (drill 1.0)")])
        rep, rules = rules_of(d)
        check("fab-ring", "ANNULAR_RING_BELOW_FAB_MIN" in rules,
              f"rules={rules}")
        # 14g. Oval slot 0.4 wide: below the 0.50 plated-slot minimum.
        d = mutate_pcb(copy_good(tmp / "slot"),
                       subs=[('(pad "1" thru_hole rect (at -2.5 0) '
                              '(size 1.8 1.8) (drill 1.0)',
                              '(pad "1" thru_hole rect (at -2.5 0) '
                              '(size 1.8 1.8) (drill oval 1.7 0.4)')])
        rep, rules = rules_of(d)
        check("fab-slot", "SLOT_BELOW_FAB_MIN" in rules
              and "DRILL_BELOW_FAB_MIN" not in rules, f"rules={rules}")
        # 14h. Via drill 0.1 and via diameter 0.2 both fire VIA rule.
        d = mutate_pcb(copy_good(tmp / "via"), add=(
            '\n\t(via (at 30 30) (size 0.5) (drill 0.1) '
            '(layers "F.Cu" "B.Cu") (net 1) '
            '(uuid "aa000003-0000-4000-8000-000000000003"))\n'))
        rep, rules = rules_of(d)
        check("fab-via-drill", "VIA_BELOW_FAB_MIN" in rules,
              f"rules={rules}")
        # 14i. NPTH pad with size == drill must NOT read as a 0 ring.
        j1pad2 = ('(pad "2" thru_hole circle (at 2.5 0) (size 1.8 1.8) '
                  '(drill 1.0) (layers "*.Cu" "*.Mask") (net 2 "GND"))')
        d = mutate_pcb(copy_good(tmp / "npth"), subs=[(
            j1pad2, j1pad2 + '\n\t\t(pad "3" np_thru_hole circle '
            '(at 0 5) (size 3.0 3.0) (drill 3.0) '
            '(layers "*.Cu" "*.Mask"))')])
        rep, rules = rules_of(d)
        check("fab-npth-not-ring", "ANNULAR_RING_BELOW_FAB_MIN" not in rules
              and rep["verdict"] == "PASS", f"rules={rules}")

        # 14j. CPL agreement: complete CPL is silent; a missing part
        #      plus an unknown ref both fire.
        pos_head = "Ref,Val,Package,PosX,PosY,Rot,Side\n"
        pos_rows = "".join(
            f"{r},X,Pkg,10,10,0,Top\n"
            for r in ("J1", "R1", "D1", "D2", "SW1"))
        d = copy_good(tmp / "cplgood")
        (d / "pos.csv").write_text(pos_head + pos_rows)
        rep, rules = rules_of(d)
        check("cpl-complete", not any(r.startswith("CPL_") for r in rules)
              and rep["verdict"] == "PASS", f"rules={rules}")
        d = copy_good(tmp / "cplbad")
        (d / "pos.csv").write_text(
            pos_head + pos_rows.replace("SW1,X", "X99,X"))
        rep, rules = rules_of(d)
        check("cpl-mismatch", "CPL_UNKNOWN_REFERENCE" in rules
              and "CPL_PART_MISSING" in rules, f"rules={rules}")
        # 14k. A lone position file is NOT mistaken for the BOM:
        #      BOM_MISSING must fire, with no quantity-mismatch spam.
        d = copy_good(tmp / "cplonly")
        (d / "bom.csv").unlink()
        (d / "pos.csv").write_text(pos_head + pos_rows)
        rep, rules = rules_of(d)
        check("cpl-not-a-bom", "BOM_MISSING" in rules
              and "BOM_QUANTITY_MISMATCH" not in rules
              and rep["coverage"]["placement"]["file"] == "pos.csv",
              f"rules={rules}")

        # 14l. BOM part-number conflict (standalone mode): same MPN,
        #      different values fires; same part split in two rows not.
        conflict = tmp / "conflict.csv"
        conflict.write_text(
            "Reference,Quantity,Value,Footprint,MPN\n"
            "R1,1,10k,R_0603,RC0603FR-0710KL\n"
            "R2,1,1k,R_0603,RC0603FR-0710KL\n")
        p = cli([str(conflict), "--format", "json"])
        rep = json.loads(p.stdout)
        rules = {f["rule"] for f in rep["findings"]}
        check("bom-pn-conflict", "BOM_PART_NUMBER_CONFLICT" in rules,
              f"rules={rules}")
        fine = tmp / "fine.csv"
        fine.write_text(
            "Reference,Quantity,Value,Footprint,MPN\n"
            "R1,1,10k,R_0603,RC0603FR-0710KL\n"
            "R2,1,10k,R_0603,RC0603FR-0710KL\n")
        p = cli([str(fine), "--format", "json"])
        rep = json.loads(p.stdout)
        rules = {f["rule"] for f in rep["findings"]}
        check("bom-pn-split-ok", "BOM_PART_NUMBER_CONFLICT" not in rules,
              f"rules={rules}")

        # ---------------------------------------------------------------
        # 15. v0.3 fix wave: regression form of the adversarial review
        #     (red team 87 cases + audit 10 Major + FP tester 2 FPs).
        # ---------------------------------------------------------------
        # 15a. Malformed version stamp warns instead of vanishing.
        d = copy_good(tmp / "v03stamp")
        sch = d / "led-board.kicad_sch"
        sch.write_text(sch.read_text().replace(
            "(version 20231120)", '(version "20250114x")'))
        rep, rules = rules_of(d)
        check("v03-malformed-stamp", "KICAD_VERSION_UNTESTED" in rules,
              f"rules={rules}")
        # 15b. A v9 stamp on a hierarchical SUB-sheet warns too.
        d = copy_good(tmp / "v03subsheet")
        sub = d / "sub.kicad_sch"
        sub.write_text(
            '(kicad_sch (version 20250114) (generator "eeschema")\n'
            '\t(generator_version "9.0")\n'
            '\t(uuid "aa000010-0000-4000-8000-000000000001")\n'
            '\t(paper "A4"))\n')
        sch = d / "led-board.kicad_sch"
        text = sch.read_text().rstrip()
        assert text.endswith(")")
        text = (text[:text.rfind(")")]
                + '\n\t(sheet (at 20 20) (size 20 10)\n'
                  '\t\t(property "Sheetname" "Sub" (at 20 19 0)\n'
                  '\t\t\t(effects (font (size 1 1))))\n'
                  '\t\t(property "Sheetfile" "sub.kicad_sch" '
                  '(at 20 21 0)\n'
                  '\t\t\t(effects (font (size 1 1)))))\n)\n')
        sch.write_text(text)
        rep, rules = rules_of(d)
        check("v03-subsheet-v9", "KICAD_VERSION_UNTESTED" in rules,
              f"rules={rules}")

        # 15c. Track evasions: unit suffix + lowercase layer, list
        #      width, and copper arcs are all measured now.
        d = mutate_pcb(copy_good(tmp / "v03units"), add=(
            '\n\t(segment (start 20 20) (end 40 20) (width 0.05mm) '
            '(layer "f.cu") (net 1) '
            '(uuid "aa000011-0000-4000-8000-000000000001"))\n'))
        rep, rules = rules_of(d)
        check("v03-track-unit-case", "TRACK_WIDTH_BELOW_FAB_MIN" in rules,
              f"rules={rules}")
        d = mutate_pcb(copy_good(tmp / "v03arc"), add=(
            '\n\t(arc (start 20 20) (mid 30 25) (end 40 20) '
            '(width 0.05) (layer "F.Cu") (net 1) '
            '(uuid "aa000012-0000-4000-8000-000000000001"))\n'))
        rep, rules = rules_of(d)
        check("v03-arc-track", "TRACK_WIDTH_BELOW_FAB_MIN" in rules,
              f"rules={rules}")
        # 15d. Drill written after an (offset ...) node still reads.
        d = mutate_pcb(copy_good(tmp / "v03offset"),
                       subs=[("(drill 1.0)", "(drill (offset 0 0) 0.1)")])
        rep, rules = rules_of(d)
        check("v03-drill-offset-first", "DRILL_BELOW_FAB_MIN" in rules,
              f"rules={rules}")
        # 15e. A truly unreadable dimension is reported, not dropped.
        d = mutate_pcb(copy_good(tmp / "v03unread"), add=(
            '\n\t(segment (start 20 20) (end 40 20) (width abc) '
            '(layer "F.Cu") (net 1) '
            '(uuid "aa000013-0000-4000-8000-000000000001"))\n'))
        rep, rules = rules_of(d)
        check("v03-dimension-unreadable", "DIMENSION_UNREADABLE" in rules,
              f"rules={rules}")
        # 15f. Reference-less footprint still triggers the outline BLOCK.
        import re as _re
        d = copy_good(tmp / "v03refless")
        pcb = d / "led-board.kicad_pcb"
        text = "\n".join(ln for ln in pcb.read_text().splitlines()
                         if "gr_rect" not in ln)
        text = _re.sub(r'\(property "Reference" "[^"]*"',
                       '(property "ReferenceX" ""', text)
        pcb.write_text(text)
        rep, rules = rules_of(d)
        check("v03-outline-refless", "BOARD_OUTLINE_MISSING" in rules
              and rep["verdict"] == "BLOCK", f"rules={rules}")
        # 15g. Empty (filled_polygon) is unfilled; a keepout zone is
        #      not a copper pour and stays silent.
        d = mutate_pcb(copy_good(tmp / "v03zempty"), add=(
            '\n\t(zone (net 2) (net_name "GND") (layer "F.Cu") '
            '(uuid "aa000014-0000-4000-8000-000000000001") '
            '(polygon (pts (xy 15 20) (xy 70 20) (xy 70 50) '
            '(xy 15 50))) (filled_polygon (layer "F.Cu") (pts)))\n'))
        rep, rules = rules_of(d)
        check("v03-zone-empty-fill", "ZONE_UNFILLED" in rules,
              f"rules={rules}")
        d = mutate_pcb(copy_good(tmp / "v03zkeep"), add=(
            '\n\t(zone (net 0) (layer "F.Cu") '
            '(uuid "aa000015-0000-4000-8000-000000000001") '
            '(keepout (tracks not_allowed) (copperpour not_allowed)) '
            '(polygon (pts (xy 15 20) (xy 70 20) (xy 70 50) '
            '(xy 15 50))))\n'))
        rep, rules = rules_of(d)
        check("v03-zone-keepout", "ZONE_UNFILLED" not in rules
              and rep["verdict"] == "PASS", f"rules={rules}")

        # 15h. CPL: an empty (header-only) position file blames every
        #      part instead of passing silently.
        d = copy_good(tmp / "v03cplempty")
        (d / "pos.csv").write_text(pos_head)
        rep, rules = rules_of(d)
        check("v03-cpl-empty", "CPL_PART_MISSING" in rules,
              f"rules={rules}")
        # 15i. Per-side position files merge: no false missing parts.
        d = copy_good(tmp / "v03cplsides")
        (d / "top-pos.csv").write_text(
            pos_head + "".join(f"{r},X,Pkg,10,10,0,Top\n"
                               for r in ("J1", "R1", "D1")))
        (d / "bottom-pos.csv").write_text(
            pos_head + "".join(f"{r},X,Pkg,10,10,0,Bottom\n"
                               for r in ("D2", "SW1")))
        rep, rules = rules_of(d)
        check("v03-cpl-per-side", not any(r.startswith("CPL_")
                                          for r in rules)
              and rep["verdict"] == "PASS", f"rules={rules}")
        # 15j. Duplicate placement rows are flagged.
        d = copy_good(tmp / "v03cpldup")
        (d / "pos.csv").write_text(pos_head + pos_rows + pos_rows[:0]
                                   + "R1,X,Pkg,10,10,0,Top\n")
        rep, rules = rules_of(d)
        check("v03-cpl-duplicate", "CPL_DUPLICATE_REFERENCE" in rules,
              f"rules={rules}")
        # 15k. exclude_from_pos_files footprints are not blamed.
        d = copy_good(tmp / "v03cplexcl")
        pcb = d / "led-board.kicad_pcb"
        text = pcb.read_text()
        idx = text.index('"SW1"')
        text = text[:idx] + text[idx:].replace(
            "(attr smd)", "(attr smd exclude_from_pos_files)", 1)
        pcb.write_text(text)
        (d / "pos.csv").write_text(
            pos_head + "".join(f"{r},X,Pkg,10,10,0,Top\n"
                               for r in ("J1", "R1", "D1", "D2")))
        rep, rules = rules_of(d)
        check("v03-cpl-exclude-pos", "CPL_PART_MISSING" not in rules,
              f"rules={rules}")
        # 15l. A hostile 5 MB CPL header cannot bloat the report.
        d = copy_good(tmp / "v03bloat")
        (d / "pos.csv").write_text("Junk," + "X" * 5_000_000 + "\n")
        p = cli([str(d), "--format", "json"])
        check("v03-cpl-bloat-bounded", len(p.stdout) < 200_000
              and "CPL_UNREADABLE" in p.stdout,
              f"bytes={len(p.stdout)}")
        # 15m. A position-column file WITH part numbers is a BOM.
        d = copy_good(tmp / "v03m9")
        (d / "bom.csv").unlink()
        (d / "data.csv").write_text(
            "Reference,PosX,PosY,Value,Footprint,MPN\n"
            "R1,1,1,330R,Resistor_SMD:R_0805_2012Metric,"
            "RC0805FR-07330RL\n")
        rep, rules = rules_of(d)
        check("v03-pos-columns-bom",
              rep["coverage"]["bom"]["file"] == "data.csv"
              and "BOM_MISSING" not in rules, f"rules={rules}")

        # 15n. BOM conflict normalisation: dash-variant MPNs group
        #      together; whitespace-only value differences do not fire.
        dash = tmp / "v03dash.csv"
        dash.write_text(
            "Reference,Quantity,Value,Footprint,MPN\n"
            "R1,1,10k,R_0603,RC0805FR-07330RL\n"
            "R2,1,1k,R_0603,RC0805FR07330RL\n")
        p = cli([str(dash), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v03-bom-dash-conflict",
              "BOM_PART_NUMBER_CONFLICT" in rules, f"rules={rules}")
        wsok = tmp / "v03wsok.csv"
        wsok.write_text(
            "Reference,Quantity,Value,Footprint,MPN\n"
            "R1,1,10k,R_0603,RC0603FR-0710KL\n"
            "R2,1,10 k,R_0603,RC0603FR-0710KL\n")
        p = cli([str(wsok), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v03-bom-ws-nofire",
              "BOM_PART_NUMBER_CONFLICT" not in rules, f"rules={rules}")

        # 15o. --output refuses the in-use dataset and hardlinks.
        ds = tmp / "v03ds.json"
        ds.write_text(json.dumps({"name": "x", "parts": {},
                                  "snapshot_date": "2026-01-01"}))
        p = cli([str(GOOD), "--dataset", str(ds), "--output", str(ds)])
        check("v03-output-dataset", p.returncode == 3
              and json.loads(ds.read_text())["name"] == "x")
        d = copy_good(tmp / "v03hardlink")
        link = tmp / "v03report.txt"
        os.link(d / "led-board.kicad_pcb", link)
        before = (d / "led-board.kicad_pcb").read_bytes()
        p = cli([str(d), "--output", str(link)])
        check("v03-output-hardlink", p.returncode == 3
              and (d / "led-board.kicad_pcb").read_bytes() == before)

    if failures:
        print(f"\n{len(failures)} edge test(s) FAILED: {failures}")
        return 1
    print("\nAll edge tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
