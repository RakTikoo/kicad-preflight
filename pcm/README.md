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

## Test plan (needs a real KiCad — none on the build machine)
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
