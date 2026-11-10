# Stream-Downloader - contributor instructions

Project of **Angela López Mendoza**. Address her as **Angela** in every message.
Stream-Downloader is a private, single-user Windows x64 MCP server (STDIO) that downloads videos with
yt-dlp and FFmpeg. It is a simplified, fully decoupled mirror of the video downloader inside her
IDM-MCP Core 0.3.1 (`C:\Development\IDM`). Current release: **1.0.0**.

- [README.md](./README.md): setup, tools, configuration, tests, troubleshooting.
- [DESIGN.md](./DESIGN.md): implemented architecture and contract.
- [COMPARISON.md](./COMPARISON.md): every original line beside its counterpart, with reasons.
- [CHANGELOG.md](./CHANGELOG.md): history.

Source code and dated test evidence decide what is implemented. If a document disagrees, fix the
document; never invent support or widen scope silently.

## PRIVATE - this project must never be public

Angela's words: *"this should be never public!"* Stream-Downloader is private and stays on this
machine. There is **no git repository** here (she had the one created on 2026-10-06 removed before any
commit). Never `git init`, commit, push, add a remote, upload, open a PR, publish an artifact or
document page, or share a link to anything in this folder. Only Angela decides otherwise, in her own
words.

## Angela's machine rules, as they apply here

1. **No version control, no publication.** See the section above. Her machine-wide git rules
   (`main` only, never rewrite history, no worktrees or background-task chips) still govern any
   repository she creates herself.
2. **Everything runs visibly, in the foreground.** Builds, tests and scripts run in a visible window:
   `Start-Process pwsh -ArgumentList '-NoExit','-EncodedCommand',<base64>` (with the sandbox
   disabled) opens a real Windows Terminal window on her desktop. Never `run_in_background`.
3. **No headless tests.** Browsers are headed, in the real Chrome, driven by Tlamatini's
   **Playwrighter**. Every photo is a full-desktop shot by Tlamatini's **Shoter** - `PIL.ImageGrab`
   is forbidden. Before each photo Tlamatini's **Windower** brings the window under test forward and
   pins it; the harness confirms it owns the foreground and never counts a photo it cannot confirm
   (a photo of another app proves nothing - this happened on the first run). Her taskbar auto-hides,
   so the clock goes into the photographed window's title and the evidence page.
4. **Watch tests live.** Poll the run's logs every 5-15 s and report what changed. Read the **head**
   of a failing log first (the root cause is there, not in a stack tail), and the server's own STDERR
   (`server-*.log`), not only the harness output. Never report a result you have not seen.
5. **Tests establish their environment** and stop with a named reason and exit code when they truly
   cannot (see `tests/harness/preflight.py`). Never `taskkill /PID <pid> /T`: test hosts run in a
   Windows Job Object and only that job is terminated.
6. **Never build only for this workstation.** Sizes come from `MachineProfile` (processors, memory,
   seek penalty of the destination volume); unknown hardware is not the worst case; explicit settings
   are obeyed exactly; the decision and its measurement are always reported. Test hardware you do not
   have by injecting it (`tests/StreamDownloader.Tests`).
7. **Tlamatini's tools first.** Screenshots, browser and window work go through Tlamatini's agents
   (bridges in `tests/harness/tlamatini.py`). If one lacks a capability, say so to Angela in one line;
   the known gap: Windower's `list` does not report the foreground window, so the harness confirms
   focus with a read-only Win32 query.

## Decoupling contract (enforced by `tests/verify_decoupling.py`)

* Nothing under `src/`, `scripts/`, `tests/` (except `verify_decoupling.py` and
  `verify_comparison.py`), `Stream-Downloader.json`, `.gitignore` or the build files may name the
  original program, its COM interop, its tool names, folders, staging folder or progress markers.
  Only the documentation and `tools/generate_comparison.py` may explain the origin.
* Own identity everywhere: server `Stream-Downloader`, tools `stream_*`, configuration section
  `StreamDownloader`, state `%LOCALAPPDATA%\Stream-Downloader\state`, tools
  `%LOCALAPPDATA%\Stream-Downloader\tools`, staging `.stream-downloader-staging`, DPAPI entropy
  `Stream-Downloader/v1`, markers `@@STREAMDL_*@@`.
* Nothing reads `C:\Development\IDM` at run time. Only the comparison generator reads it, at
  development time, and never writes to it.

## Comparison contract

