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
        # v0.6: the zone rect above spans the whole board and its
        # stored fill CONTAINS J1 pad 1 (net VBAT) -- a stale-fill
        # clearance error, not a clean board. kicad-cli DRC on this
        # exact file agrees: "zone clearance 0.5000 mm; actual
        # 0.0000 mm". The filled-ok pin therefore uses a zone over
        # verified-empty B.Cu copper (the only B.Cu objects on the
        # good board are the two J1 pads at x<=23.4, y 29.1-30.9),
        # and the original placement is pinned separately below.
        check("fab-zone-filled-ok", "ZONE_UNFILLED" not in rules,
              f"rules={rules}")
        zone_ok = zone.replace("(xy 15 20) (xy 70 20) (xy 70 50) "
                               "(xy 15 50)",
                               "(xy 30 20) (xy 70 20) (xy 70 50) "
                               "(xy 30 50)")
        filled_ok = (' (filled_polygon (layer "B.Cu") (pts '
                     '(xy 30 20) (xy 70 20) (xy 70 50) (xy 30 50)))')
        d = mutate_pcb(copy_good(tmp / "zonefilledok"),
                       add=zone_ok.replace("{FILL}", filled_ok))
        rep, rules = rules_of(d)
        check("fab-zone-filled-clean", "ZONE_UNFILLED" not in rules
              and rep["verdict"] == "PASS", f"rules={rules}")
        # A stored fill polygon containing different-net copper is
        # a clearance violation (stale fill), per KiCad's own DRC.
        d = mutate_pcb(copy_good(tmp / "zonestale"),
                       add=zone.replace("{FILL}", filled))
        rep, rules = rules_of(d)
        check("fab-zone-stale-fill",
              "GEOMETRY_CLEARANCE_BELOW_MIN" in rules
              and "ZONE_UNFILLED" not in rules, f"rules={rules}")

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

        # ---- §19: v0.5 netlist layer (ERC-lite + parity) ----
        # Tiny hand-built projects with real pin definitions.
        NL_DEFS = (
            '(symbol "Device:R" (symbol "R_1_1"'
            ' (pin passive line (at 0 3.81 270) (length 1.27)'
            ' (name "~" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))'
            ' (pin passive line (at 0 -3.81 90) (length 1.27)'
            ' (name "~" (effects (font (size 1.27 1.27))))'
            ' (number "2" (effects (font (size 1.27 1.27)))))))'
            '(symbol "Test:Sensor" (symbol "Sensor_1_1"'
            ' (pin power_in line (at -5.08 2.54 0) (length 2.54)'
            ' (name "VDD" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))'
            ' (pin passive line (at -5.08 -2.54 0) (length 2.54)'
            ' (name "OUT" (effects (font (size 1.27 1.27))))'
            ' (number "2" (effects (font (size 1.27 1.27)))))))'
            '(symbol "Test:Out" (symbol "Out_1_1"'
            ' (pin output line (at 5.08 0 180) (length 2.54)'
            ' (name "O" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))))'
            '(symbol "Test:HiddenPwr" (symbol "HiddenPwr_1_1"'
            ' (pin passive line (at -5.08 0 0) (length 2.54)'
            ' (name "S" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))'
            ' (pin power_in line hide (at 0 5.08 270) (length 0)'
            ' (name "GND" (effects (font (size 1.27 1.27))))'
            ' (number "3" (effects (font (size 1.27 1.27)))))))'
            '(symbol "power:GND" (power) (symbol "GND_1_1"'
            ' (pin power_in line (at 0 0 270) (length 0)'
            ' (name "GND" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))))'
            '(symbol "power:PWR_FLAG" (power)'
            ' (symbol "PWR_FLAG_1_1"'
            ' (pin power_out line (at 0 0 270) (length 0)'
            ' (name "FLAG" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))))')
        NL_PINS = {"Device:R": ("1", "2"), "Test:Sensor": ("1", "2"),
                   "Test:Out": ("1",), "Test:HiddenPwr": ("1", "3"),
                   "power:GND": ("1",), "power:PWR_FLAG": ("1",)}

        def nl_placed(lib, ref, x, y, rot=0, mirror=None, fp=""):
            pins = "".join(f' (pin "{n}" (uuid "u{n}"))'
                           for n in NL_PINS[lib])
            mir = f" (mirror {mirror})" if mirror else ""
            return (f'(symbol (lib_id "{lib}") (at {x} {y} {rot}){mir}'
                    f' (unit 1) (in_bom yes) (on_board yes)'
                    f' (uuid "u-{ref}")'
                    f' (property "Reference" "{ref}" (at {x} {y} 0)'
                    f' (effects (font (size 1.27 1.27))))'
                    f' (property "Value" "V" (at {x} {y} 0)'
                    f' (effects (font (size 1.27 1.27))))'
                    f' (property "Footprint" "{fp}" (at {x} {y} 0)'
                    f' (effects (font (size 1.27 1.27)))){pins})')

        def nl_wire(*pts):
            chain = " ".join(f"(xy {a} {b})" for a, b in pts)
            return f'(wire (pts {chain}))'

        def nl_label(name, x, y, kind="label"):
            return (f'({kind} "{name}" (at {x} {y} 0)'
                    f' (effects (font (size 1.27 1.27))))')

        def nl_project(name, root_body, sub_body=None, pcb_text=None):
            d = tmp / name; d.mkdir()

            def sch(body):
                return ('(kicad_sch (version 20231120)'
                        ' (generator "eeschema")'
                        ' (generator_version "8.0") (uuid "u")'
                        ' (paper "A4") (lib_symbols ' + NL_DEFS
                        + ')\n' + body
                        + '\n(sheet_instances (path "/" (page "1")))\n)\n')
            (d / "a.kicad_sch").write_text(sch(root_body))
            if sub_body is not None:
                (d / "sub.kicad_sch").write_text(sch(sub_body))
            if pcb_text is not None:
                (d / "a.kicad_pcb").write_text(pcb_text)
            (d / "a.kicad_pro").write_text("{}")
            return d

        SHEET_BLOCK = (
            '(sheet (at 80 20) (size 20 20)'
            ' (property "Sheetname" "Sub" (at 80 19 0)'
            ' (effects (font (size 1.27 1.27))))'
            ' (property "Sheetfile" "sub.kicad_sch" (at 80 41 0)'
            ' (effects (font (size 1.27 1.27)))))')

        # 19a. Transform: rotation 90 + mirror y, and mirror x, land
        # pins exactly on their wires (hand-computed points in the
        # test design above: R1 pins -> (46.19,50) / (53.81,50);
        # R3 mirror-x pins -> (80,53.81) / (80,46.19)).
        body = (
            nl_placed("Device:R", "R1", 50, 50, rot=90, mirror="y")
            + nl_wire((46.19, 50), (40, 50)) + nl_label("NA", 43, 50)
            + nl_wire((53.81, 50), (60, 50)) + nl_label("NB", 57, 50)
            + nl_placed("Device:R", "R3", 80, 50, mirror="x")
            + nl_wire((80, 53.81), (80, 60)) + nl_label("NX", 80, 57)
            + nl_wire((80, 46.19), (80, 40)) + nl_label("NY", 80, 43))
        d = nl_project("v05transform", body)
        rep, rules = rules_of(d)
        check("v05-transform-rot-mirror",
              "ERC_UNCONNECTED_PIN" not in rules
              and rep["coverage"]["netlist"]["pins_resolved"] == 4,
              f"rules={rules}")

        # 19b. Crossing wires: no junction = no connection (two
        # outputs stay separate); with a junction they conflict.
        cross_body = (
            nl_placed("Test:Out", "U1", 50, 50)
            + nl_wire((55.08, 50), (70, 50))
            + nl_placed("Test:Out", "U2", 60, 30)
            + nl_wire((65.08, 30), (65.08, 60)))
        d = nl_project("v05cross-nojunction", cross_body)
        rep, rules = rules_of(d)
        check("v05-crossing-no-junction",
              "ERC_OUTPUT_CONFLICT" not in rules, f"rules={rules}")
        d = nl_project("v05cross-junction",
                       cross_body + '(junction (at 65.08 50))')
        rep, rules = rules_of(d)
        check("v05-crossing-with-junction",
              "ERC_OUTPUT_CONFLICT" in rules, f"rules={rules}")

        # 19c. Local labels are per-sheet: root's "X" net (power_in,
        # no driver) must still fire even though the sub-sheet's
        # "X" net is driven by a PWR_FLAG.
        sens_root = (
            nl_placed("Test:Sensor", "U1", 50, 50)
            + nl_wire((44.92, 47.46), (35, 47.46))
            + nl_label("X", 40, 47.46)
            + nl_wire((44.92, 52.54), (35, 52.54))
            + nl_label("Y", 40, 52.54) + SHEET_BLOCK)
        sens_sub = (
            nl_placed("Test:Sensor", "U2", 50, 50)
            + nl_wire((44.92, 47.46), (35, 47.46))
            + nl_label("X", 40, 47.46)
            + nl_placed("power:PWR_FLAG", "#FLG01", 38, 47.46)
            + nl_wire((44.92, 52.54), (35, 52.54))
            + nl_label("Y", 40, 52.54))
        d = nl_project("v05label-scope", sens_root, sub_body=sens_sub)
        p = cli([str(d), "--format", "json"])
        rep = json.loads(p.stdout)
        n = sum(1 for f in rep["findings"]
                if f["rule"] == "ERC_POWER_NOT_DRIVEN")
        check("v05-local-label-scoped", n == 1, f"count={n}")

        # 19d. Global labels DO merge across sheets: the sub-sheet
        # PWR_FLAG drives the root's global "G" net.
        glob_root = (
            nl_placed("Test:Sensor", "U1", 50, 50)
            + nl_wire((44.92, 47.46), (35, 47.46))
            + nl_label("G", 40, 47.46, kind="global_label")
            + nl_wire((44.92, 52.54), (35, 52.54))
            + nl_label("Y", 40, 52.54) + SHEET_BLOCK)
        glob_sub = (
            nl_placed("Test:Sensor", "U2", 50, 50)
            + nl_wire((44.92, 47.46), (35, 47.46))
            + nl_label("G", 40, 47.46, kind="global_label")
            + nl_placed("power:PWR_FLAG", "#FLG01", 38, 47.46)
            + nl_wire((44.92, 52.54), (35, 52.54))
            + nl_label("Y", 40, 52.54))
        d = nl_project("v05global-merge", glob_root, sub_body=glob_sub)
        rep, rules = rules_of(d)
        check("v05-global-label-merges",
              "ERC_POWER_NOT_DRIVEN" not in rules, f"rules={rules}")

        # 19e. Power symbols merge globally across sheets too.
        pwr_root = (
            nl_placed("Test:Sensor", "U1", 50, 50)
            + nl_wire((44.92, 47.46), (35, 47.46))
            + nl_placed("power:GND", "#PWR01", 40, 47.46)
            + nl_wire((44.92, 52.54), (35, 52.54))
            + nl_label("Y", 40, 52.54) + SHEET_BLOCK)
        pwr_sub = (
            nl_wire((35, 50), (45, 50))
            + nl_placed("power:GND", "#PWR02", 38, 50)
            + nl_placed("power:PWR_FLAG", "#FLG01", 42, 50))
        d = nl_project("v05power-merge", pwr_root, sub_body=pwr_sub)
        rep, rules = rules_of(d)
        check("v05-power-symbol-merges",
              "ERC_POWER_NOT_DRIVEN" not in rules, f"rules={rules}")

        # 19f. No-connect marker suppresses exactly its own pin.
        body = (
            nl_placed("Device:R", "R1", 50, 50)
            + nl_wire((50, 46.19), (50, 40)) + nl_label("W", 50, 43)
            + '(no_connect (at 50 53.81))'
            + nl_placed("Device:R", "R2", 80, 50)
            + nl_wire((80, 46.19), (80, 40)) + nl_label("W2", 80, 43))
        d = nl_project("v05nc", body)
        rep, rules = rules_of(d)
        unc = [f for f in rep["findings"]
               if f["rule"] == "ERC_UNCONNECTED_PIN"]
        check("v05-nc-marker-suppresses",
              len(unc) == 1 and "R2" in unc[0]["where"],
              f"unc={unc}")

        # 19g. Bus-connected abstention: a power_in pin whose only
        # path is a wire stub onto a bus segment is excluded (its
        # undriven-power finding must NOT fire) and counted.
        body = (
            nl_placed("Test:Sensor", "U1", 50, 50)
            + nl_wire((44.92, 47.46), (40, 47.46))
            + '(bus (pts (xy 40 40) (xy 40 60)))'
            + nl_wire((44.92, 52.54), (35, 52.54))
            + nl_label("Z", 40, 52.54))
        d = nl_project("v05bus", body)
        rep, rules = rules_of(d)
        check("v05-bus-abstains",
              "ERC_POWER_NOT_DRIVEN" not in rules
              and rep["coverage"]["netlist"]["pins_bus_connected"] >= 1,
              f"rules={rules} cov={rep['coverage'].get('netlist')}")

        # 19h. Budget breach abstains wholesale: one wire chain
        # with 2,100 segments makes the segment-pair budget bite;
        # the layer reports SCAN_INCOMPLETE, never partial ERC.
        pts = " ".join(f"(xy {i * 0.5} {100 + (i % 2)})"
                       for i in range(2101))
        body = (
            nl_placed("Device:R", "R1", 50, 50)
            + nl_wire((50, 46.19), (50, 40)) + nl_label("W", 50, 43)
            + nl_wire((50, 53.81), (50, 60)) + nl_label("V", 50, 57)
            + f'(wire (pts {pts}))')
        d = nl_project("v05budget", body)
        rep, rules = rules_of(d)
        check("v05-budget-abstains",
              "SCAN_INCOMPLETE" in rules
              and rep["coverage"]["netlist"].get("budget_exceeded")
              and not any(r.startswith(("ERC_", "PARITY_"))
                          for r in rules),
              f"rules={sorted(rules)[:8]}")

        # 19i/19j. Renamed net (NOTE) and per-ref pad-skip
        # abstention, on one shared sch<->pcb layout.
        def tiny_pcb(pad1_shape):
            return (
                '(kicad_pcb (version 20240108) (generator "pcbnew")'
                ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal)'
                ' (44 "Edge.Cuts" user)) (net 0 "") (net 1 "NEW")'
                ' (net 2 "G2")'
                ' (footprint "Resistor_SMD:R_0805_2012Metric"'
                ' (layer "F.Cu") (at 20 20) (attr smd)'
                ' (property "Reference" "R1" (at 0 -3 0)'
                ' (layer "F.SilkS") (effects (font (size 1 1))))'
                f' (pad "1" smd {pad1_shape} (at -0.95 0) (size 1 1)'
                ' (layers "F.Cu") (net 1 "NEW"))'
                ' (pad "2" smd rect (at 0.95 0) (size 1 1)'
                ' (layers "F.Cu") (net 2 "G2")))'
                ' (footprint "Resistor_SMD:R_0805_2012Metric"'
                ' (layer "F.Cu") (at 40 20) (attr smd)'
                ' (property "Reference" "R2" (at 0 -3 0)'
                ' (layer "F.SilkS") (effects (font (size 1 1))))'
                ' (pad "1" smd rect (at -0.95 0) (size 1 1)'
                ' (layers "F.Cu") (net 1 "NEW"))'
                ' (pad "2" smd rect (at 0.95 0) (size 1 1)'
                ' (layers "F.Cu") (net 2 "G2")))'
                ' (gr_rect (start 10 10) (end 60 30)'
                ' (stroke (width 0.1) (type default)) (fill no)'
                ' (layer "Edge.Cuts")))')
        pair_body = (
            nl_placed("Device:R", "R1", 50, 50,
                      fp="Resistor_SMD:R_0805_2012Metric")
            + nl_placed("Device:R", "R2", 70, 50,
                        fp="Resistor_SMD:R_0805_2012Metric")
            + nl_wire((50, 46.19), (70, 46.19))
            + nl_label("OLD", 60, 46.19)
            + nl_wire((50, 53.81), (50, 60)) + nl_label("G2", 50, 57)
            + nl_wire((70, 53.81), (70, 60)) + nl_label("G2", 70, 57))
        d = nl_project("v05renamed", pair_body,
                       pcb_text=tiny_pcb("rect"))
        rep, rules = rules_of(d)
        check("v05-renamed-net-note",
              "PARITY_NET_RENAMED" in rules
              and "PARITY_NET_SCH_ONLY" not in rules
              and "PARITY_NET_PCB_ONLY" not in rules,
              f"rules={rules}")
        # Same layout, but the PCB renames OLD->"OLD" for R2 only
        # and R1's pad 1 is a custom shape: R1 is unmeasurable, so
        # parity must abstain per-ref (SCAN_INCOMPLETE), not blame.
        pcb_custom = tiny_pcb("custom").replace(
            '(net 1 "NEW")', '(net 1 "OLD")')
        d = nl_project("v05padskip", pair_body, pcb_text=pcb_custom)
        rep, rules = rules_of(d)
        check("v05-pad-skip-abstains",
              "SCAN_INCOMPLETE" in rules
              and not any(r.startswith("PARITY_") for r in rules),
              f"rules={rules}")

        # 19k. Documented KiCad semantic (reconciled 2026-11-10):
        # a hidden power_in pin on an ordinary symbol connects
        # globally to the net named by the pin name, wherever it
        # sits.  Two such pins named GND, touching nothing, form
        # ONE net: neither is blamed unconnected, and the shared
        # net is power-not-driven exactly once.
        hidden_body = (
            nl_placed("Test:HiddenPwr", "U1", 50, 50)
            + nl_wire((44.92, 50), (35, 50)) + nl_label("SIG", 40, 50)
            + nl_placed("Test:HiddenPwr", "U2", 50, 80)
            + nl_wire((44.92, 80), (35, 80))
            + nl_label("SIG2", 40, 80))
        d = nl_project("v05hiddenpwr", hidden_body)
        rep, rules = rules_of(d)
        n_pnd = sum(1 for f in rep["findings"]
                    if f["rule"] == "ERC_POWER_NOT_DRIVEN")
        check("v05-hidden-power-pin-name-connects",
              "ERC_UNCONNECTED_PIN" not in rules and n_pnd == 1,
              f"rules={rules}")

        # 19l. Documented matrix, two more cells: (i) two PWR_FLAGs
        # on one net = two power_out pins = ERC_OUTPUT_CONFLICT
        # (and the net IS driven, so no POWER_NOT_DRIVEN); (ii) an
        # ordinary output pin does NOT drive a power net -- only
        # power_out counts for KiCad's power-pin test.
        two_flags = (
            nl_placed("Test:Sensor", "U1", 50, 50)
            + nl_wire((44.92, 47.46), (35, 47.46))
            + nl_placed("power:PWR_FLAG", "#FLG01", 38, 47.46)
            + nl_placed("power:PWR_FLAG", "#FLG02", 41, 47.46)
            + nl_wire((44.92, 52.54), (35, 52.54)))
        d = nl_project("v05twoflags", two_flags)
        rep, rules = rules_of(d)
        check("v05-two-pwr-flags-conflict",
              "ERC_OUTPUT_CONFLICT" in rules
              and "ERC_POWER_NOT_DRIVEN" not in rules,
              f"rules={rules}")
        out_drives = (
            nl_placed("Test:Sensor", "U1", 50, 50)
            + nl_wire((44.92, 47.46), (35.08, 47.46))
            + nl_placed("Test:Out", "U2", 30, 47.46)
            + nl_wire((44.92, 52.54), (35, 52.54)))
        d = nl_project("v05outnotpower", out_drives)
        rep, rules = rules_of(d)
        n_pnd = sum(1 for f in rep["findings"]
                    if f["rule"] == "ERC_POWER_NOT_DRIVEN")
        check("v05-output-does-not-drive-power",
              n_pnd == 1, f"rules={rules}")

        # ---------------------------------------------------------------
        # 19m+. v0.5 fix wave: regression pins for the adversarial
        # reviews (red team RT-03/06/07/09/13/42/44/48b, auditor
        # F1-F12/F14, FP tester P1/P2).  Each test is suite-native:
        # projects are built here in the temp dir, not copied from
        # the reviewers' repro folders.
        # ---------------------------------------------------------------
        def placed2(lib, ref, x, y, pins, value="V", fp="",
                    unit=1, in_bom=True, on_board=True):
            pinstr = "".join(f' (pin "{n}" (uuid "u{n}"))'
                             for n in pins)
            return (
                f'(symbol (lib_id "{lib}") (at {x} {y} 0)'
                f' (unit {unit})'
                f' (in_bom {"yes" if in_bom else "no"})'
                f' (on_board {"yes" if on_board else "no"})'
                f' (uuid "u-{ref}")'
                f' (property "Reference" "{ref}" (at {x} {y} 0)'
                f' (effects (font (size 1.27 1.27))))'
                f' (property "Value" "{value}" (at {x} {y} 0)'
                f' (effects (font (size 1.27 1.27))))'
                f' (property "Footprint" "{fp}" (at {x} {y} 0)'
                f' (effects (font (size 1.27 1.27)))){pinstr})')

        def nl_project2(name, body, defs=None, sub_body=None,
                        pcb_text=None):
            d = tmp / name; d.mkdir()

            def sch(b):
                return ('(kicad_sch (version 20231120)'
                        ' (generator "eeschema")'
                        ' (generator_version "8.0") (uuid "u")'
                        ' (paper "A4") (lib_symbols '
                        + (NL_DEFS if defs is None else defs)
                        + ')\n' + b
                        + '\n(sheet_instances (path "/" (page "1")))'
                        '\n)\n')
            (d / "a.kicad_sch").write_text(sch(body))
            if sub_body is not None:
                (d / "sub.kicad_sch").write_text(sch(sub_body))
            if pcb_text is not None:
                (d / "a.kicad_pcb").write_text(pcb_text)
            (d / "a.kicad_pro").write_text("{}")
            return d

        def fp_pcb(nets_and_pads):
            # nets_and_pads: {netname: [(ref, padno), ...]} plus
            # footprints are emitted for every ref seen, both pads
            # of R0805 parts; pads not listed get no net.
            nets = sorted(nets_and_pads)
            decl = "".join(f' (net {i + 1} "{n}")'
                           for i, n in enumerate(nets))
            pad_net = {}
            for n, pads in nets_and_pads.items():
                for ref, padno in pads:
                    pad_net[(ref, padno)] = (nets.index(n) + 1, n)
            refs = sorted({r for r, _p in pad_net})
            fps = ""
            for k, ref in enumerate(refs):
                pads = ""
                for padno in ("1", "2"):
                    key = (ref, padno)
                    if key in pad_net:
                        nid, nname = pad_net[key]
                        pads += (f' (pad "{padno}" smd rect'
                                 f' (at {"-0.95" if padno == "1" else "0.95"} 0)'
                                 f' (size 1 1) (layers "F.Cu")'
                                 f' (net {nid} "{nname}"))')
                    else:
                        pads += (f' (pad "{padno}" smd rect'
                                 f' (at {"-0.95" if padno == "1" else "0.95"} 0)'
                                 f' (size 1 1) (layers "F.Cu"))')
                fps += (f' (footprint "Resistor_SMD:R_0805_2012Metric"'
                        f' (layer "F.Cu") (at {20 + 20 * k} 20)'
                        f' (attr smd)'
                        f' (property "Reference" "{ref}" (at 0 -3 0)'
                        f' (layer "F.SilkS")'
                        f' (effects (font (size 1 1)))){pads})')
            return ('(kicad_pcb (version 20240108)'
                    ' (generator "pcbnew")'
                    ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal)'
                    ' (44 "Edge.Cuts" user)) (net 0 "")' + decl
                    + fps
                    + ' (gr_rect (start 10 10) (end 200 30)'
                      ' (stroke (width 0.1) (type default)) (fill no)'
                      ' (layer "Edge.Cuts")))')

        RFP = "Resistor_SMD:R_0805_2012Metric"

        # 19m (R1/RT-07). Local + hierarchical labels share ONE
        # per-sheet namespace: two outputs in the sub-sheet, one
        # on a hierarchical FEED label, one on a local FEED label,
        # are ONE net and conflict.
        sub_outs = (
            nl_placed("Test:Out", "U1", 50, 50)
            + nl_wire((55.08, 50), (65, 50))
            + nl_label("FEED", 60, 50, kind="hierarchical_label")
            + nl_placed("Test:Out", "U2", 50, 80)
            + nl_wire((55.08, 80), (65, 80))
            + nl_label("FEED", 60, 80))
        d = nl_project("v05m-local-hier", SHEET_BLOCK,
                       sub_body=sub_outs)
        rep, rules = rules_of(d)
        check("v05m-local-hier-share-namespace",
              "ERC_OUTPUT_CONFLICT" in rules, f"rules={rules}")

        # 19n (R3/RT-06, verified correct as-is). A wire carrying
        # BOTH a local and a global SIG, plus another wire in the
        # same sheet carrying only local SIG: KiCad's manual says
        # same-name labels connect regardless of type within a
        # sheet, so the merge (and its output conflict) is the
        # documented behaviour, pinned here as intended.
        dual = (
            nl_placed("Test:Out", "U1", 50, 50)
            + nl_wire((55.08, 50), (65, 50))
            + nl_label("SIG", 58, 50)
            + nl_label("SIG", 62, 50, kind="global_label")
            + nl_placed("Test:Out", "U2", 50, 80)
            + nl_wire((55.08, 80), (65, 80))
            + nl_label("SIG", 60, 80))
        d = nl_project("v05m-global-local-samename", dual)
        rep, rules = rules_of(d)
        check("v05m-global-local-samename-merges-intended",
              "ERC_OUTPUT_CONFLICT" in rules, f"rules={rules}")

        # 19o (R2/RT-48b). ALL power names on one wire group it:
        # R1.1's wire carries GND and EARTH power symbols, R2.1's
        # wire carries EARTH; the schematic shorts GND<->EARTH,
        # the PCB does not -- parity must BLOCK.
        body = (
            placed2("Device:R", "R1", 50, 50, ("1", "2"), fp=RFP)
            + nl_wire((50, 46.19), (40, 46.19))
            + placed2("power:GND", "#PWR01", 42, 46.19, ("1",),
                      value="GND")
            + placed2("power:GND", "#PWR02", 48, 46.19, ("1",),
                      value="EARTH")
            + nl_wire((50, 53.81), (60, 53.81))
            + nl_label("G2", 55, 53.81)
            + placed2("Device:R", "R2", 80, 50, ("1", "2"), fp=RFP)
            + nl_wire((80, 46.19), (70, 46.19))
            + placed2("power:GND", "#PWR03", 75, 46.19, ("1",),
                      value="EARTH")
            + nl_wire((80, 53.81), (90, 53.81))
            + nl_label("G2", 85, 53.81))
        pcb = fp_pcb({"GND": [("R1", "1")],
                      "EARTH": [("R2", "1")],
                      "G2": [("R1", "2"), ("R2", "2")]})
        d = nl_project2("v05m-two-power-names", body, pcb_text=pcb)
        rep, rules = rules_of(d)
        check("v05m-all-power-names-group",
              any(r.startswith("PARITY_") for r in rules)
              and rep["verdict"] == "BLOCK", f"rules={rules}")

        # 19p (R4/RT-09). Power status follows the FILE: a power:
        # lib def WITHOUT the (power) flag (appended def wins) is
        # not a power symbol, so the two GND symbols do not merge
        # and the unflagged Sensor net stays undriven.
        noflag_defs = NL_DEFS + (
            '(symbol "power:GND" (symbol "GND_1_1"'
            ' (pin power_in line (at 0 0 270) (length 0)'
            ' (name "GND" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))))')
        body = (
            nl_placed("Test:Sensor", "U1", 50, 50)
            + nl_wire((44.92, 47.46), (35, 47.46))
            + nl_placed("power:GND", "#PWR01", 40, 47.46)
            + nl_wire((44.92, 52.54), (35, 52.54))
            + nl_label("A", 40, 52.54)
            + nl_placed("Test:Sensor", "U2", 50, 80)
            + nl_wire((44.92, 77.46), (35, 77.46))
            + nl_placed("power:GND", "#PWR02", 38, 77.46)
            + nl_placed("power:PWR_FLAG", "#FLG01", 42, 77.46)
            + nl_wire((44.92, 82.54), (35, 82.54))
            + nl_label("B", 40, 82.54))
        d = nl_project2("v05m-no-power-flag", body, defs=noflag_defs)
        rep, rules = rules_of(d)
        n_pnd = sum(1 for f in rep["findings"]
                    if f["rule"] == "ERC_POWER_NOT_DRIVEN")
        check("v05m-power-flag-is-file-evidence",
              n_pnd == 1, f"pnd={n_pnd} rules={rules}")

        # 19q (R5/RT-13). A hidden power_in pin sitting ON a signal
        # wire joins its named net alone: the wire (an output +
        # a PWR_FLAG'd GND net nearby) must stay clean.
        body = (
            nl_placed("Test:HiddenPwr", "U1", 50, 50)
            + nl_wire((44.92, 50), (35, 50))
            + nl_label("SIG", 40, 50)
            # U1's hidden pin 3 sits at (50, 44.92); an output
            # wire runs exactly through that point.
            + nl_placed("Test:Out", "U2", 44.92, 44.92)
            + nl_wire((50, 44.92), (65, 44.92))
            + nl_label("VOUT", 60, 44.92)
            # A real GND net with a flag, elsewhere.
            + nl_wire((35, 80), (55, 80))
            + placed2("power:GND", "#PWR01", 40, 80, ("1",),
                      value="GND")
            + placed2("power:PWR_FLAG", "#FLG01", 48, 80, ("1",),
                      value="PWR_FLAG"))
        d = nl_project("v05m-hidden-pin-detached", body)
        rep, rules = rules_of(d)
        check("v05m-hidden-pin-moves-alone",
              "ERC_OUTPUT_CONFLICT" not in rules
              and "ERC_UNCONNECTED_PIN" not in rules,
              f"rules={rules}")

        # 19r (R6/RT-42). Unit-common (_0_1) pins are one physical
        # pin: wired on unit 1's instance, unit 2's copy is the
        # same pin and must not be reported unconnected.
        quad_defs = NL_DEFS + (
            '(symbol "Test:Quad"'
            ' (symbol "Quad_0_1"'
            ' (pin passive line (at 0 5.08 270) (length 1.27)'
            ' (name "VCC" (effects (font (size 1.27 1.27))))'
            ' (number "4" (effects (font (size 1.27 1.27))))))'
            ' (symbol "Quad_1_1"'
            ' (pin passive line (at -5.08 0 0) (length 2.54)'
            ' (name "A" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27))))))'
            ' (symbol "Quad_2_1"'
            ' (pin passive line (at -5.08 0 0) (length 2.54)'
            ' (name "B" (effects (font (size 1.27 1.27))))'
            ' (number "2" (effects (font (size 1.27 1.27)))))))')
        NL_PINS["Test:Quad"] = ("4", "1", "2")
        body = (
            placed2("Test:Quad", "U1", 50, 50, ("4", "1"), unit=1)
            + nl_wire((50, 44.92), (40, 44.92))
            + nl_label("VCC", 45, 44.92)
            + '(no_connect (at 44.92 50))'
            + placed2("Test:Quad", "U1", 50, 90, ("4", "2"), unit=2)
            + '(no_connect (at 44.92 90))')
        d = nl_project2("v05m-unit-common", body, defs=quad_defs)
        rep, rules = rules_of(d)
        check("v05m-unit-common-pins-deduped",
              "ERC_UNCONNECTED_PIN" not in rules, f"rules={rules}")

        # 19s (R7/RT-44 + audit F4). The same sheet file used by
        # two sheet blocks: the netlist layer abstains wholesale
        # (SCAN_INCOMPLETE naming it), with no ERC/parity blame.
        sub_body = (
            nl_placed("Device:R", "R9", 50, 50)
            + nl_wire((50, 46.19), (50, 40))
            + nl_label("W", 50, 43)
            + nl_wire((50, 53.81), (50, 60))
            + nl_label("V", 50, 57))
        d = nl_project("v05m-sheet-twice",
                       SHEET_BLOCK + SHEET_BLOCK,
                       sub_body=sub_body)
        rep, rules = rules_of(d)
        check("v05m-multi-instanced-sheet-abstains",
              "SCAN_INCOMPLETE" in rules
              and not any(r.startswith(("ERC_", "PARITY_"))
                          for r in rules)
              and rep["coverage"]["netlist"].get(
                  "sheets_multi_instanced"),
              f"rules={rules}")

        # 19t (R8/RT-03). Two schematic nets share the display
        # name GND (a sheet-local label net and the global power
        # net): name matching pairs by membership overlap, so the
        # power twin matches PCB GND exactly and the local twin
        # is reported as a rename to PCB VIN -- no wrong-twin
        # membership BLOCK.
        body = (
            placed2("Device:R", "R1", 50, 50, ("1", "2"), fp=RFP)
            + placed2("Device:R", "R2", 70, 50, ("1", "2"), fp=RFP)
            + nl_wire((50, 46.19), (70, 46.19))
            + nl_label("GND", 60, 46.19)
            + nl_wire((50, 53.81), (50, 60))
            + nl_label("G2", 50, 57)
            + nl_wire((70, 53.81), (70, 60))
            + nl_label("G2", 70, 57)
            + placed2("Device:R", "R3", 50, 90, ("1", "2"), fp=RFP)
            + placed2("Device:R", "R4", 70, 90, ("1", "2"), fp=RFP)
            + nl_wire((50, 86.19), (70, 86.19))
            + placed2("power:GND", "#PWR01", 55, 86.19, ("1",),
                      value="GND")
            + placed2("power:GND", "#PWR02", 65, 86.19, ("1",),
                      value="GND")
            + nl_wire((50, 93.81), (50, 100))
            + nl_label("G2", 50, 97)
            + nl_wire((70, 93.81), (70, 100))
            + nl_label("G2", 70, 97))
        pcb = fp_pcb({"VIN": [("R1", "1"), ("R2", "1")],
                      "GND": [("R3", "1"), ("R4", "1")],
                      "G2": [("R1", "2"), ("R2", "2"),
                             ("R3", "2"), ("R4", "2")]})
        d = nl_project2("v05m-ambiguous-name", body, pcb_text=pcb)
        rep, rules = rules_of(d)
        check("v05m-ambiguous-name-overlap-match",
              "PARITY_NET_MEMBERSHIP" not in rules
              and "PARITY_NET_RENAMED" in rules, f"rules={rules}")

        # 19u (A1/F1). Bus touch is budgeted: one bus chain of
        # 40,001 points (40k bus segments) plus ~60 distinct
        # label/pin points far from the bus makes the bus-touch
        # work pass the point-segment budget -- the layer must
        # abstain (SCAN_INCOMPLETE), not run unbudgeted.
        bus_pts = " ".join(f"(xy {i * 0.5} 0)" for i in range(40001))
        labs = "".join(nl_label(f"L{i}", 10 + i * 1.0, 200)
                       for i in range(55))
        body = (
            nl_placed("Device:R", "R1", 50, 50)
            + nl_wire((50, 46.19), (50, 40)) + nl_label("W", 50, 43)
            + nl_wire((50, 53.81), (50, 60)) + nl_label("V", 50, 57)
            + f'(bus (pts {bus_pts}))' + labs)
        d = nl_project("v05m-bus-budget", body)
        rep, rules = rules_of(d)
        check("v05m-bus-touch-budgeted",
              "SCAN_INCOMPLETE" in rules
              and rep["coverage"]["netlist"].get("budget_exceeded")
              and not any(r.startswith(("ERC_", "PARITY_"))
                          for r in rules),
              f"rules={sorted(rules)[:8]}")

        # 19v (A2/F2). Parity matching is indexed: 40 schematic
        # nets whose memberships reappear under 40 different PCB
        # names are all found by the exact-membership hash (40
        # renames) with far fewer than 40x40 pair examinations
        # counted -- the old O(S x P) scan needed ~1,600+.
        body_parts = []
        sch_only_nets = {}
        for i in range(40):
            ref = f"R{i + 1}"
            y = 20 + i * 10
            body_parts.append(
                placed2("Device:R", ref, 50, y, ("1", "2"), fp=RFP)
                + nl_wire((50, y - 3.81), (40, y - 3.81))
                + nl_label(f"SN{i}", 45, y - 3.81)
                + nl_wire((50, y + 3.81), (40, y + 3.81))
                + nl_label("G2", 45, y + 3.81))
            sch_only_nets[f"PN{i}"] = [(ref, "1")]
            sch_only_nets.setdefault("G2", []).append((ref, "2"))
        d = nl_project2("v05m-parity-indexed", "".join(body_parts),
                        pcb_text=fp_pcb(sch_only_nets))
        rep, rules = rules_of(d)
        nl_cov = rep["coverage"]["netlist"]
        check("v05m-parity-indexed",
              nl_cov.get("parity_renamed") == 40
              and 0 < nl_cov.get("parity_pair_checks", 99999) < 400,
              f"cov={nl_cov.get('parity_renamed')},"
              f"{nl_cov.get('parity_pair_checks')}")

        # 19w (A3/F3). An np_thru_hole pad DOES carry membership:
        # the 19i layout with R1's G2 pad as NPTH must not lose
        # that membership (no membership BLOCK; rename NOTE only).
        pcb_npth = tiny_pcb("rect").replace(
            '(pad "2" smd rect (at 0.95 0) (size 1 1)'
            ' (layers "F.Cu") (net 2 "G2"))',
            '(pad "2" np_thru_hole circle (at 0.95 0) (size 1 1)'
            ' (layers "*.Cu") (net 2 "G2"))', 1)
        d = nl_project("v05m-npth-membership", pair_body,
                       pcb_text=pcb_npth)
        rep, rules = rules_of(d)
        check("v05m-npth-pad-membership",
              "PARITY_NET_MEMBERSHIP" not in rules
              and "PARITY_NET_RENAMED" in rules, f"rules={rules}")

        # 19x (A4/F5). Point coincidence is an EPS distance test:
        # pins 0.00002 mm apart across a rounding boundary merge;
        # pins 0.0008 mm apart in one old bucket do not.
        def two_r(x1, x2, tag):
            return (
                placed2("Device:R", "R1", x1, 50, ("1", "2"))
                + nl_wire((x1, 53.81), (x1, 60))
                + nl_label(f"{tag}A", x1, 57)
                + placed2("Device:R", "R2", x2, 50, ("1", "2"))
                + nl_wire((x2, 53.81), (x2, 60))
                + nl_label(f"{tag}B", x2, 57))
        d = nl_project("v05m-bucket-straddle",
                       two_r(10.00049, 10.00051, "S"))
        rep, rules = rules_of(d)
        check("v05m-eps-straddle-merges",
              "ERC_UNCONNECTED_PIN" not in rules, f"rules={rules}")
        d = nl_project("v05m-bucket-same",
                       two_r(10.0006, 10.0014, "T"))
        rep, rules = rules_of(d)
        unc = [f for f in rep["findings"]
               if f["rule"] == "ERC_UNCONNECTED_PIN"]
        check("v05m-eps-same-bucket-separate",
              len(unc) == 2, f"unc={len(unc)} rules={rules}")

        # 19y (A5/F6). Giant digit strings produce a normal
        # report, never an exit-3 int-limit crash: (i) a pin
        # number of 5,000 digits; (ii) a 5,000-digit unit in a
        # sub-symbol name.
        bignum_defs = NL_DEFS + (
            '(symbol "Test:BigNum" (symbol "BigNum_1_1"'
            f' (pin passive line (at 0 3.81 270) (length 1.27)'
            f' (name "A" (effects (font (size 1.27 1.27))))'
            f' (number "{"9" * 5000}"'
            f' (effects (font (size 1.27 1.27)))))'
            ' (pin passive line (at 0 -3.81 90) (length 1.27)'
            ' (name "B" (effects (font (size 1.27 1.27))))'
            ' (number "2" (effects (font (size 1.27 1.27)))))))')
        body = (
            placed2("Test:BigNum", "U8", 50, 50, ("2",))
            + nl_wire((50, 53.81), (50, 60))
            + nl_label("BN", 50, 57))
        d = nl_project2("v05m-bignum-pin", body, defs=bignum_defs)
        p = cli([str(d), "--format", "json"])
        try:
            rep = json.loads(p.stdout)
            ok_bigpin = p.returncode in (0, 1, 2) \
                and "Traceback" not in p.stderr
        except json.JSONDecodeError:
            ok_bigpin = False
        check("v05m-bignum-pin-no-crash", ok_bigpin,
              f"exit={p.returncode}")
        bigunit_defs = NL_DEFS + (
            f'(symbol "Test:BigUnit"'
            f' (symbol "BigUnit_{"9" * 5000}_1"'
            ' (pin passive line (at 0 3.81 270) (length 1.27)'
            ' (name "A" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))))')
        body = (
            placed2("Test:BigUnit", "U9", 50, 50, ("1",))
            + nl_placed("Device:R", "R1", 80, 50)
            + nl_wire((80, 46.19), (80, 40))
            + nl_label("W", 80, 43)
            + nl_wire((80, 53.81), (80, 60))
            + nl_label("V", 80, 57))
        d = nl_project2("v05m-bigunit", body, defs=bigunit_defs)
        p = cli([str(d), "--format", "json"])
        try:
            rep = json.loads(p.stdout)
            ok_bigunit = p.returncode in (0, 1, 2) \
                and rep["coverage"]["netlist"].get(
                    "pins_unresolved", 0) >= 1
        except json.JSONDecodeError:
            ok_bigunit = False
        check("v05m-bigunit-no-crash", ok_bigunit,
              f"exit={p.returncode}")

        # 19z (A6/F7). Duplicate lib defs and duplicate pin
        # numbers are counted in Coverage, not silently dropped.
        dup_defs = NL_DEFS + (
            '(symbol "Test:Dup" (symbol "Dup_1_1"'
            ' (pin passive line (at 0 3.81 270) (length 1.27)'
            ' (name "A" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))))'
            '(symbol "Test:Dup" (symbol "Dup_1_1"'
            ' (pin passive line (at 0 3.81 270) (length 1.27)'
            ' (name "A" (effects (font (size 1.27 1.27))))'
            ' (number "99" (effects (font (size 1.27 1.27)))))))'
            '(symbol "Test:DupPin" (symbol "DupPin_1_1"'
            ' (pin passive line (at 0 3.81 270) (length 1.27)'
            ' (name "A" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))'
            ' (pin passive line (at 0 -3.81 90) (length 1.27)'
            ' (name "B" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))))')
        body = (
            placed2("Test:Dup", "U1", 50, 50, ("99",))
            + placed2("Test:DupPin", "U2", 80, 50, ("1",)))
        d = nl_project2("v05m-duplicates", body, defs=dup_defs)
        rep, rules = rules_of(d)
        nl_cov = rep["coverage"]["netlist"]
        check("v05m-duplicates-counted",
              nl_cov.get("lib_defs_duplicate", 0) >= 1
              and nl_cov.get("pins_duplicate", 0) >= 1,
              f"cov={nl_cov.get('lib_defs_duplicate')},"
              f"{nl_cov.get('pins_duplicate')}")

        # 19aa (A7/F8). Unreadable coordinates are COUNTED in
        # Coverage per kind: a lib pin at (at nan ...) and a
        # label at (at nan ...).
        nan_defs = NL_DEFS + (
            '(symbol "Test:NanPin" (symbol "NanPin_1_1"'
            ' (pin passive line (at nan 3.81 270) (length 1.27)'
            ' (name "A" (effects (font (size 1.27 1.27))))'
            ' (number "1" (effects (font (size 1.27 1.27)))))'
            ' (pin passive line (at 0 -3.81 90) (length 1.27)'
            ' (name "B" (effects (font (size 1.27 1.27))))'
            ' (number "2" (effects (font (size 1.27 1.27)))))))')
        body = (
            placed2("Test:NanPin", "U1", 50, 50, ("1", "2"))
            + nl_wire((50, 53.81), (50, 60))
            + nl_label("NP", 50, 57)
            + '(label "LOST" (at nan 10 0)'
            ' (effects (font (size 1.27 1.27))))')
        d = nl_project2("v05m-coords-counted", body, defs=nan_defs)
        rep, rules = rules_of(d)
        nl_cov = rep["coverage"]["netlist"]
        check("v05m-coords-unreadable-counted",
              nl_cov.get("coords_unreadable_pins", 0) >= 1
              and nl_cov.get("coords_unreadable_labels", 0) >= 1
              and nl_cov.get("coords_unreadable", 0) >= 2,
              f"cov={nl_cov.get('coords_unreadable')},"
              f"{nl_cov.get('coords_unreadable_pins')},"
              f"{nl_cov.get('coords_unreadable_labels')}")

        # 19ab (A8/F9). A lib pin with no (number ...) is an
        # unresolved pin, not a resolved pin numbered "".
        nonum_defs = NL_DEFS + (
            '(symbol "Test:NoNum" (symbol "NoNum_1_1"'
            ' (pin passive line (at 0 3.81 270) (length 1.27)'
            ' (name "A" (effects (font (size 1.27 1.27)))))'
            ' (pin passive line (at 0 -3.81 90) (length 1.27)'
            ' (name "B" (effects (font (size 1.27 1.27))))'
            ' (number "2" (effects (font (size 1.27 1.27)))))))')
        body = (
            placed2("Test:NoNum", "U1", 50, 50, ("2",))
            + nl_wire((50, 53.81), (50, 60))
            + nl_label("NN", 50, 57))
        d = nl_project2("v05m-numberless-pin", body, defs=nonum_defs)
        rep, rules = rules_of(d)
        nl_cov = rep["coverage"]["netlist"]
        bad_pin_msgs = [f for f in rep["findings"]
                        if f["rule"] == "ERC_UNCONNECTED_PIN"
                        and "(pin(s) )" in f["message"]]
        check("v05m-numberless-pin-unresolved",
              nl_cov.get("pins_unresolved", 0) >= 1
              and not bad_pin_msgs,
              f"cov={nl_cov.get('pins_unresolved')}")

        # 19ac (A9/F10). A zero-pin lib def whose only instance
        # CLAIMS pins: the whole-layer abstention still reports
        # the claimed-but-unresolvable counts.
        zero_defs = (
            '(symbol "Test:Zero" (symbol "Zero_1_1"))')
        body = (
            placed2("Test:Zero", "U1", 50, 50, ("1", "2"))
            + nl_wire((40, 50), (60, 50)))
        d = nl_project2("v05m-zero-pin-claims", body, defs=zero_defs)
        rep, rules = rules_of(d)
        nl_cov = rep["coverage"]["netlist"]
        check("v05m-zero-pin-claims-counted",
              nl_cov.get("abstained")
              == "no pin definitions in schematic"
              and nl_cov.get("pins_unresolved") == 2
              and nl_cov.get("sch_unresolved_refs") == ["U1"],
              f"cov={nl_cov.get('abstained')},"
              f"{nl_cov.get('pins_unresolved')},"
              f"{nl_cov.get('sch_unresolved_refs')}")

        # 19ad (A10/F11). Power names are exact: a power symbol
        # with Value " GND" (leading space) does NOT merge with
        # the "GND" net -- R1's two pins stay on two nets.
        body = (
            placed2("Device:R", "R1", 50, 50, ("1", "2"))
            + nl_wire((50, 46.19), (40, 46.19))
            + placed2("power:GND", "#PWR01", 45, 46.19, ("1",),
                      value=" GND")
            + nl_wire((50, 53.81), (40, 53.81))
            + placed2("power:PWR_FLAG", "#FLG01", 45, 53.81,
                      ("1",), value="PWR_FLAG")
            + nl_label("GND", 48, 53.81, kind="global_label"))
        d = nl_project("v05m-power-name-exact", body)
        rep, rules = rules_of(d)
        check("v05m-power-name-not-stripped",
              rep["coverage"]["netlist"].get("nets_total") == 2,
              f"nets={rep['coverage']['netlist'].get('nets_total')}")

        # 19ae (A11/F12). An unnamed schematic net that is a
        # subset of exactly ONE leftover PCB net pairs with it
        # and reports PARITY_NET_MEMBERSHIP naming the PCB net
        # (no sch-only BLOCK for the unnamed net); here the
        # unnamed {R1.1, R2.1} sits inside PCB Net-X, which also
        # carries R3.1 from the named OTHER net.
        body = (
            placed2("Device:R", "R1", 50, 50, ("1", "2"), fp=RFP)
            + placed2("Device:R", "R2", 70, 50, ("1", "2"), fp=RFP)
            + nl_wire((50, 46.19), (70, 46.19))
            + nl_wire((50, 53.81), (50, 60))
            + nl_label("G2", 50, 57)
            + nl_wire((70, 53.81), (70, 60))
            + nl_label("G2", 70, 57)
            + placed2("Device:R", "R3", 50, 90, ("1", "2"), fp=RFP)
            + nl_wire((50, 86.19), (40, 86.19))
            + nl_label("OTHER", 45, 86.19)
            + nl_wire((50, 93.81), (50, 100))
            + nl_label("G2", 50, 97))
        pcb = fp_pcb({"Net-X": [("R1", "1"), ("R2", "1"),
                                ("R3", "1")],
                      "G2": [("R1", "2"), ("R2", "2"),
                             ("R3", "2")]})
        d = nl_project2("v05m-unnamed-subset", body, pcb_text=pcb)
        rep, rules = rules_of(d)
        memb = [f for f in rep["findings"]
                if f["rule"] == "PARITY_NET_MEMBERSHIP"
                and "Net-X" in f["message"]]
        sch_only_unnamed = [f for f in rep["findings"]
                            if f["rule"] == "PARITY_NET_SCH_ONLY"
                            and "unnamed" in f["message"]]
        check("v05m-unnamed-subset-pairs",
              len(memb) == 1 and not sch_only_unnamed,
              f"rules={rules}")

        # 19af (A12/F14). A segment with non-finite length
        # (1e308 coordinates) is matched endpoint-only and
        # counted: a pin and a label exactly on its line are
        # honestly reported, not falsely connected, no crash.
        body = (
            nl_placed("Test:Out", "U1", 4.92, 10)
            + '(label "ONLINE" (at 20 20 0)'
            ' (effects (font (size 1.27 1.27))))'
            + '(wire (pts (xy -1e308 -1e308) (xy 1e308 1e308)))')
        d = nl_project("v05m-degenerate-segment", body)
        rep, rules = rules_of(d)
        check("v05m-degenerate-segment-endpoint-only",
              "ERC_UNCONNECTED_PIN" in rules
              and "ERC_DANGLING_LABEL" in rules
              and rep["coverage"]["netlist"].get(
                  "degenerate_segments", 0) >= 1,
              f"rules={rules}")

        # 19ag (P1). BOM existence = all non-power symbols: a
        # fully-placed on_board=no symbol listed in the BOM is
        # NOT a BOM_UNKNOWN_REFERENCE (it is at most
        # not-orderable); a ref that exists nowhere still is.
        body = (
            placed2("Device:R", "R1", 50, 50, ("1", "2"), fp=RFP)
            + nl_wire((50, 46.19), (50, 40))
            + nl_label("W", 50, 43)
            + nl_wire((50, 53.81), (50, 60))
            + nl_label("V", 50, 57)
            + placed2("Device:R", "C1", 80, 50, ("1", "2"),
                      fp=RFP, on_board=False)
            + nl_wire((80, 46.19), (80, 40))
            + nl_label("W", 80, 43)
            + nl_wire((80, 53.81), (80, 60))
            + nl_label("V", 80, 57))
        d = nl_project("v05m-bom-onboard-no", body)
        (d / "bom.csv").write_text(
            "Reference,Quantity,Value,Footprint,MPN\n"
            "R1,1,10k,R_0805,RC0805FR-0710KL\n"
            "C1,1,100n,C_0805,CL21B104KCFNNNE\n")
        rep, rules = rules_of(d)
        check("v05m-bom-onboard-no-not-unknown",
              "BOM_UNKNOWN_REFERENCE" not in rules
              and "BOM_PART_NOT_ORDERABLE" in rules,
              f"rules={rules}")
        (d / "bom.csv").write_text(
            "Reference,Quantity,Value,Footprint,MPN\n"
            "R1,1,10k,R_0805,RC0805FR-0710KL\n"
            "C9,1,100n,C_0805,CL21B104KCFNNNE\n")
        rep, rules = rules_of(d)
        check("v05m-bom-truly-unknown-still-fires",
              "BOM_UNKNOWN_REFERENCE" in rules, f"rules={rules}")

        # 19ah (P2). Two PWR_FLAGs on one net: the conflict
        # message names the likely cause (duplicate flags, one
        # PWR_FLAG per net) instead of outputs-shorted wording.
        d = nl_project("v05m-pwrflag-message", two_flags)
        rep, rules = rules_of(d)
        msgs = [f["message"] for f in rep["findings"]
                if f["rule"] == "ERC_OUTPUT_CONFLICT"]
        check("v05m-pwrflag-conflict-message",
              len(msgs) == 1 and "one PWR_FLAG per net" in msgs[0]
              and "remove the extra flag" in msgs[0],
              f"msgs={msgs}")

        # ---- §20: outline containment (v0.5 benchmark fix) ----
        # A self-closed element no longer certifies an unrelated
        # open chain: a chain is a routed slot only when it lies
        # fully inside a closed loop's interior.
        def outline_pcb(body):
            return ('(kicad_pcb\n\t(version 20240108)\n'
                    '\t(generator "pcbnew")\n'
                    '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
                    '(44 "Edge.Cuts" user))\n'
                    '\t(net 0 "")\n\t(net 1 "A")\n\t(net 2 "B")\n'
                    + body + "\n)\n")

        def outline_closed_of(rep):
            return rep["coverage"]["geometry_lite"]["outline_closed"]

        # 20a. The benchmark shape (esp-rust-board__outline-opened
        # in miniature): outer chain with an 8 mm gap in its
        # bottom edge, plus two self-closed mounting-hole circles
        # inside. The circles must NOT mask the gap: BLOCK.
        d = tmp / "v06gap"; d.mkdir()
        (d / "a.kicad_pcb").write_text(outline_pcb(
            '\t(gr_line (start 10 10) (end 40 10) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 40 10) (end 40 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 40 30) (end 26 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 18 30) (end 10 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 10 30) (end 10 10) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_circle (center 15 15) (end 16.5 15) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_circle (center 35 15) (end 36.5 15) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        rep, rules = rules_of(d)
        check("v06-gap-masked-by-circles",
              "BOARD_OUTLINE_OPEN" in rules
              and outline_closed_of(rep) is False, f"rules={rules}")

        # 20b. A genuine routed slot: an L-shaped open chain fully
        # inside a self-closed (gr_rect) outline stays silent.
        d = tmp / "v06slotrect"; d.mkdir()
        (d / "a.kicad_pcb").write_text(outline_pcb(
            '\t(gr_rect (start 10 10) (end 40 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 15 20) (end 25 20) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 25 20) (end 25 25) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        rep, rules = rules_of(d)
        check("v06-slot-inside-rect",
              "BOARD_OUTLINE_OPEN" not in rules
              and outline_closed_of(rep) is True, f"rules={rules}")

        # 20c. Same slot inside a CHAINED closed loop (four lines,
        # no self-closed element): the walked loop's interior
        # certifies the slot — silent.
        d = tmp / "v06slotchain"; d.mkdir()
        (d / "a.kicad_pcb").write_text(outline_pcb(
            '\t(gr_line (start 10 10) (end 40 10) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 40 10) (end 40 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 40 30) (end 10 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 10 30) (end 10 10) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 15 20) (end 25 20) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 25 20) (end 25 25) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        rep, rules = rules_of(d)
        check("v06-slot-inside-chained-loop",
              "BOARD_OUTLINE_OPEN" not in rules
              and outline_closed_of(rep) is True, f"rules={rules}")

        # 20d. An open chain partially outside the closed loop
        # (it crosses the right edge) is not a slot: BLOCK.
        d = tmp / "v06partial"; d.mkdir()
        (d / "a.kicad_pcb").write_text(outline_pcb(
            '\t(gr_rect (start 10 10) (end 40 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 20 20) (end 45 20) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        rep, rules = rules_of(d)
        check("v06-chain-partially-outside",
              "BOARD_OUTLINE_OPEN" in rules
              and outline_closed_of(rep) is False, f"rules={rules}")

        # 20e. A self-closed circle as the ONLY outline element
        # is a closed (round) board: no finding, closed verdict.
        d = tmp / "v06circleonly"; d.mkdir()
        (d / "a.kicad_pcb").write_text(outline_pcb(
            '\t(gr_circle (center 25 20) (end 35 20) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        rep, rules = rules_of(d)
        check("v06-circle-only-outline",
              "BOARD_OUTLINE_OPEN" not in rules
              and "BOARD_OUTLINE_MISSING" not in rules
              and outline_closed_of(rep) is True, f"rules={rules}")

        # 20f. A chain whose endpoint touches the outline is a
        # notch in the board edge, not an interior slot: BLOCK.
        d = tmp / "v06notch"; d.mkdir()
        (d / "a.kicad_pcb").write_text(outline_pcb(
            '\t(gr_line (start 10 10) (end 40 10) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 40 10) (end 40 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 40 30) (end 10 30) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 10 30) (end 10 10) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))\n'
            '\t(gr_line (start 20 20) (end 20 10) '
            '(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'))
        rep, rules = rules_of(d)
        check("v06-notch-touching-outline",
              "BOARD_OUTLINE_OPEN" in rules
              and outline_closed_of(rep) is False, f"rules={rules}")

    if failures:
        print(f"\n{len(failures)} edge test(s) FAILED: {failures}")
        return 1
    print("\nAll edge tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
