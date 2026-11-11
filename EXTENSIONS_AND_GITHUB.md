# KiCad Preflight — GitHub & Extensions Strategy

Written 2026-11-09. Research sources are cited inline with access
dates; anything not verified live is marked UNVERIFIED rather than
repeated. (Lesson from SkillScan: its "no dominant scanner" market
claim shipped unverified. That does not happen here — see §5.)

> **H-1B GATE — READ FIRST (decided by Rakshit, 2026-11-09;
> ~/workspace/agentic-spikes/H1B_GATE.md, which outranks this plan).**
> The PCM official submission and ALL launch/advertising steps
> described below are **PAUSED** until the gate clears — Rakshit back
> in India (planned Feb–Mar 2027), or an immigration attorney's clear
> opinion. This document is a plan on file, not a set of ready-to-go
> actions: no forum posts, no Show HN, no PCM merge request, no
> topics/SEO campaign, no promotion of any kind while the gate is on.
> Growth in the meantime is **organic only** (the public repo as-is,
> search, word of mouth). There are no monetisation hooks anywhere in
> this project — no sponsors, donations, or paid tiers — and none may
> be added while the gate is on. The PCM plugin skeleton as *code*
> (§3.1) is product work and continues; only its submission/listing
> is gated. Dates from the old launch plan (Jan 4–15, 2027) are
> non-events under the gate.

**Status gate (updated 2026-11-11):** the GitHub repo is live and
pushed (public, v0.6.0, CI green). What remains gated is submission
and promotion only: no PCM official submission or listing, no launch
activity, and no money of any kind, until Rakshit clears the gate.

---

## 1. Scope: KiCad-first, not KiCad-only

Rakshit's clarification, 2026-11-09: Preflight is not "for KiCad" the
way a fab site is "for ordering" — KiCad is design software, and the
wedge is KiCad-first by design, not KiCad-only forever.

