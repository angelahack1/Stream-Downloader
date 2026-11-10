"""Live, visible console output for Angela, mirrored line by line into a plain-text log.

Every harness prints to the real console window (colours, progress, timestamps) and appends the
same lines - without colour codes - to its run log, so the run can be watched live AND read back
afterwards. Nothing is buffered: each line is flushed the moment it is written.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
C = dict(reset="\x1b[0m", dim="\x1b[2m", bold="\x1b[1m", red="\x1b[91m", green="\x1b[92m",
         yellow="\x1b[93m", blue="\x1b[94m", magenta="\x1b[95m", cyan="\x1b[96m", white="\x1b[97m")


def _enable_virtual_terminal() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def human_bytes(value) -> str:
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.2f} {unit}"
        size /= 1024
    return f"{size:,.2f} GiB"


class Console:
    def __init__(self, log_path: Path | None = None, title: str | None = None):
        _enable_virtual_terminal()
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # A unique title lets Windower find THIS window (never an older one) before each photo.
        self.title = title or "Stream-Downloader test"
        if os.name == "nt":
            try:
                ctypes.windll.kernel32.SetConsoleTitleW(self.title)
            except Exception:
                pass

    def emit(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"{C['dim']}{stamp}{C['reset']} {text}", flush=True)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as log:
                log.write(f"{stamp} {ANSI.sub('', text)}\n")

    def banner(self, title: str, subtitle: str = "") -> None:
        bar = "=" * 78
        self.emit(f"{C['magenta']}{bar}{C['reset']}")
        self.emit(f"{C['bold']}{C['white']}  {title}{C['reset']}")
        if subtitle:
            self.emit(f"{C['magenta']}  {subtitle}{C['reset']}")
        self.emit(f"{C['magenta']}{bar}{C['reset']}")

    def section(self, title: str) -> None:
        self.emit(f"{C['blue']}--- {title} ---{C['reset']}")

    def step(self, text: str) -> None:
        self.emit(f"{C['cyan']}>{C['reset']} {text}")

    def ok(self, text: str) -> None:
        self.emit(f"{C['green']}PASS{C['reset']} {text}")

    def fail(self, text: str) -> None:
        self.emit(f"{C['red']}FAIL{C['reset']} {text}")

    def warn(self, text: str) -> None:
        self.emit(f"{C['yellow']}WARN{C['reset']} {text}")

    def info(self, text: str) -> None:
        self.emit(f"{C['dim']}     {text}{C['reset']}")

    def job(self, view: dict, label: str = "") -> None:
        """One live line per observed job change: state, stream progress and the latest event."""
        state = view.get("state", "?")
        colour = C["green"] if state == "completed_verified" else C["red"] if state.startswith("failed") or state == "outcome_unknown" else C["yellow"]
        parts = [f"{C['white']}{label:<26}{C['reset']}"] if label else []
        parts.append(f"{colour}{state:<18}{C['reset']}")
        if view.get("streamPercent") is not None:
            percent = float(view["streamPercent"])
            filled = int(max(0.0, min(100.0, percent)) / 5)
            parts.append(f"[{'#' * filled}{'.' * (20 - filled)}] {percent:5.1f}% of current stream")
        if view.get("observedBytes") is not None:
            parts.append(human_bytes(view["observedBytes"]))
        events = view.get("events") or []
        if events:
            parts.append(f"{C['dim']}{events[-1]['code']}{C['reset']}")
        self.emit("     " + "  ".join(parts))
