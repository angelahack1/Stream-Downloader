"""Bridges to Tlamatini's own agents - Angela's standing rule for every test on this machine.

* Every screenshot is a FULL-DESKTOP photo taken by Tlamatini's **Shoter** agent (through its
  harness launcher ``shoter_shot.take_shot``). ``PIL.ImageGrab`` is never used here: if Shoter
  cannot take the photo, that is reported as missing evidence - never papered over.
* Before every photo, Tlamatini's **Windower** agent brings the window under test forward and pins
  it on top for the length of the shot (``windower_focus.focus`` / ``set_topmost``), exactly as
  Tlamatini's own visible harnesses do. A photo of some other application proves nothing, so the
  harness then CONFIRMS which window really owns the foreground (a read-only Win32 query - Windower's
  ``list`` does not report focus) and records a photo it cannot confirm as UNVERIFIED evidence.
* The browser is driven by Tlamatini's **Playwrighter** agent (through ``playwrighter_run``),
  HEADED, in the real installed Chrome. No hand-rolled Playwright script.

Angela's taskbar is set to auto-hide, so the clock is put where every photo shows it: in the
title of the window being photographed (and on the browser evidence page).

Stream-Downloader's runtime does not depend on Tlamatini; only its visible-evidence tests do.
The checkout is found through ``TLAMATINI_ROOT``, then the sibling ``..\\Tlamatini`` folder,
then ``C:\\Development\\Tlamatini``.
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve()
CANDIDATE_ROOTS = [
    os.environ.get("TLAMATINI_ROOT", ""),
    str(_HERE.parents[3] / "Tlamatini"),
    r"C:\Development\Tlamatini",
]
_HARNESS = Path(".claude") / "skills" / "tlamatini-daily-chat-test" / "harness"
_AGENTS = Path("Tlamatini") / "agent" / "agents"
_LAUNCHERS = ("shoter_shot.py", "playwrighter_run.py", "windower_focus.py")


class TlamatiniMissing(RuntimeError):
    """No usable Tlamatini checkout (agents + launchers) was found."""


def find_root() -> Path | None:
    for candidate in CANDIDATE_ROOTS:
        if not candidate:
            continue
        root = Path(candidate)
        if (all((root / _AGENTS / agent / f"{agent}.py").is_file() for agent in ("shoter", "playwrighter", "windower"))
                and all((root / _HARNESS / launcher).is_file() for launcher in _LAUNCHERS)):
            return root
    return None


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def foreground_title() -> str:
    """Title of the window that owns the foreground right now (read-only)."""
    try:
        user32 = ctypes.windll.user32
        handle = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def set_console_title(title: str) -> None:
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


class Tlamatini:
    def __init__(self):
        root = find_root()
        if root is None:
            raise TlamatiniMissing("Tlamatini's Shoter/Playwrighter/Windower agents and launchers were not found in: "
                                   + ", ".join(c for c in CANDIDATE_ROOTS if c))
        self.root = root
        self.agents = root / _AGENTS
        # playwrighter_run reads TLAMATINI_AGENTS_ROOT at import time; the Playwrighter agent's own
        # `shoter` step resolves the Shoter template through the same variable.
        os.environ["TLAMATINI_AGENTS_ROOT"] = str(self.agents)
        self.shoter = _load("tlamatini_shoter_shot", root / _HARNESS / "shoter_shot.py")
        self.shoter.TEMPLATE = str(self.agents / "shoter")
        self.playwrighter = _load("tlamatini_playwrighter_run", root / _HARNESS / "playwrighter_run.py")
        self.windower = _load("tlamatini_windower_focus", root / _HARNESS / "windower_focus.py")
        self.windower._CANDIDATES.insert(0, str(self.agents / "windower"))

    # ------------------------------------------------------------------ windows
    def _runtime(self, output_dir: Path) -> str:
        runtime = Path(output_dir).parent / "_windower"
        runtime.mkdir(parents=True, exist_ok=True)
        return str(runtime)

    def arrange(self, window_title: str, mode: str, output_dir: Path) -> bool:
        """Snap a window into part of the screen with Windower (left, right, full, ...)."""
        result = self.windower._run(self._runtime(output_dir), {
            "action": "arrange", "window_title": window_title, "match_mode": "substring",
            "match_index": 0, "arrange_mode": mode, "activate_after": True})
        return bool(result) and result.returncode == 0 and "matched: true" in ((result.stdout or "") + (result.stderr or "")).lower()

    def windows(self, window_title: str, output_dir: Path) -> str:
        return self.windower.list_windows(window_title, self._runtime(output_dir))

    # ------------------------------------------------------------------ evidence
    def evidence(self, output_dir: Path, name: str, window_title: str) -> dict:
        """Bring ``window_title`` forward with Windower, pin it, CONFIRM it owns the foreground,
        photograph the whole desktop with Shoter, unpin. Never raises; never pretends."""
        output_dir = Path(output_dir)
        runtime = self._runtime(output_dir)
        record = dict(path=None, verified=False, window=window_title, foreground="", focused=False, pinned=False)
        try:
            record["focused"] = self.windower.focus(window_title, runtime)
            record["pinned"] = self.windower.set_topmost(window_title, runtime, True)
            time.sleep(0.5)
            record["foreground"] = foreground_title()
            record["verified"] = record["focused"] and window_title.lower() in record["foreground"].lower()
            record["path"] = self.shoter.take_shot(str(output_dir), name, runtime_base=str(output_dir.parent / "_shoter"))
        except Exception as exc:  # fail-open: the caller reports the missing evidence
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if record["pinned"]:
                self.windower.set_topmost(window_title, runtime, False)
        record["verified"] = bool(record["verified"] and record["path"])
        return record

    def console_evidence(self, output_dir: Path, name: str, console_title: str) -> dict:
        """Photograph this harness's own console window, with the time in its title."""
        stamped = f"{console_title} | {datetime.now():%Y-%m-%d %H:%M:%S}"
        set_console_title(stamped)
        time.sleep(0.3)
        try:
            return self.evidence(output_dir, name, console_title)
        finally:
            set_console_title(console_title)

    # ------------------------------------------------------------------ browser
    def browse(self, steps: list, runtime_base: Path, start_url: str, hold_open_seconds: int = 2,
               timeout_ms: int = 60000, timeout: int = 600, viewport=(1280, 860)) -> dict:
        """Drive the real installed Chrome, HEADED, with Playwrighter. Step failures are data."""
        return self.playwrighter.run_steps(steps, str(runtime_base), start_url=start_url, headless=False,
                                           hold_open_seconds=hold_open_seconds, timeout_ms=timeout_ms,
                                           timeout=timeout, viewport=viewport)
