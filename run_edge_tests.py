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

        # ---- §16: v0.4 geometry-lite + Gerber package edge cases ----
        # 16a. A FIFO with a drill-ish name inside a package folder:
        # the package scan stats files but only ever opens regular
        # files under a size guard — it must not hang.
        d = tmp / "v04fifo"; d.mkdir()
        (d / "pkg-F_Cu.gtl").write_text("G04 *\nM02*\n")
        (d / "pkg-B_Cu.gbl").write_text("G04 *\nM02*\n")
        (d / "pkg-Edge_Cuts.gm1").write_text("G04 *\nM02*\n")
        os.mkfifo(d / "drill.txt")
        try:
            p = cli([str(d), "--gerber", "--format", "json"], timeout=15)
            check("v04-gerber-fifo", p.returncode in (1, 2)
                  and "Traceback" not in p.stderr,
                  f"exit={p.returncode}")
        except subprocess.TimeoutExpired:
            check("v04-gerber-fifo", False, "TIMEOUT (hang)")

        # 16b. Object budget: 45k tracks exceed the geometry budget;
        # the layer must abstain honestly (GEOMETRY_SKIPPED), fast.
        d = tmp / "v04budget"; d.mkdir()
        segs = "\n".join(
            f'\t(segment (start {10 + (i % 200)} {10 + (i // 200)}) '
            f'(end {11 + (i % 200)} {10 + (i // 200)}) (width 0.25) '
            f'(layer "F.Cu") (net 1))' for i in range(45000))
        (d / "big.kicad_pcb").write_text(
            '(kicad_pcb\n\t(version 20240108)\n\t(generator "pcbnew")\n'
            '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
            '(44 "Edge.Cuts" user))\n\t(net 0 "")\n\t(net 1 "N")\n'
            + segs + "\n)\n")
        try:
            p = cli([str(d), "--format", "json"], timeout=60)
            rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
            check("v04-geometry-budget", "GEOMETRY_SKIPPED" in rules,
                  f"rules={sorted(rules)[:6]}")
        except subprocess.TimeoutExpired:
            check("v04-geometry-budget", False, "TIMEOUT")

        # 16c. --gerber on a file target is a usage error.
        p = cli([str(GOOD / "bom.csv"), "--gerber"])
        check("v04-gerber-on-file", p.returncode == 3,
              f"exit={p.returncode}")

        # 16d. Mixed naming schemes in one package -> NOTE.
        d = tmp / "v04mixed"; d.mkdir()
        (d / "demo-F_Cu.gtl").write_text("G04 *\nM02*\n")
        (d / "bottom_copper.gbr").write_text("G04 *\nM02*\n")
        (d / "outline.gbr").write_text("G04 *\nM02*\n")
        (d / "holes.drl").write_text("M48\n%%\nM30\n")
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v04-mixed-schemes", "GERBER_MIXED_SCHEMES" in rules,
              f"rules={rules}")

        # 16e. Drill-only package: BLOCK on missing top copper.
        d = tmp / "v04drillonly"; d.mkdir()
        (d / "holes.drl").write_text("M48\n%%\nM30\n")
        p = cli([str(d), "--gerber", "--format", "json"])
        rep = json.loads(p.stdout)
        rules = {f["rule"] for f in rep["findings"]}
        check("v04-drill-only", rep["verdict"] == "BLOCK"
              and "GERBER_TOP_COPPER_MISSING" in rules
              and "GERBER_DRILL_WITHOUT_COPPER" in rules,
              f"verdict={rep['verdict']} rules={rules}")

        # 16f. Deterministic output: identical bytes on repeat runs.
        for target in ("samples/geo-violation", "samples/gerber-incomplete"):
            p1 = cli([str(ROOT / target), "--format", "json"])
            p2 = cli([str(ROOT / target), "--format", "json"])
            check(f"v04-determinism-{Path(target).name}",
                  p1.stdout == p2.stdout and p1.returncode == p2.returncode)

        # 16g. A Gerber-extension file with no determinable role is
        # reported, not silently skipped.
        d = tmp / "v04unknown"; d.mkdir()
        (d / "demo-F_Cu.gtl").write_text("G04 *\nM02*\n")
        (d / "mystery.gbr").write_text("G04 *\nM02*\n")
        p = cli([str(d), "--gerber", "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v04-unrecognised-file", "GERBER_UNRECOGNISED_FILE" in rules,
              f"rules={rules}")

        # 16h/16i. Tiny synthetic boards: arc-sided outline closes;
        # net-0 (unnamed net) pads never pair, however close.
        def tiny_pcb(body):
            return ('(kicad_pcb\n\t(version 20240108)\n'
                    '\t(generator "pcbnew")\n'
                    '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
                    '(44 "Edge.Cuts" user))\n'
                    '\t(net 0 "")\n\t(net 1 "A")\n\t(net 2 "B")\n'
                    + body + "\n)\n")
        d = tmp / "v04arc"; d.mkdir()
        (d / "a.kicad_pcb").write_text(tiny_pcb(
            '\t(gr_line (start 10 10) (end 30 10) (stroke (width 0.05) '
            '(type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_arc (start 30 10) (mid 35 20) (end 30 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 30 30) (end 10 30) (stroke (width 0.05) '
            '(type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 10 30) (end 10 10) (stroke (width 0.05) '
            '(type solid)) (layer "Edge.Cuts"))'))
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v04-arc-outline-closed", "BOARD_OUTLINE_OPEN" not in rules
              and "BOARD_OUTLINE_MISSING" not in rules, f"rules={rules}")
        d = tmp / "v04net0"; d.mkdir()
        (d / "a.kicad_pcb").write_text(tiny_pcb(
            '\t(footprint "R" (layer "F.Cu") (at 20 20) (attr smd)\n'
            '\t\t(pad "1" smd rect (at -0.525 0) (size 1 1) '
            '(layers "F.Cu") (net 0 ""))\n'
            '\t\t(pad "2" smd rect (at 0.525 0) (size 1 1) '
            '(layers "F.Cu") (net 0 "")))\n'
            '\t(gr_rect (start 10 10) (end 40 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v04-net0-never-paired",
              "GEOMETRY_CLEARANCE_BELOW_MIN" not in rules, f"rules={rules}")

        # ---- §17: v0.4 adversarial-review fix regressions ----
        # 17a. Netclass thresholds reach TRACKS (their names come
        # from the (net N "name") table — FP-1/RT-10) and KiCad's
        # pattern list is priority-ordered, FIRST match wins
        # (RT-08/RT-11): HV segments 0.5 mm apart flag when HV*
        # comes first, and are legal when the * catch-all does.
        def hv_board(patterns):
            pro = {"net_settings": {
                "classes": [{"name": "Default", "clearance": 0.2},
                            {"name": "HV", "clearance": 1.0}],
                "netclass_patterns": [
                    {"pattern": pat, "netclass": cls}
                    for pat, cls in patterns]}}
            pcb = ('(kicad_pcb\n\t(version 20240108)\n'
                   '\t(generator "pcbnew")\n'
                   '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
                   '(44 "Edge.Cuts" user))\n'
                   '\t(net 0 "")\n\t(net 1 "HV_A")\n\t(net 2 "HV_B")\n'
                   '\t(segment (start 10 10) (end 20 10) (width 0.25) '
                   '(layer "F.Cu") (net 1))\n'
                   '\t(segment (start 10 10.75) (end 20 10.75) '
                   '(width 0.25) (layer "F.Cu") (net 2))\n'
                   '\t(gr_rect (start 5 5) (end 25 15) '
                   '(stroke (width 0.05) (type solid)) '
                   '(layer "Edge.Cuts"))\n)\n')
            return pcb, json.dumps(pro)
        for name, patterns, expect in (
                ("v04r-track-netclass-first-match",
                 [("HV*", "HV"), ("*", "Default")], True),
                ("v04r-pattern-catchall-first",
                 [("*", "Default"), ("HV*", "HV")], False)):
            d = tmp / name; d.mkdir()
            pcb_text, pro_text = hv_board(patterns)
            (d / "a.kicad_pcb").write_text(pcb_text)
            (d / "a.kicad_pro").write_text(pro_text)
            p = cli([str(d), "--format", "json"])
            rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
            check(name, ("GEOMETRY_CLEARANCE_BELOW_MIN" in rules)
                  == expect, f"rules={rules}")

        # 17b. An outline shattered at EVERY corner still BLOCKs
        # (RT-12): four lines, each corner gapped ~0.04 mm.
        d = tmp / "v04r-four-gaps"; d.mkdir()
        (d / "a.kicad_pcb").write_text(tiny_pcb(
            '\t(gr_line (start 10.03 10) (end 29.97 10) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 30 10.03) (end 30 29.97) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 29.97 30) (end 10.03 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 10 29.97) (end 10 10.03) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v04r-outline-four-gaps", "BOARD_OUTLINE_OPEN" in rules,
              f"rules={rules}")

        # 17c. Legacy (angle ...) arcs chain under BOTH real-world
        # readings (RT-04): centre-in-(start) and endpoints.
        for name, arc in (
                ("v04r-legacy-arc-centre",
                 '\t(gr_arc (start 25 30) (end 40 30) (angle 180) '
                 '(stroke (width 0.05) (type solid)) '
                 '(layer "Edge.Cuts"))'),
                ("v04r-legacy-arc-ends",
                 '\t(gr_arc (start 40 30) (end 10 30) (angle 180) '
                 '(stroke (width 0.05) (type solid)) '
                 '(layer "Edge.Cuts"))')):
            d = tmp / name; d.mkdir()
            (d / "a.kicad_pcb").write_text(tiny_pcb(
                '\t(gr_line (start 10 10) (end 40 10) '
                '(stroke (width 0.05) (type solid)) '
                '(layer "Edge.Cuts"))\n'
                '\t(gr_line (start 40 10) (end 40 30) '
                '(stroke (width 0.05) (type solid)) '
                '(layer "Edge.Cuts"))\n' + arc + '\n'
                '\t(gr_line (start 10 30) (end 10 10) '
                '(stroke (width 0.05) (type solid)) '
                '(layer "Edge.Cuts"))'))
            p = cli([str(d), "--format", "json"])
            rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
            check(name, "BOARD_OUTLINE_OPEN" not in rules,
                  f"rules={rules}")

        # 17d. A through via occupies the inner layers too (RT-15):
        # an In1.Cu track through its centre is a violation.
        d = tmp / "v04r-via-inner"; d.mkdir()
        (d / "a.kicad_pcb").write_text(
            '(kicad_pcb\n\t(version 20240108)\n\t(generator "pcbnew")\n'
            '\t(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) '
            '(31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
            '\t(net 0 "")\n\t(net 1 "A")\n\t(net 2 "B")\n'
            '\t(via (at 20 20) (size 0.6) (drill 0.3) '
            '(layers "F.Cu" "B.Cu") (net 1))\n'
            '\t(segment (start 15 20) (end 25 20) (width 0.25) '
            '(layer "In1.Cu") (net 2))\n'
            '\t(gr_rect (start 10 10) (end 30 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n)\n')
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v04r-via-inner-layer",
              "GEOMETRY_CLEARANCE_BELOW_MIN" in rules, f"rules={rules}")

        # 17e. One board-spanning segment must not blow up the
        # grid (RT-01: was 19 s / 850 MB): it still gets its
        # copper-to-edge verdict, fast.
        d = tmp / "v04r-giant-span"; d.mkdir()
        (d / "a.kicad_pcb").write_text(tiny_pcb(
            '\t(segment (start -1000000 20) (end 1000000 20) '
            '(width 0.25) (layer "F.Cu") (net 1))\n'
            '\t(gr_rect (start 10 10) (end 40 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        try:
            p = cli([str(d), "--format", "json"], timeout=20)
            rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
            check("v04r-giant-span-segment",
                  "GEOMETRY_COPPER_TO_EDGE" in rules, f"rules={rules}")
        except subprocess.TimeoutExpired:
            check("v04r-giant-span-segment", False, "TIMEOUT")

        # 17f. Gerber fix pack: PTH+NPTH split is not a duplicate
        # (FP-2); In1..In8 KiCad names classify (RT-06); dot-
        # separated names classify (RT-07); a BOM-prefixed
        # Excellon .txt is a drill file (RT-09); .g1-.g10 inners
        # have no gap (RT-05).
        d = tmp / "v04r-pthnpth"; d.mkdir()
        for fname in ("b-F_Cu.gtl", "b-B_Cu.gbl", "b-F_Mask.gts",
                      "b-B_Mask.gbs", "b-F_Silkscreen.gto",
                      "b-Edge_Cuts.gm1", "b-PTH.drl", "b-NPTH.drl"):
            (d / fname).write_text("G04 *\nM02*\n")
        p = cli([str(d), "--format", "json"])
        rep = json.loads(p.stdout)
        rules = {f["rule"] for f in rep["findings"]}
        check("v04r-pth-npth-not-duplicate",
              "GERBER_DUPLICATE_LAYER" not in rules
              and rep["verdict"] == "PASS", f"rules={rules}")
        d = tmp / "v04r-in78"; d.mkdir()
        names = ["b-F_Cu.gtl", "b-B_Cu.gbl", "b-F_Mask.gts",
                 "b-B_Mask.gbs", "b-F_Silkscreen.gto",
                 "b-Edge_Cuts.gm1", "b.drl"] + \
            [f"b-In{i}_Cu.gtl" for i in range(1, 9)]
        for fname in names:
            (d / fname).write_text("G04 *\nM02*\n")
        p = cli([str(d), "--format", "json"])
        rep = json.loads(p.stdout)
        rules = {f["rule"] for f in rep["findings"]}
        check("v04r-kicad-in7-in8", rep["verdict"] == "PASS",
              f"rules={rules}")
        d = tmp / "v04r-dotnames"; d.mkdir()
        for fname in ("board.F_Cu.gbr", "board.B_Cu.gbr",
                      "board.F_Mask.gbr", "board.B_Mask.gbr",
                      "board.F_Silkscreen.gbr", "board.Edge_Cuts.gbr",
                      "board.drl"):
            (d / fname).write_text("G04 *\nM02*\n")
        p = cli([str(d), "--format", "json"])
        rep = json.loads(p.stdout)
        rules = {f["rule"] for f in rep["findings"]}
        check("v04r-dot-separated-names", rep["verdict"] == "PASS",
              f"rules={rules}")
        d = tmp / "v04r-bom-drill"; d.mkdir()
        for fname in ("x.gtl", "x.gbl", "x.gts", "x.gbs", "x.gto",
                      "x.gko"):
            (d / fname).write_text("G04 *\nM02*\n")
        (d / "drill.txt").write_bytes(
            b"\xef\xbb\xbf; generated by CAM\nM48\n%\nM30\n")
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v04r-excellon-bom-comment",
              "GERBER_DRILL_MISSING" not in rules, f"rules={rules}")
        d = tmp / "v04r-g10"; d.mkdir()
        for fname in ("x.gtl", "x.gbl", "x.gts", "x.gbs", "x.gto",
                      "x.gko", "x.drl"):
            (d / fname).write_text("G04 *\nM02*\n")
        for i in range(1, 11):
            (d / f"x.g{i}").write_text("G04 *\nM02*\n")
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v04r-inner-1-to-10-nogap",
              "GERBER_INNER_LAYER_GAP" not in rules, f"rules={rules}")

        # ---- §18: v0.4 code-audit fix regressions (AUDIT F1-F8) ----
        # 18a. A circular outline is real geometry (F2): a pad
        # crossing it fires copper-to-edge; a centred pad doesn't.
        for name, fy, expect in (
                ("v05-circle-outline-crossing", 10.1, True),
                ("v05-circle-outline-inside", 25.0, False)):
            d = tmp / name; d.mkdir()
            (d / "a.kicad_pcb").write_text(tiny_pcb(
                '\t(gr_circle (center 25 25) (end 40 25) '
                '(stroke (width 0.05) (type solid)) '
                '(layer "Edge.Cuts"))\n'
                '\t(footprint "R" (layer "F.Cu") '
                f'(at 25 {fy}) (attr smd)\n'
                '\t\t(pad "1" smd rect (at 0 0) (size 1 1) '
                '(layers "F.Cu") (net 1 "A")))\n'))
            p = cli([str(d), "--format", "json"])
            rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
            check(name, ("GEOMETRY_COPPER_TO_EDGE" in rules) == expect,
                  f"rules={rules}")

        # 18b. Candidate enumeration is budgeted (F1): 4,000
        # same-net tracks in ~2 mm^2 abstain, fast, instead of
        # enumerating ~8M candidate pairs unbounded.
        d = tmp / "v05dense"; d.mkdir()
        segs = "\n".join(
            f'\t(segment (start {10 + (i % 64) * 0.015:.3f} '
            f'{10 + (i // 64) * 0.015:.3f}) '
            f'(end {10 + (i % 64) * 0.015 + 0.01:.3f} '
            f'{10 + (i // 64) * 0.015:.3f}) (width 0.01) '
            f'(layer "F.Cu") (net 1))' for i in range(4000))
        (d / "a.kicad_pcb").write_text(tiny_pcb(segs))
        try:
            p = cli([str(d), "--format", "json"], timeout=30)
            rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
            check("v05-candidate-budget", "GEOMETRY_SKIPPED" in rules,
                  f"rules={sorted(rules)[:6]}")
        except subprocess.TimeoutExpired:
            check("v05-candidate-budget", False, "TIMEOUT")

        # 18c. Budgets bite during construction (F3): 300 huge
        # arcs would subdivide to ~300k objects; the layer must
        # abstain without materialising them.
        d = tmp / "v05arcs"; d.mkdir()
        arcs = "\n".join(
            '\t(arc (start 0 0) (mid 10000 10000) (end 20000 0) '
            '(width 0.25) (layer "F.Cu") (net 1))'
            for _ in range(300))
        (d / "a.kicad_pcb").write_text(tiny_pcb(arcs))
        try:
            p = cli([str(d), "--format", "json"], timeout=30)
            rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
            check("v05-arc-amplification-budget",
                  "GEOMETRY_SKIPPED" in rules, f"rules={sorted(rules)[:6]}")
        except subprocess.TimeoutExpired:
            check("v05-arc-amplification-budget", False, "TIMEOUT")

        # 18d. A negative-size pad is excluded, not measured
        # from a negative radius (F7): the foreign pad inside it
        # produces no clearance finding, and Coverage owns up.
        d = tmp / "v05negpad"; d.mkdir()
        (d / "a.kicad_pcb").write_text(tiny_pcb(
            '\t(footprint "R1" (layer "F.Cu") (at 20 20) (attr smd)\n'
            '\t\t(pad "1" smd rect (at 0 0) (size -2 -2) '
            '(layers "F.Cu") (net 1 "A")))\n'
            '\t(footprint "R2" (layer "F.Cu") (at 20 20) (attr smd)\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 0.5 0.5) '
            '(layers "F.Cu") (net 2 "B")))\n'
            '\t(gr_rect (start 10 10) (end 30 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v05-negative-pad-excluded",
              "GEOMETRY_CLEARANCE_BELOW_MIN" not in rules
              and "SCAN_INCOMPLETE" in rules, f"rules={rules}")

        # 18e. A filename is not a stackup (F5): one .g200000
        # file gets a bounded gap finding, not a 189 MB report.
        d = tmp / "v05hugeinner"; d.mkdir()
        for fname in ("x.gtl", "x.gbl", "x.gts", "x.gbs", "x.gto",
                      "x.gko", "x.drl", "x.g200000"):
            (d / fname).write_text("G04 *\nM02*\n")
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v05-gerber-inner-huge",
              "GERBER_INNER_LAYER_GAP" in rules
              and len(p.stdout) < 50000, f"out={len(p.stdout)}")

        # 18f. Generic Gerber names need a fab extension (F6):
        # "bottom_copper.txt" must not satisfy bottom copper.
        d = tmp / "v05notesspoof"; d.mkdir()
        for fname in ("x-F_Cu.gtl", "x-Edge_Cuts.gm1", "x.drl"):
            (d / fname).write_text("G04 *\nM02*\n")
        (d / "bottom_copper.txt").write_text("these are notes\n")
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v05-gerber-notes-spoof",
              "GERBER_BOTTOM_COPPER_MISSING" in rules, f"rules={rules}")

        # 18g. A lone .kicad_pro does not hijack a Gerber folder
        # (F8): a complete package beside one still auto-detects.
        d = tmp / "v05proonly"; d.mkdir()
        (d / "a.kicad_pro").write_text("{}\n")
        for fname in ("b-F_Cu.gtl", "b-B_Cu.gbl", "b-F_Mask.gts",
                      "b-B_Mask.gbs", "b-F_Silkscreen.gto",
                      "b-Edge_Cuts.gm1", "b.drl"):
            (d / fname).write_text("G04 *\nM02*\n")
        p = cli([str(d), "--format", "json"])
        rep = json.loads(p.stdout)
        check("v05-gerber-lone-kicad-pro", rep["verdict"] == "PASS",
              f"verdict={rep['verdict']}")

        # 18h. .g0 is not an inner layer (F5/F8 tail): no
        # nonsense gap finding.
        d = tmp / "v05g0"; d.mkdir()
        for fname in ("x.gtl", "x.gbl", "x.gts", "x.gbs", "x.gto",
                      "x.gko", "x.drl", "x.g0"):
            (d / fname).write_text("G04 *\nM02*\n")
        p = cli([str(d), "--format", "json"])
        rules = {f["rule"] for f in json.loads(p.stdout)["findings"]}
        check("v05-gerber-g0-not-inner",
              "GERBER_INNER_LAYER_GAP" not in rules, f"rules={rules}")

    if failures:
        print(f"\n{len(failures)} edge test(s) FAILED: {failures}")
        return 1
    print("\nAll edge tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
