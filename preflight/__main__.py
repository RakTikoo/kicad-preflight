"""KiCad Preflight CLI.

Usage:
    python3 -m preflight <project-folder-or-file> [--format text|md|json]
                         [--bom PATH] [--dataset PATH] [--output PATH]
    python3 -m preflight <bom.csv>   — standalone BOM mode (any EDA tool)

Exit codes: 0 = PASS, 1 = CAUTION, 2 = BLOCK, 3 = usage error.
The CLI never prints a Python traceback: bad targets, bad datasets,
unwritable outputs and internal errors all exit 3 with a message.
"""

import argparse
import os
import sys
from pathlib import Path

from . import engine
from .finding import EXIT_CODES, EXIT_USAGE_ERROR
from .partrisk import DatasetError
from .report import RENDERERS
from .sexpr import SexprError

_PROJECT_SUFFIXES = {".kicad_pro", ".kicad_sch", ".kicad_pcb"}
_PROTECTED_SUFFIXES = _PROJECT_SUFFIXES | {".csv"}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2 by default; Preflight reserves 2 for BLOCK,
        # so usage errors exit 3 instead.
        self.print_usage(sys.stderr)
        print(f"preflight: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE_ERROR)


def build_parser():
    parser = _Parser(
        prog="preflight",
        description="Pre-fab checks for a KiCad project: schematic/PCB "
                    "agreement, BOM sanity, and offline part risk — one "
                    "plain-English report before you order. A lone BOM "
                    "CSV (from any EDA tool) runs the BOM checks only.")
    parser.add_argument("target",
                        help="KiCad project folder, a .kicad_pro / "
                             ".kicad_sch / .kicad_pcb file inside it, "
                             "or a BOM .csv for standalone BOM mode")
    parser.add_argument("--format", choices=sorted(RENDERERS),
                        default="text", help="report format (default: text)")
    parser.add_argument("--bom", default=None,
                        help="BOM CSV to use (default: auto-detect in "
                             "the project folder)")
    parser.add_argument("--dataset", default=None,
                        help="part-risk dataset JSON (default: the "
                             "bundled sample dataset)")
    parser.add_argument("--output", default=None,
                        help="write the report to this file instead of "
                             "stdout")
    return parser


def _fail(message):
    print(f"preflight: error: {message}", file=sys.stderr)
    return EXIT_USAGE_ERROR


def main(argv=None):
    args = build_parser().parse_args(argv)
    target = Path(args.target)
    if not target.exists():
        return _fail(f"no such file or folder: {target}")
    if args.bom and not Path(args.bom).is_file():
        return _fail(f"BOM file not found: {args.bom}")
    if args.dataset and not Path(args.dataset).is_file():
        return _fail(f"dataset file not found: {args.dataset}")

    standalone_bom = None
    if target.is_file():
        suffix = target.suffix.lower()
        if suffix == ".csv":
            standalone_bom = target
        elif suffix not in _PROJECT_SUFFIXES:
            return _fail(
                f"file target must be a .kicad_pro, .kicad_sch, "
                f".kicad_pcb or .csv file, not {target.name!r}")
    else:
        has_project = any(next(target.glob(pat), None) is not None
                          for pat in ("*.kicad_sch", "*.kicad_pcb",
                                      "*.kicad_pro"))
        if not has_project:
            csvs = sorted(target.glob("*.csv")) + sorted(target.glob("*.CSV"))
            if len(csvs) == 1:
                standalone_bom = csvs[0]
            else:
                return _fail(
                    f"no KiCad project files (.kicad_sch/.kicad_pcb/"
                    f".kicad_pro) found in {target}")

    try:
        if standalone_bom is not None:
            report = engine.run_bom_only(standalone_bom,
                                         dataset_path=args.dataset)
            input_paths = {standalone_bom.resolve()}
        else:
            report = engine.run(target, bom_override=args.bom,
                                dataset_path=args.dataset)
            folder = target if target.is_dir() else target.parent
            input_paths = {p.resolve() for p in folder.iterdir()
                           if p.suffix.lower() in _PROTECTED_SUFFIXES}
    except DatasetError as exc:
        return _fail(str(exc))
    except (ValueError, SexprError) as exc:
        return _fail(str(exc))
    except Exception as exc:  # no tracebacks, ever (audit hardening)
        return _fail(f"internal error ({type(exc).__name__}): {exc}")

    rendered = RENDERERS[args.format](report)
    if args.output:
        out_path = Path(args.output)
        if out_path.resolve() in input_paths or (
                out_path.exists()
                and out_path.suffix.lower() in _PROTECTED_SUFFIXES):
            return _fail(
                f"refusing to overwrite a project/BOM file: {out_path} "
                "(--output must name a new report file)")
        try:
            out_path.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            return _fail(f"cannot write report to {out_path}: {exc}")
        print(f"Report written to {args.output} "
              f"(verdict: {report.verdict})")
    else:
        try:
            sys.stdout.write(rendered)
            sys.stdout.flush()
        except BrokenPipeError:
            # `| head` and friends: exit quietly, no traceback.
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
            except OSError:
                pass
    return EXIT_CODES[report.verdict]


if __name__ == "__main__":
    sys.exit(main())
