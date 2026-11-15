"""KiCad Preflight — pre-fab checks for KiCad projects.

One shared checks engine, two front doors: this CLI and the KiCad
in-app plugin (pcm/). Pure Python 3 standard library, deterministic
checks, plain-English findings.

Library use: `from preflight import run` — run(target) checks a
project folder (or a single project file) and returns a Report;
`run_bom_only` checks a lone BOM CSV, `run_gerber_package` checks a
folder of exported Gerbers + drill files.
"""

__version__ = "0.7.0"

from .engine import Report, run, run_bom_only, run_gerber_package

__all__ = [
    "Report",
    "__version__",
    "run",
    "run_bom_only",
    "run_gerber_package",
]
