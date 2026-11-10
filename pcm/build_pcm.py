#!/usr/bin/env python3
"""Build the KiCad PCM package for KiCad Preflight.

Produces, under pcm/dist/:
  * com.github.RakTikoo.kicad-preflight-<version>.zip — the package
    archive (metadata.json WITHOUT download_* keys, per the official
    addons guide: a file cannot carry its own hash),
  * metadata-submission.json — the metadata WITH download_sha256 /
    download_size / install_size / download_url filled in; this is the
    file submitted by merge request to
    https://gitlab.com/kicad/addons/metadata when Rakshit approves the
    official-repo submission (Jan 15, 2027 in the launch plan),
  * repository.json + packages.json — a self-hosted third-party PCM
    repository (launch plan: live Jan 4, 2027, before the official
    listing exists).

The engine package (../preflight/) is copied into the archive under
plugins/ so the plugin and the CLI run the identical code.

Usage: python3 pcm/build_pcm.py [--download-base URL]
Does NOT upload or submit anything anywhere.
"""

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
PKG = HERE / "package"
DIST = HERE / "dist"
IDENTIFIER = "com.github.RakTikoo.kicad-preflight"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--download-base",
                    default="https://github.com/RakTikoo/kicad-preflight"
                            "/releases/download")
    args = ap.parse_args()

    meta = json.loads((PKG / "metadata.json").read_text(encoding="utf-8"))
    version = meta["versions"][0]["version"]
    if DIST.exists():
        shutil.rmtree(DIST)
    stage = DIST / "stage"
    plugins = stage / "plugins"
    plugins.mkdir(parents=True)
    # Plugin entry point + icon, then the engine package itself.
    for f in (PKG / "plugins").iterdir():
        shutil.copy2(f, plugins / f.name)
    shutil.copytree(ROOT / "preflight", plugins / "preflight",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(PKG / "resources", stage / "resources")
    shutil.copy2(PKG / "metadata.json", stage / "metadata.json")

    zip_name = f"{IDENTIFIER}-{version}.zip"
    zip_path = DIST / zip_name
    install_size = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                install_size += path.stat().st_size
                zf.write(path, path.relative_to(stage))
    blob = zip_path.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()

    download_url = f"{args.download_base}/v{version}/{zip_name}"
    submission = json.loads(json.dumps(meta))
    submission["versions"][0].update({
        "download_sha256": sha,
        "download_size": len(blob),
        "download_url": download_url,
        "install_size": install_size,
    })
    (DIST / "metadata-submission.json").write_text(
        json.dumps(submission, indent=4) + "\n", encoding="utf-8")

    (DIST / "repository.json").write_text(json.dumps({
        "$schema": "https://go.kicad.org/pcm/schemas/v2",
        "name": "KiCad Preflight (third-party)",
        "description": "Self-hosted repository for KiCad Preflight, "
                       "used until the official PCM listing lands.",
        "maintainer": meta["author"],
    }, indent=4) + "\n", encoding="utf-8")
    (DIST / "packages.json").write_text(
        json.dumps([submission], indent=4) + "\n", encoding="utf-8")

    print(f"Built {zip_path} ({len(blob)} bytes, sha256 {sha[:16]}…)")
    print(f"Install size {install_size} bytes; download URL {download_url}")
    print("Nothing was uploaded or submitted.")


if __name__ == "__main__":
    main()
