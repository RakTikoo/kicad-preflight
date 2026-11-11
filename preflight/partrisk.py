"""Offline part-risk checks against a bundled SAMPLE distributor dataset.

The dataset (preflight/data/parts.json) is a small, hand-authored snapshot
shipped with the tool so the spike works with no network and no API
keys. Its values are illustrative — every finding cites the dataset name
and snapshot date, and the README says plainly that this is not live
distributor data. A live-data source is a later version's job.

Dataset handling is defensive: the file is schema-validated at load
(DatasetError -> usage error, exit 3, never a traceback), numbers that
arrive as strings/floats ("0", 0.0 — as real distributor exports
produce) are coerced explicitly, an MPN present with an unusable entry
is reported as such (not masqueraded as "not in dataset"), and MPN
lookup falls back to a case-insensitive match that is disclosed in the
finding's evidence.
"""

import json
from pathlib import Path

from .finding import BLOCK, CAUTION, NOTE, Finding

LOW_STOCK_THRESHOLD = 50      # fewer pieces than this counts as "low"
LONG_LEAD_WEEKS = 16          # longer lead time than this gets flagged

DEFAULT_DATASET = Path(__file__).parent / "data" / "parts.json"

_OBSOLETE_STATUSES = {"obsolete", "eol", "end of life", "discontinued",
                      "last time buy", "ltb"}
_NRND_STATUSES = {"nrnd", "not recommended for new designs",
                  "not recommended"}


class DatasetError(ValueError):
    """The part-risk dataset is missing, unreadable, or wrongly shaped."""


