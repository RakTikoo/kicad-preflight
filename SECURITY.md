# Security policy

KiCad Preflight's whole job is reading **untrusted** project folders —
boards downloaded from the internet, shared in forums, sent by
collaborators — so vulnerabilities in Preflight itself matter: a
crafted project file that can crash, hang, exhaust, or deceive the
checker defeats the tool's purpose.

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue:

- Use GitHub's private vulnerability reporting for this repository:
  **Security → Report a vulnerability** on
  https://github.com/RakTikoo/kicad-preflight
  (GitHub Security Advisories). This keeps the report visible only to
  the reporter and the maintainer until a fix is available.

Please include, as far as you can:

- the Preflight version (`preflight/__init__.py`) and your Python version,
- a minimal project folder (or file) that triggers the issue,
- what you expected to happen and what actually happened (hang, crash,
  resource exhaustion, verdict bypass, forged report output, …).

Do **not** include real credentials or anyone else's private data in a
report. The project's own corpus uses public open-source boards and
synthetic fixtures only — that is enough.

## What counts as a defect

- A project file that makes Preflight hang, crash with a traceback, or
  consume unbounded memory/CPU (no scan budget may be bypassable).
- A broken board that receives PASS because a file was silently
  skipped (`SCAN_INCOMPLETE` should have fired instead).
- Crafted part values / references / filenames that forge or corrupt
  the report (terminal escapes, broken Markdown/JSON output).
- A fake `kicad-cli` or dataset file being trusted beyond its documented
  role.

## Supported versions

Only the latest release is supported. Preflight is pre-1.0; security
fixes land on the newest tag and are called out in the release notes.
