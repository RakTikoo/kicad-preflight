#!/usr/bin/env python3
"""Build the broken corpus: labelled mutations of the open-source boards.

Each mutation copies a clean project folder from corpus/projects/ into
corpus/broken/<name>/, applies ONE textual fault, and writes a
MUTATION.md naming the fault and the Preflight rule that must catch it.
Run:  python3 corpus/build_broken.py
Re-running rebuilds from the pristine copies.
"""

import csv
import io
import re
import shutil
from pathlib import Path

HERE = Path(__file__).parent
PROJECTS = HERE / "projects"
BROKEN = HERE / "broken"

ESP = PROJECTS / "esp-rust-board" / "hardware" / "esp-rust-board"
UPSY = PROJECTS / "upsy-desky" / "pcb"


def copy_tree(src, dst):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns("*.pdf", "gerber",
                                                  "placement", "bom",
                                                  "schematic"))
    return dst


def write_mutation(folder, title, fault, expected_rule):
    (folder / "MUTATION.md").write_text(
        f"# Mutation: {title}\n\nFault planted: {fault}\n\n"
        f"Preflight MUST catch this with rule: {expected_rule}\n",
        encoding="utf-8")


def remove_balanced_block(text, start_idx):
    """Remove the balanced (...) block starting at start_idx."""
    depth = 0
    i = start_idx
    in_str = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[:start_idx] + text[i + 1:]
        i += 1
    raise ValueError("unbalanced block")


def edit_bom(folder, fn):
    bom = folder / "esp-rust-board.csv"
    rows = list(csv.reader(io.StringIO(bom.read_text(encoding="utf-8"))))
    rows = fn(rows)
    out = io.StringIO()
    csv.writer(out, quoting=csv.QUOTE_ALL).writerows(rows)
    bom.write_text(out.getvalue(), encoding="utf-8")


def main():
    BROKEN.mkdir(exist_ok=True)

    # 1 — BOM row deleted (esp): the C6 row vanishes from the BOM.
    d = copy_tree(ESP, BROKEN / "esp-bom-row-deleted")
    def drop_c6(rows):
        return [r for r in rows if not r[0].startswith('"C6') and "C6," not in r[0].replace(" ", "")]
    edit_bom(d, lambda rows: [r for r in rows
                              if r[0].replace(" ", "") != "C6,"])
    write_mutation(d, "BOM row deleted",
                   "The BOM row for C6 was deleted; C6 is still in the schematic.",
                   "SCHEMATIC_PART_MISSING_FROM_BOM")

    # 2 — BOM quantity changed (esp): first data row qty 3 -> 7.
    d = copy_tree(ESP, BROKEN / "esp-bom-qty-changed")
    def qty(rows):
        rows[1][1] = "7"
        return rows
    edit_bom(d, qty)
    write_mutation(d, "BOM quantity changed",
                   "First BOM row quantity changed from 3 to 7 while it lists 3 references.",
                   "BOM_QUANTITY_MISMATCH")

    # 3 — BOM gains an unknown reference (esp): extra row for C999.
    d = copy_tree(ESP, BROKEN / "esp-bom-unknown-ref")
    def add(rows):
        rows.append(["C999, ", "1", "10nF 10V", "C15195", "GRM123",
                     "Capacitor_SMD:C_0402_1005Metric", "planted"])
        return rows
    edit_bom(d, add)
    write_mutation(d, "BOM unknown reference",
                   "A BOM row for C999 was added; no C999 exists in the schematic.",
                   "BOM_UNKNOWN_REFERENCE")

    # 4 — Schematic footprint blanked (upsy): R1 loses its Footprint property value.
    d = copy_tree(UPSY, BROKEN / "upsy-sch-footprint-blanked")
    sch = d / "upsy-desky.kicad_sch"
    text = sch.read_text(encoding="utf-8")
    m = re.search(r'\(property "Reference" "R1"', text)
    assert m, "R1 not found"
    fp = re.search(r'\(property "Footprint" "[^"]*"',
                   text[m.start():m.start() + 4000])
    assert fp, "R1 footprint property not found"
    pos = m.start() + fp.start()
    text = (text[:pos] + '(property "Footprint" ""'
            + text[pos + len(fp.group(0)):])
    sch.write_text(text, encoding="utf-8")
    write_mutation(d, "Schematic footprint blanked",
                   "R1's Footprint property in the schematic was emptied.",
                   "SCHEMATIC_FOOTPRINT_MISSING")

    # 5 — PCB footprint swapped (upsy): first footprint's library name changed.
    d = copy_tree(UPSY, BROKEN / "upsy-pcb-footprint-swapped")
    pcb = d / "upsy-desky.kicad_pcb"
    text = pcb.read_text(encoding="utf-8")
    m = re.search(r'\(footprint "([^"]+)"', text)
    assert m, "no footprint found"
    text = text[:m.start()] + f'(footprint "{m.group(1)}_WRONG"' + text[m.end():]
    pcb.write_text(text, encoding="utf-8")
    write_mutation(d, "PCB footprint swapped",
                   f"PCB footprint '{m.group(1)}' renamed to '{m.group(1)}_WRONG'.",
                   "FOOTPRINT_MISMATCH")

    # 6 — Footprint deleted from PCB (upsy): first footprint block removed.
    d = copy_tree(UPSY, BROKEN / "upsy-pcb-footprint-deleted")
    pcb = d / "upsy-desky.kicad_pcb"
    text = pcb.read_text(encoding="utf-8")
    start = text.index("(footprint ")
    text = remove_balanced_block(text, start)
    pcb.write_text(text, encoding="utf-8")
    write_mutation(d, "Footprint deleted from PCB",
                   "The first footprint block was removed from the PCB file; "
                   "its reference remains in the schematic.",
                   "SCH_PART_NOT_ON_PCB")

    print("Broken corpus built:", sorted(p.name for p in BROKEN.iterdir()))


if __name__ == "__main__":
    main()
