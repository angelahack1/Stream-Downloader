# Changelog - Stream-Downloader

All notable changes to this private, single-user MCP server by Angela López Mendoza.
The [README](./README.md) and [DESIGN.md](./DESIGN.md) are the current operating contract.

## 1.0.0 - 2026-10-06 - the decoupled mirror

Stream-Downloader starts as a simplified mirror of the video downloader inside IDM-MCP Core 0.3.1
(`C:\Development\IDM`), completely decoupled from that program. [COMPARISON.md](./COMPARISON.md)
shows every original line beside its counterpart and the reason for every difference.

### Kept, line for line where possible

- The yt-dlp/FFmpeg pipeline (`YtDlpService`): checksum-verified first-use acquisition with
  quarantine of a binary that does not run, Deno/Node runtime selection, format selectors that cap
  height on every fallback, `after_move` exact-output reporting, ffprobe verification, URL redaction
  of diagnostics, and child processes with their own closed STDIN, bounded time and tree kill.
- The job model and lifecycle: per-job staging on the destination volume, persist-before-act,
  idempotency bound to the normalized request, SHA-256 and move intent saved before the
  never-overwriting move, recovery of both sides of an interrupted move, `outcome_unknown` after an
  unclean restart, retry only of definite failures with a new key.
- The single-owner transactional JSON state with fail-closed validation, DPAPI-protected URLs, the
  path policy and the private-network policy. The same pinned SDK and the same locked packages.

### Removed - everything that belonged to Internet Download Manager

- The COM adapter and its `IDMan.Interop` assembly, direct-file routing, the HTTP probe that chose
  an engine, the inspect/plan/start tools, the staged-file monitor, the browser-panel tool, and the
  IDM-only options, states, models and fields. Ten tools become six: `stream_capabilities`,
  `stream_download`, `stream_download_status`, `stream_download_list`, `stream_download_wait`,
  `stream_download_retry`.
- The original's local snapshot/rollback scripts: project infrastructure, not part of the video
  downloader. Stream-Downloader is private, has no version control, and is never published.

### Added

- **Hardware-aware sizing** (`MachineProfile`): simultaneous downloads, concurrent fragments and
  child priority sized from logical processors, physical memory and the destination volume's seek
  penalty; explicit `MaxConcurrentDownloads`, `ConcurrentFragments`, `ChildProcessPriority` obeyed
  exactly; every decision reported with its measurement.
- `streamPercent` in job views, so progress of the stream in transfer can be watched.
- `Stream-Downloader.json`, the External-MCP spec Tlamatini loads (also Claude Code's shape).
- Own identity everywhere: server name, tool names, configuration section, state and tools folders,
  staging folder, DPAPI entropy and progress markers.
- A visible test suite (`scripts/Test-Local.ps1`): C# unit/regression with injected hardware and
  real Windows priority checks; a decoupling proof; a comparison proof; 19 local MCP integration
  tests including a real unclean crash and first-use acquisition; Tlamatini's own External-MCP
  client loading the spec and downloading through it; and Angela's own YouTube videos, checked
  against an independent yt-dlp oracle, fully decoded and played in headed Chrome. Every test ends
  with a full-desktop Shoter photo of a window that Windower brought forward and the harness confirmed.
- `tools/generate_comparison.py`, `COMPARISON.md` and `COMPARISON.html`.

### Verification

See the "Verified" line of the README's test section for the latest complete run.
