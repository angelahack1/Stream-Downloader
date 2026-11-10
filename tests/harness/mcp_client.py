"""MCP STDIO client used by every Stream-Downloader suite. Standard library only.

It drives the real executable exactly as Tlamatini or Claude Code would, with two of Angela's
rules built in:

* Every setting is isolated per run through ``StreamDownloader__*`` environment variables, so a
  test never touches her real job history or download folders.
* A host that will not close gracefully is stopped by terminating the Windows **Job Object** it
  runs in: that host and the yt-dlp/FFmpeg children it launched, nothing else. ``taskkill /T`` is
  never used - it kills a whole process tree and has taken a test's own console down with it.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXECUTABLE = ROOT / "artifacts" / "local" / "StreamDownloader.McpHost.exe"
SPEC_FILE = ROOT / "Stream-Downloader.json"
SERVER_KEY = "Stream-Downloader"
EXPECTED_TOOLS = ("stream_capabilities", "stream_download", "stream_download_status",
                  "stream_download_list", "stream_download_wait", "stream_download_retry")
TOOLS_DIRECTORY = Path(os.environ.get("LOCALAPPDATA", "")) / "Stream-Downloader" / "tools"


def sha256_file(path) -> str:
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


# --------------------------------------------------------------------------- Job Object
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class ProcessJob:
    """A Windows Job Object holding exactly one test host and whatever it launches."""

    _EXTENDED_LIMIT_INFORMATION = 9
    _KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def __init__(self, pid: int):
        self.handle = _kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(self.handle, self._EXTENDED_LIMIT_INFORMATION,
                                                 ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = _kernel32.OpenProcess(self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _kernel32.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(process)

    def terminate(self) -> None:
        if self.handle:
            _kernel32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        # KILL_ON_JOB_CLOSE: anything this host left behind ends here, and only that.
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def descendants_of(pid: int) -> list[dict]:
    """Every live process descended from ``pid``. Windows never re-parents orphans, so a child
    that outlived its parent still names it - and yt-dlp.exe is itself a bootloader with a child."""
    script = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name | "
              "ConvertTo-Json -Compress")
    completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    table = json.loads(completed.stdout or "[]")
    table = table if isinstance(table, list) else [table]
    found, frontier = [], {int(pid)}
    while frontier:
        children = [p for p in table if p.get("ParentProcessId") in frontier and p.get("ProcessId") not in frontier]
        found.extend(children)
        frontier = {p["ProcessId"] for p in children} - {int(pid)}
        table = [p for p in table if p not in children]
    return found


# --------------------------------------------------------------------------- client
class HostRefused(RuntimeError):
    """The host exited or never answered the MCP handshake; carries its own STDERR reason."""


class McpClient:
    def __init__(self, run: Path, executable: Path = DEFAULT_EXECUTABLE, isolate: bool = True, **overrides):
        self.responses: queue.Queue = queue.Queue()
        self.counter = 0
        self.lock = threading.Lock()
        run = Path(run)
        run.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        settings = {}
        if isolate:
            settings = dict(StateDirectory=str(run / "state"), DefaultDownloadDirectory=str(run / "output"),
                            BlockPrivateNetworkTargets="false", AutoAcquireYtDlp="false",
                            SiteDownloadTimeoutSeconds="180")
        settings.update(overrides)
        env.update({"StreamDownloader__" + key: str(value) for key, value in settings.items()})
        self.settings = settings
        self.log_path = run / ("server-" + uuid.uuid4().hex[:8] + ".log")
        self.log = open(self.log_path, "w", encoding="utf-8")
        spec = json.loads(SPEC_FILE.read_text(encoding="utf-8-sig"))["mcpServers"][SERVER_KEY]
        launch_args = spec.get("args", []) if Path(spec["command"]).resolve() == Path(executable).resolve() else []
        self.process = subprocess.Popen([str(executable), *launch_args], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8")
        try:
            self.job: ProcessJob | None = ProcessJob(self.process.pid)
        except OSError:
            if self.process.poll() is None:
                raise
            self.job = None  # It already exited (e.g. refused its state); the handshake below says why.

        def receive():
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except Exception as exc:
                    self.responses.put(exc)
            self.responses.put(EOFError("MCP stdout closed"))

        threading.Thread(target=receive, daemon=True).start()
        try:
            self.initialize = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                          "clientInfo": {"name": "stream-downloader-tests", "version": "1.0.0"}},
                                           timeout=60)
        except (EOFError, queue.Empty, TimeoutError, OSError) as exc:
            self.process.poll() is None or self.process.wait(timeout=5)
            reason = self.stderr_head()
            self.close()
            raise HostRefused(f"{type(exc).__name__}: {reason or 'no STDERR output'}") from exc
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _write(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def stderr_head(self, lines: int = 6) -> str:
        """The FIRST lines of the host's STDERR: the root cause lives there, not in the stack tail."""
        try:
            self.log.flush()
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        meaningful = [line.strip() for line in text.splitlines()
                      if line.strip() and not line.strip().startswith("at ")]
        return " | ".join(meaningful[:lines])

    def request(self, method: str, params: dict, timeout: float = 60):
        with self.lock:
            self.counter += 1
            request_id = self.counter
            self._write(dict(jsonrpc="2.0", id=request_id, method=method, params=params))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                response = self.responses.get(timeout=max(0.1, deadline - time.monotonic()))
                if isinstance(response, Exception):
                    raise response
                if response.get("id") == request_id:
                    if "error" in response:
                        raise AssertionError(response["error"])
                    return response["result"]
            raise TimeoutError(method)

    def call(self, name: str, **arguments) -> dict:
        result = self.request("tools/call", dict(name=name, arguments=arguments))
        assert not result.get("isError"), result
        assert "structuredContent" in result, result
        return result["structuredContent"]

    def wait_for(self, job: dict, console=None, timeout: float = 600, expected: str | None = "completed_verified") -> dict:
        """Follow a job with stream_download_wait (bounded long-poll), reporting every change live."""
        version = job["stateVersion"]
        current = job
        deadline = time.monotonic() + timeout
        if console:
            console.job(job)
        while time.monotonic() < deadline:
            remaining = max(1, min(10, int(deadline - time.monotonic())))
            response = self.call("stream_download_wait", jobId=job["jobId"], afterVersion=version, timeoutSeconds=remaining)
            assert response.get("data") is not None, response
            current = response["data"]
            if current["stateVersion"] != version and console:
                console.job(current)
            version = current["stateVersion"]
            if current["terminal"]:
                assert expected is None or current["state"] == expected, \
                    f"expected {expected}, got {current['state']}: {current.get('failureCode')} {current.get('failureMessage')}"
                return current
        raise TimeoutError(f"job {job['jobId']} still {current['state']} after {timeout:.0f}s")

    def close(self, timeout: float = 20) -> bool:
        """Graceful MCP shutdown: close STDIN and let the host stop its own children."""
        graceful = True
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            self.process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            graceful = False
            if self.job:
                self.job.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        if self.job:
            self.job.close()
        if not self.log.closed:
            self.log.close()
        return graceful

    def crash(self) -> None:
        """An unclean host death: the host and its children end at once, no shutdown path runs."""
        assert self.job is not None, "this host is not inside a Job Object"
        self.job.terminate()
        try:
            self.process.wait(timeout=15)
        finally:
            self.job.close()
            if not self.log.closed:
                self.log.close()
