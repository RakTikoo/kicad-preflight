# PCM package — KiCad Preflight

Skeleton for distributing the in-KiCad plugin through KiCad's Plugin
and Content Manager, following the official KiCad addons guide
(https://dev-docs.KiCad.org/en/addons/index.html, accessed 2026-11-09):

- `package/metadata.json` — the in-archive metadata (schema v2,
  identifier `com.github.RakTikoo.kicad-preflight`, type `plugin`,
  license MIT, no `download_*` keys — those belong only in the
  submission copy).
- `package/plugins/__init__.py` — the Action Plugin (registers a
  `pcbnew.ActionPlugin` subclass; `Run()` calls the shared engine on
  the open board's project folder and shows the text report).
- `package/resources/icon.png` — 64×64 PCM icon (placeholder art).
- `build_pcm.py` — assembles the zip (engine copied into
  `plugins/preflight/`), computes SHA-256/sizes, and emits the
  submission metadata + a self-hosted repository pair
  (`repository.json` / `packages.json`) under `dist/`.
  It uploads and submits nothing.

## Status (2026-11-10 probe) and remaining test plan

Proven inside a real KiCad 8.0.9 PCB Editor (VM, virtual display): the
built PCM zip was installed PCM-style into a KiCad profile, a driver
plugin running inside the editor invoked this plugin's `Run()` with no
patching, and the report dialog appeared over a real open board with
the expected findings; `register()` executes cleanly in-app. So test
plan steps 1–2 below are done in substance (step 2 was driven
programmatically rather than by mouse — the dialog, engine and report
are the real ones).

Remaining — step 3, the desktop check: on a stock KiCad install, add
the built zip via PCM → Manage, install, and confirm the entry appears
under Tools → External Plugins and runs. In the probe environment,
KiCad's startup scan did not auto-discover the identifier-named
package folder (a plain-named sibling package in the same folder did
load; the probe install was a hand extraction of KiCad, which
misresolves some of its own resource paths the same way). Whether that
is a scan-by-directory-name behaviour or an extraction artefact is
exactly what the stock-install check settles.

Original test plan, for reference:
1. Copy `package/plugins/` (plus the built engine folder) into a KiCad
   plugin search path (`pcbnew.PLUGIN_DIRECTORIES_SEARCH`).
2. Open the broken sample board in the PCB Editor; run
   Tools → External Plugins → KiCad Preflight; the dialog must show
   the same findings as `python3 -m preflight samples/broken`.
3. Build the zip with `build_pcm.py`, add `dist/` as a third-party
   repository in PCM → Manage, install, repeat step 2.
4. Validate `metadata-submission.json` against the official schema
   (https://go.kicad.org/pcm/schemas/v2) before any submission.

Submission to the official repository (merge request to
gitlab.com/kicad/addons/metadata) is **PAUSED under the H-1B gate**
(`~/workspace/agentic-spikes/H1B_GATE.md`) — it is a distribution
submission, not mere code work, and happens only after the gate
clears AND Rakshit approves. Nothing in this folder is submitted by
building it; the built `dist/` outputs were removed when the gate
was applied.