- **v0 (this build):** the KiCad-native parser reads `.kicad_sch` /
  `.kicad_pcb`. In parallel, the **BOM checks are tool-agnostic**: they
  operate on BOM CSVs, and a BOM exported from Altium, Eagle/Fusion, or
  EasyEDA has the same classes of problems (missing/placeholder MPN,
  quantity vs reference-count mismatches, DNP errors, unknown
  references against a parts list). The code is structured so the BOM
  module (`preflight/bomchecks.py`, fed by `preflight/bom.py`) imports
  nothing from the KiCad parser, and the CLI accepts a standalone BOM
  CSV target — `python3 -m preflight production-bom.csv` — running the
  BOM checks with an honest coverage line ("no schematic supplied;
  cross-checks not run").
- **Next:** the KiCad plugin (§3) — the in-app front door for the
  KiCad wedge.
- **Later:** the fab-site browser extension (§4) — the tool-agnostic
  front door. At a JLCPCB/PCBWay checkout, the uploaded BOM/Gerbers are
  checked regardless of which design tool produced them.
- **Possible later parsers:** EasyEDA JSON exports and Eagle `.brd`/
  `.sch` XML are both text formats; either could feed the same checks
  engine the way the KiCad parser does now. Not scoped, not promised —
  recorded so the engine's parser interface stays clean.

What this means for the code today: parsers produce a neutral project
model; checks consume the model or a BOM alone; no check reaches into
KiCad file specifics. That separation is a design rule for
contributions (see CONTRIBUTING.md).

## 2. GitHub plan — RakTikoo/kicad-preflight

- **Name availability (verified 2026-11-09):**
  `GET https://api.github.com/repos/RakTikoo/kicad-preflight` returned
  **404** while the same call for the existing `RakTikoo/skillscan`
  returned 200 — the name appears free on Rakshit's account. (Caveat:
  a 404 could in principle hide a private repo owned by someone else;
  on Rakshit's own account he can see that directly at creation time.)
- **Repo description (search words first, per ADVERTISE_BOTH §2.3):**
  "Pre-fab checker for KiCad projects — catches footprint mismatches,
  DRC/ERC problems and BOM part risks before you order your PCB."
- **Topics (16 of ≤20; basic repo metadata only — applying topics is hygiene, not a campaign, and any promotional use is paused under the gate):** `kicad`, `kicad-plugin`, `kicad-pcm`, `pcb`,
  `pcb-design`, `eda`, `electronics`, `bom`, `design-rule-check`,
  `dfm`, `python`, `open-hardware`, `jlcpcb`, `pcbway`, `preflight`,
  `kicad-action-plugin`.
- **README — the 30-second demo structure:**
  1. One sentence: what it checks and when (right before you order).
  2. Demo block above the fold: run on the deliberately broken sample
     → the actual report output (PASS/CAUTION/BLOCK verdicts) → fix →
     clean re-run. (GIF of the in-KiCad flow replaces this when the
     plugin is real; the CLI transcript is the v0 demo.)
  3. Install paths: CLI today; PCM repository URL when the launch
     stack opens (Jan 2027).
  4. "What it checks" rule table; "Known limits" section (sample
     part-risk data honestly labelled, kicad-cli needed for official
     DRC/ERC, KiCad ≥6 file formats).
  5. Corpus & honesty links: `corpus/SOURCES.md`, the adversarial
     review summary.
- **CI:** `.github/workflows/tests.yml` (in tree) mirrors SkillScan's:
  checkout@v4 + setup-python@v5 (3.12), zero runtime dependencies,
  three suites — `run_tests.py`, `run_corpus_tests.py`,
  `run_edge_tests.py`. Dependabot watches github-actions only.
- **Releases/versioning:** SemVer, first tag `v0.1.0` at repo-public
  day with user-language notes ("catches X, misses Y"). Once the PCM
  package ships, GitHub releases and PCM versions stay in lockstep —
  PCM published versions are effectively immutable, so every plugin
  bump is a new version + new metadata MR, never a re-upload.
- **Community files (in tree):** LICENSE (MIT), CONTRIBUTING.md,
  SECURITY.md, issue templates for the two highest-value reports
  (**false positive** and **missed fault**), `good-first-issues.md`
  with 8 seeded drafts (post as issues only after the push).
- **Social preview:** dark card, board screenshot + a red
  "footprint mismatch" report line + "MIT · KiCad plugin + CLI"
  (asset created at repo-public time, Dec 29–30 per launch plan).

## 3. KiCad extension — Action Plugin + PCM

Researched from official KiCad documentation, accessed 2026-11-09:
- PCB Python Bindings (Action Plugins):
  https://dev-docs.KiCad.org/en/apis-and-binding/pcbnew/index.html
- KiCad Addons / PCM guide:
  https://dev-docs.KiCad.org/en/addons/index.html

### 3.1 Plugin skeleton (built: `pcm/` in this tree)
- Action Plugins are discovered in KiCad's plugin search paths and
  appear under **Tools → External Plugins**, optionally as a toolbar
  button. The plugin is a Python package: a class derived from
  `pcbnew.ActionPlugin`, `defaults()` setting name / category /
  description / `show_toolbar_button` / `icon_file_name` (24×24 PNG),
  a `Run()` entry point, and `MyPlugin().register()` executed at
  import (`__init__.py` for a package layout — the "complex plugin"
  pattern in the official docs, chosen because the engine is a
  multi-file package and the same layout supports `python -m` runs).
- Preflight's `Run()` takes the open board's file path
  (`pcbnew.GetBoard().GetFileName()`), hands its folder to the shared
  engine, and shows the text report in a dialog. The engine never
  imports `pcbnew`, so CLI and plugin can never drift apart.
- Version/API caution: KiCad 9 added the IPC API and the SWIG
  `pcbnew` bindings are the legacy path (PCM metadata carries a
  per-version `runtime` field, `"ipc"` or `"swig"`, since KiCad
  9.0.1). The skeleton uses the SWIG Action Plugin API documented
  above; whether to target IPC for KiCad 10 is an open decision to
  make when the plugin is first run in a real KiCad — flagged here,
  not silently assumed. UNVERIFIED: actual plugin behaviour inside
  KiCad — there is no KiCad on the build machine; test plan is in
  `pcm/README.md`.

### 3.2 PCM packaging (per the official addons guide)
- A package is a ZIP (ISO 21320-1) with a strict structure for Python
  plugins: `metadata.json` at the root, `plugins/` with the plugin
  placed directly inside, optional `resources/icon.png` (64×64).
  Extra files are not permitted in the archive.
- `metadata.json` follows the PCM JSON schema — v1 (KiCad 6.0+) at
  https://go.kicad.org/pcm/schemas/v1, v2 (KiCad 10.0+) at
  https://go.kicad.org/pcm/schemas/v2; new packages should target v2,
  and `plugin`-type packages are automatically served to older KiCad
  versions too. Required fields include `name`, `description`
  (≤150 chars), `description_full`, `identifier`, `type` (`plugin`),
  `author`, `license`, and `versions[]`, each with `version`,
  `status` (stable/testing/development/deprecated), `kicad_version`
  (minimum), optional `kicad_version_max`, and — only in the copy
  submitted to the repository, never in the in-archive copy —
  `download_url`, `download_sha256`, `download_size`, `install_size`.
- **Identifier:** must be reverse-DNS namespaced; for a GitHub-hosted
  project the guide's pattern is `com.github.username.packagename`,
  giving **`com.github.RakTikoo.kicad-preflight`** (matches the launch
  plan). (The field description on the same page says identifiers may
  contain only alphanumerics and dashes — the page contradicts its own
  namespacing section; the reverse-DNS instruction plus the schema is
  what submissions actually use. Noted so the discrepancy is a known
  thing, not a surprise at submission time.)
- **Licensing:** packages in the official repository must be
  open-source; packages containing code must use a GPL-compatible
  licence. Preflight's MIT licence satisfies this.
- **Commercial-services clause:** packages that link to or provide
  commercial services (fab, component lookup, order management) must
  contact the KiCad team first. Preflight's plugin performs offline
  checks and does not connect to any commercial service, so on the
  guide's own FAQ this does not apply; if a future version ever calls
  a distributor/fab API, that changes and the team must be emailed
  before submission. The fab-site browser extension (§4) is a
  separate distribution (browser stores), not a PCM package.

### 3.3 Submission / listing (official repository)
1. Host the package zip at a public `download_url`; source must live
   somewhere with issue tracking (GitHub ✓).
2. Merge request to **https://gitlab.com/kicad/addons/metadata**:
   create `packages/<identifier>/` containing the `metadata.json`
   (with `download_*` keys) and optional icon.
3. KiCad CI validates schema + archive; a human reviews; published
   versions are effectively immutable — updates are a new version +
   a new MR.
Launch sequencing (ADVERTISE_BOTH.md) — **PAUSED under the H-1B gate (see the notice at the top); retained as the plan of record for the post-gate revisit:** self-hosted third-party repository first, official MR after a week of public feedback, so the immutable first official version isn't the rawest build. `pcm/build_pcm.py` can produce the zip, the submission metadata, and the self-hosted pair locally; its outputs were removed from this tree when the gate was applied and it must not be run as launch preparation while the gate is on.

## 4. Browser extension — later expansion (outline only)

- **What:** a Manifest V3 extension for the JLCPCB and PCBWay order
  pages. When a user uploads a BOM (and Gerbers) at checkout, the
  extension re-runs the tool-agnostic BOM checks (plus basic Gerber
  presence/sanity where feasible) client-side and shows a
  PASS/CAUTION/BLOCK panel before payment — the same findings language
  as the CLI, at the exact moment the mistake becomes money.
- **Why it's the any-tool front door:** the fab doesn't care which EDA
  tool made the files, and neither does this surface — it completes
  the §1 scope story (KiCad parser → any-tool BOM → any-tool fab
  checkout).
- **Privacy stance:** all processing local in the browser; no board
  data leaves the machine; no analytics on file contents. This is a
  feature, stated in the listing copy.
- **Reuse:** the checks are deterministic and small — port the BOM
  module to JS (or ship a WASM/pyodide build only if the port proves
  unmaintainable; decision deferred until scoped).
- **Why phase 2, not spike scope:** two store review processes,
  fab-site DOMs that change without notice, and a user base reached
  only after the KiCad wedge proves pull. Building it now would spend
  the spike on distribution plumbing for an unproven checker.

## 5. Market / competitor claims — verification log

All checks run 2026-11-09 via the GitHub API and official KiCad docs:

| Claim | Status |
| --- | --- |
| "KiCad has built-in DRC/ERC" | **Verified** — official checks exist and are exposed headlessly via `kicad-cli` (`sch erc`, `pcb drc`); Preflight's adapter folds their output in rather than reimplementing them. Preflight checks the *order* (BOM, footprint agreement, part risk), which DRC/ERC do not cover. |
| "No KiCad preflight tool exists" | **FALSE — do not use.** GitHub search `kicad preflight` returned 4 repos on 2026-11-09: `BennettDixon/pcb-preflight` (MIT, created 2026-10-16, "Your board passed DRC. Can you order it?"), `ratemypcb/ratemypcb` (Apache-2.0, CLI + KiCad plugin), `NeilD201/PreFlight`, and one renamed mirror. |
| "No *dominant* preflight tool" | **Verified with method stated:** every competitor found has **0 stars** and was created Aug–Nov 2026. The category is new and unsettled — that is the honest claim, and it expires; re-check before repeating it at launch (the daily watch does this). |
| "PCM is the in-app distribution channel for KiCad plugins" | **Verified** — official addons guide, §3 above. |
| Pricing/claims about Flux, Quilter, CELUS etc. from the market scan | **UNVERIFIED in this pass** — not repeated in any launch copy until checked against live sources. |
