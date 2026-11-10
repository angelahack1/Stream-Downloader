"""One confirmed full-desktop photo of this console (used by Test-Local.ps1 between phases).

Windower brings the window forward and pins it, the harness confirms it owns the foreground, and
Shoter takes the photo. Exit 0 only for a confirmed photo; 3 when the evidence is missing or
could not be confirmed.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from harness import paths
from harness.preflight import EXIT_UNSAFE_PATH
from harness.tlamatini import Tlamatini, TlamatiniMissing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="photo folder inside test-artifacts")
    parser.add_argument("--name", required=True, help="bare .png file name")
    args = parser.parse_args()
    try:
        directory = paths.evidence_dir(args.dir, "photos")
        name = paths.file_name(args.name, ".png")
    except paths.UnsafePath as exc:
        print(f"   !! UNSAFE_PATH ({EXIT_UNSAFE_PATH}): {exc}", flush=True)
        return EXIT_UNSAFE_PATH
    title = f"Stream-Downloader suite {Path(name).stem} {uuid.uuid4().hex[:8]}"
    try:
        evidence = Tlamatini().console_evidence(directory, name, title)
    except TlamatiniMissing as exc:
        print(f"   !! photo missing: {exc}", flush=True)
        return 3
    if evidence["verified"]:
        print(f"   photo (Shoter; Windower brought this window forward, confirmed in front): {evidence['path']}", flush=True)
        return 0
    if evidence["path"]:
        print(f"   !! photo NOT counted as evidence - the window in front was '{evidence['foreground']}'", flush=True)
    else:
        print(f"   !! photo missing: {evidence.get('error', 'Shoter left no image')}", flush=True)
    return 3


if __name__ == "__main__":
    sys.exit(main())
