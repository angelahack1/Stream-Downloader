"""Tests establish their own environment, and stop with a NAMED reason when they truly cannot.

A failed run must never surface as a meaningless stack trace or selector timeout. Each missing
prerequisite has its own exit code, so a red run says exactly which edge broke.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path

from .mcp_client import DEFAULT_EXECUTABLE, SERVER_KEY, SPEC_FILE

EXIT_PASS = 0
EXIT_TEST_FAILURE = 1
EXIT_EVIDENCE_INCOMPLETE = 3      # every check passed, but at least one Shoter photo is missing
EXIT_EXECUTABLE_MISSING = 10
EXIT_SPEC_INVALID = 11
EXIT_FFMPEG_MISSING = 12
EXIT_FFPROBE_MISSING = 13
EXIT_YTDLP_UNAVAILABLE = 14
EXIT_JS_RUNTIME_MISSING = 15
EXIT_NETWORK_UNREACHABLE = 16
EXIT_TLAMATINI_MISSING = 17
EXIT_CHROME_MISSING = 18
EXIT_ORIGINAL_SOURCE_MISSING = 19
EXIT_HOST_REFUSED = 20
EXIT_UNSAFE_PATH = 21              # a command-line path would leave test-artifacts / tests

NAMES = {value: name for name, value in globals().items() if name.startswith("EXIT_")}


class PreflightFailure(RuntimeError):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason

    def __str__(self) -> str:
        return f"{NAMES.get(self.code, self.code)} ({self.code}): {self.reason}"


def executable(path: Path = DEFAULT_EXECUTABLE) -> Path:
    if not Path(path).is_file():
        raise PreflightFailure(EXIT_EXECUTABLE_MISSING,
                               f"{path} does not exist. Build it first: scripts\\Build-Local.ps1")
    return Path(path)


def spec() -> dict:
    try:
        entry = json.loads(SPEC_FILE.read_text(encoding="utf-8-sig"))["mcpServers"][SERVER_KEY]
    except (OSError, KeyError, ValueError) as exc:
        raise PreflightFailure(EXIT_SPEC_INVALID, f"{SPEC_FILE.name} is unreadable or lacks mcpServers.{SERVER_KEY}: {exc}")
    if not entry.get("command"):
        raise PreflightFailure(EXIT_SPEC_INVALID, f"{SPEC_FILE.name} has no command for {SERVER_KEY}")
    return entry


def ffmpeg_pair() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PreflightFailure(EXIT_FFMPEG_MISSING, "FFmpeg is not on PATH (e.g. 'choco install ffmpeg').")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise PreflightFailure(EXIT_FFPROBE_MISSING, "ffprobe is not on PATH; it ships with FFmpeg.")
    return ffmpeg, ffprobe


def javascript_runtime() -> str:
    found = shutil.which("deno") or shutil.which("node")
    if not found:
        raise PreflightFailure(EXIT_JS_RUNTIME_MISSING, "YouTube needs Deno or Node for yt-dlp's JavaScript challenges.")
    return found


def network(host: str = "www.youtube.com", port: int = 443) -> None:
    try:
        with socket.create_connection((host, port), timeout=10):
            pass
    except OSError as exc:
        raise PreflightFailure(EXIT_NETWORK_UNREACHABLE, f"{host}:{port} is unreachable: {exc}")


def chrome() -> str:
    for candidate in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                      os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if Path(candidate).is_file():
            return candidate
    raise PreflightFailure(EXIT_CHROME_MISSING, "Google Chrome is not installed; Playwrighter drives the real Chrome.")