def _as_int(value):
    """Coerce a dataset number (int / integral float / digit string) to
    int; anything else (incl. bool, None, junk) -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return int(text)
        except ValueError:
            try:
                num = float(text)
            except ValueError:
                return None
            return int(num) if num.is_integer() else None
    return None


def load_dataset(path=None):
    """Return (meta, parts) where parts maps MPN -> info dict.

    Raises DatasetError with a user-readable message on any problem —
    callers turn that into a clean usage error (exit 3).
    """
    path = Path(path) if path else DEFAULT_DATASET
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(f"cannot read part-risk dataset "
                           f"{path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise DatasetError(f"part-risk dataset {path} is not valid "
                           f"JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DatasetError(f"part-risk dataset {path} must be a JSON "
                           "object with a 'parts' object")
    parts = data.get("parts", {})
    if not isinstance(parts, dict):
        raise DatasetError(f"part-risk dataset {path}: 'parts' must "
                           "be an object mapping MPN -> part info")
    for mpn, info in parts.items():
        if info is not None and not isinstance(info, dict):
            raise DatasetError(f"part-risk dataset {path}: entry for "
                               f"{mpn!r} must be an object or null")
    return data, parts


def _cite(meta):
    return (f"{meta.get('name', 'sample dataset')} "
            f"(snapshot {meta.get('snapshot_date', 'unknown')})")


def run_part_risk_checks(project, meta, parts):
    """Check each unique BOM MPN once, aggregating every row that uses
    it. Returns (findings, stats)."""
    findings = []
    bom_name = project.bom_path.name if project.bom_path else "BOM"
    by_mpn = {}
    for row in project.bom_rows:
        if row.mpn:
            by_mpn.setdefault(row.mpn, []).append(row)
    lower_map = {}
    for key in parts:
        lower_map.setdefault(key.lower(), key)
    stats = {"mpns_checked": len(by_mpn), "mpns_found": 0,
             "dataset_entries_invalid": 0}

    for mpn in sorted(by_mpn):
        rows = by_mpn[mpn]
        first = rows[0]
        where = f"{bom_name} line {first.line_no} · {mpn}"
        all_refs = [r for row in rows for r in row.refs]
        refs = ", ".join(all_refs) or "?"
        extra_lines = ", ".join(f"line {r.line_no}" for r in rows[1:])
        usage = f"{mpn} ({refs})" + (
            f" — also used on {extra_lines}" if extra_lines else "")

        info = parts.get(mpn)
        matched_case = ""
        canonical = mpn
        if info is None and mpn not in parts:
            canonical = lower_map.get(mpn.lower(), mpn)
            if canonical != mpn:
                info = parts[canonical]
                matched_case = (f"matched dataset entry '{canonical}' "
                                "case-insensitively — fix the BOM's "
                                "capitalisation")
        if canonical in parts and parts[canonical] is None:
            stats["dataset_entries_invalid"] += 1
            findings.append(Finding(
                "PART_DATASET_ENTRY_INVALID", NOTE,
                f"{mpn} ({refs}) is present in the {_cite(meta)} but "
                "its entry is empty/unusable, so its risk could not be "
                "checked. The dataset needs fixing, not the board.",
                where=where))
            continue
        if info is None:
            findings.append(Finding(
                "PART_NOT_IN_SAMPLE_DATA", NOTE,
                f"{mpn} ({refs}) is not in the {_cite(meta)}, so its "
                "lifecycle/stock risk could not be checked. Absence "
                "from the sample data says nothing about the part "
                "itself.",
                where=where))
            continue
        stats["mpns_found"] += 1
        status = str(info.get("status", "")).strip().lower()
        stock = _as_int(info.get("stock"))
        sources = _as_int(info.get("sources"))
        lead = _as_int(info.get("lead_time_weeks"))
        obsolete = status in _OBSOLETE_STATUSES
        ev = matched_case

        if obsolete:
            findings.append(Finding(
                "PART_OBSOLETE", BLOCK,
                f"{usage} is marked '{info.get('status')}' in the "
                f"{_cite(meta)}. An obsolete part can vanish from stock "
                "before your next order — pick a current replacement "
                "now, while changing it is cheap.",
                where=where,
                evidence=f"status={info.get('status')}"
                         + (f"; {ev}" if ev else "")))
        elif status in _NRND_STATUSES:
            findings.append(Finding(
                "PART_NOT_RECOMMENDED", CAUTION,
                f"{usage} is 'not recommended for new designs' in the "
                f"{_cite(meta)}. Fine for a one-off build, a poor "
                "choice if you plan to order this board again.",
                where=where,
                evidence=f"status={info.get('status')}"
                         + (f"; {ev}" if ev else "")))
        if not obsolete:
            if stock == 0:
                findings.append(Finding(
                    "PART_OUT_OF_STOCK", BLOCK,
                    f"{usage} shows 0 in stock in the {_cite(meta)}. "
                    "You cannot order this part today.",
                    where=where, evidence="stock=0"))
            elif stock is not None and 0 < stock < LOW_STOCK_THRESHOLD:
                findings.append(Finding(
                    "PART_LOW_STOCK", CAUTION,
                    f"{usage} shows only {stock} in stock in the "
                    f"{_cite(meta)} — fewer than the "
                    f"{LOW_STOCK_THRESHOLD}-piece comfort line. A small "
                    "run or a few lost boards can wipe that out; "
                    "consider a stocked alternate.",
                    where=where, evidence=f"stock={stock}"))
            if lead is not None and lead > LONG_LEAD_WEEKS:
                findings.append(Finding(
                    "PART_LONG_LEAD_TIME", CAUTION,
                    f"{usage} has a {lead}-week lead time in the "
                    f"{_cite(meta)}. Order early or pick a faster part "
                    "if this board is on a schedule.",
                    where=where, evidence=f"lead_time_weeks={lead}"))
            if sources == 1:
                findings.append(Finding(
                    "PART_SINGLE_SOURCE", NOTE,
                    f"{usage} has a single source in the {_cite(meta)}. "
                    "If that source dries up, this board waits — worth "
                    "knowing, not necessarily worth fixing.",
                    where=where, evidence="sources=1"))
    return findings, stats
