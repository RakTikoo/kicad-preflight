"""Shared finding type and severity/verdict vocabulary for Preflight.

Severity model (plain English, in the report):
  BLOCK   — the board or the order will be wrong if you proceed.
  CAUTION — risky, incomplete, or could not be fully checked; look
            before ordering.
  NOTE    — a heads-up only; never changes the verdict on its own.

Verdicts (mirroring the SkillScan convention):
  PASS    — no BLOCK, no CAUTION findings.
  CAUTION — at least one CAUTION, no BLOCK.
  BLOCK   — at least one BLOCK finding.

Exit codes: 0 = PASS, 1 = CAUTION, 2 = BLOCK, 3 = usage / IO error.
A PASS is only as good as the report's Coverage section: whenever a
file or layer could not be checked, a SCAN_INCOMPLETE finding (or a
more specific BLOCK/CAUTION) says so — a clean verdict never hides a
skipped check.
"""

from dataclasses import dataclass

BLOCK = "BLOCK"
CAUTION = "CAUTION"
NOTE = "NOTE"

SEVERITY_ORDER = [BLOCK, CAUTION, NOTE]

PASS = "PASS"
VERDICT_CAUTION = "CAUTION"
VERDICT_BLOCK = "BLOCK"

EXIT_CODES = {PASS: 0, VERDICT_CAUTION: 1, VERDICT_BLOCK: 2}
EXIT_USAGE_ERROR = 3


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    where: str = ""       # e.g. "led-board.kicad_sch · D1"
    evidence: str = ""    # the exact value/line that triggered it

    def to_dict(self):
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "where": self.where,
            "evidence": self.evidence,
        }


def verdict_for(findings):
    severities = {f.severity for f in findings}
    if BLOCK in severities:
        return VERDICT_BLOCK
    if CAUTION in severities:
        return VERDICT_CAUTION
    return PASS