* After **any** change to a file in this repository, run `python tools\generate_comparison.py`.
  The `comparison` test phase fails when COMPARISON.md or COMPARISON.html is stale or incomplete.
* A line removed, changed or added relative to the original needs its reason: a `MEMBER_NOTES`
  entry (file + enclosing member) or the file's `PAIRS` note in the generator.
* New files are picked up automatically; give them an `ADDED_NOTES` entry.

## Source map

| Path | Responsibility |
| --- | --- |
| `src/StreamDownloader.McpHost/Program.cs` | STDIO host, configuration, STDERR logging |
| `src/StreamDownloader.McpHost/Tools/StreamDownloaderTools.cs` | The six tools and their schemas |
| `src/StreamDownloader.McpHost/Models/CoreModels.cs` | Response, capability, job and persisted-state models |
| `src/StreamDownloader.McpHost/Services/StreamCoreService.cs` | Requests, idempotency, slots, workers, finalization, recovery, lifecycle |
| `src/StreamDownloader.McpHost/Services/YtDlpService.cs` | Tool discovery/acquisition, yt-dlp runner, progress parsing, ffprobe verification |
| `src/StreamDownloader.McpHost/Services/MachineProfile.cs` | Hardware probe and the sizing decisions |
| `src/StreamDownloader.McpHost/Services/StateStore.cs` | Single-owner transactional JSON state |
| `src/StreamDownloader.McpHost/Services/SecretVault.cs` | Current-user DPAPI for source URLs |
| `src/StreamDownloader.McpHost/Services/PathPolicy.cs` | Destination and Windows filename validation |
| `src/StreamDownloader.McpHost/Services/NetworkInspector.cs` | URL policy and private-address checks |
| `src/StreamDownloader.McpHost/Services/StreamDownloaderOptions.cs` | Configuration keys and defaults |
| `Stream-Downloader.json` | External-MCP spec for Tlamatini (and Claude Code) |
| `scripts/Build-Local.ps1` / `Test-Local.ps1` / `Verify-McpProtocol.ps1` | Build, the visible suite, protocol check |
| `tests/StreamDownloader.Tests/` | C# unit/regression harness (81 checks) |
| `tests/verify_*.py`, `tests/harness/` | Visible integration suites and their shared harness |
| `tests/my_videos.json` | Angela's videos for the end-to-end test |
| `tools/generate_comparison.py` | Generates COMPARISON.md / COMPARISON.html |

## Engineering invariants (kept from the original)

* STDOUT belongs to MCP; diagnostics go to STDERR. Child processes get their own, immediately
  closed STDIN (inheriting the live MCP pipe once stalled an idle client).
* One host per state directory (`owner.lock`). Malformed, incomplete or inconsistent state stops
  startup; it is never reset and `.bak` is never restored silently.
* Persist a job before starting work. Idempotency binds the normalized request; a changed request
  needs a new key; a deleted completed file needs a new key, not a replay.
* Stage per job on the destination volume. Never overwrite. Save the move intent, size and SHA-256
  before moving; on restart, complete or reject the move from that evidence.
* Recover before the MCP transport accepts calls. A clean stop cancels and awaits children
  (`host_stopped`); after an unclean restart a non-final job is `outcome_unknown`, never retried
  automatically.
* Quality caps height on every fallback and is verified; video containers are remuxed, never
  renamed; impossible codec/container pairs fail.
* yt-dlp is acquired only after a single matching SHA-256 entry; it is never silently updated; a
  freshly acquired binary that does not run is quarantined, not reused.
* Hardware sizing never exceeds the original's ceilings (4 fragments) and never raises priority
  above Normal.

## Workflow

```powershell
# build (visible window)
& 'C:\Development\XAIHT\Stream-Downloader\scripts\Build-Local.ps1'
# the whole visible suite: build, unit, decoupling, comparison, local, tlamatini, myvideos
& 'C:\Development\XAIHT\Stream-Downloader\scripts\Test-Local.ps1'
# a subset
& 'C:\Development\XAIHT\Stream-Downloader\scripts\Test-Local.ps1' -Only unit,local
# after editing anything
python tools\generate_comparison.py
```

Keep failed runs as well as passing ones (`test-artifacts/`, not versioned). Report counts as the
counts of that run, with the evidence path; a passing photo-less run is "evidence incomplete", not a
pass. In a handoff name the executable hash (`artifacts/local/manifest.json`), the evidence folder
and the remaining limits.
