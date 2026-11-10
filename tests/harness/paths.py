"""Every path a test receives on its command line is confined to this project's evidence folder.

The suites write logs, results, Shoter photos and Windower/Playwrighter runtimes under the run
directory they are given, so ``--run`` / ``--dir`` / ``--name`` / ``--config`` are validated
before use: a directory must resolve - links and junctions followed - inside ``test-artifacts``,
with every part on an allow-list; a file name must be a bare name; a configuration file must live
inside ``tests``. Anything else stops the run with a named reason instead of writing elsewhere.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

from .mcp_client import ROOT

EVIDENCE_ROOT = os.path.realpath(ROOT / "test-artifacts")
TESTS_ROOT = os.path.realpath(ROOT / "tests")
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


class UnsafePath(ValueError):
    """A command-line path that would leave the project's evidence or tests folder."""


def _inside(real: str, base: str) -> bool:
    return os.path.normcase(real).startswith(os.path.normcase(base) + os.sep)


def evidence_dir(requested: str | os.PathLike | None, prefix: str) -> Path:
    """The run directory: the requested one, validated, or a new unique one - always inside test-artifacts."""
    if requested is None:
        parts = [time.strftime(f"{prefix}-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]]
    else:
        real = os.path.realpath(requested)
        if not _inside(real, EVIDENCE_ROOT):
            raise UnsafePath(f"{requested} is not inside {EVIDENCE_ROOT}")
        parts = list(Path(os.path.relpath(real, EVIDENCE_ROOT)).parts)
    if not parts or any(not _PART.fullmatch(part) for part in parts):
        raise UnsafePath(f"{requested} has a path part outside [A-Za-z0-9._-]")
    run = Path(EVIDENCE_ROOT).joinpath(*parts)
    run.mkdir(parents=True, exist_ok=True)
    if not _inside(os.path.realpath(run), EVIDENCE_ROOT):  # a junction created meanwhile cannot redirect it
        raise UnsafePath(f"{run} resolves outside {EVIDENCE_ROOT}")
    return run


def file_name(name: str, suffix: str) -> str:
    """A bare file name with the expected extension: no separators, no drive, no parent references."""
    if os.path.basename(name) != name or not _PART.fullmatch(name) or ".." in name or not name.lower().endswith(suffix):
        raise UnsafePath(f"unsafe file name: {name!r}")
    return name


def tests_file(requested: str | os.PathLike, suffix: str) -> Path:
    """An existing file inside this project's tests folder (e.g. the list of Angela's videos)."""
    real = os.path.realpath(requested)
    if not _inside(real, TESTS_ROOT) or not real.lower().endswith(suffix) or not os.path.isfile(real):
        raise UnsafePath(f"{requested} is not a {suffix} file inside {TESTS_ROOT}")
    return Path(real)
