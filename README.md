# Stream-Downloader

Created by **Angela López Mendoza**. Stream-Downloader is a simplified mirror of the video downloader
inside her private *IDM-MCP Core 0.3.1* (`C:\Development\IDM`). It keeps the same source code, the
same verification and the same recovery guarantees, and is **completely decoupled** from that program:
no Internet Download Manager, no COM, no shared folders, no shared state, no shared tools. Every line of
the original sits beside its counterpart in [COMPARISON.md](./COMPARISON.md), each change with its reason.

| Document | Purpose |
| --- | --- |
| [README.md](./README.md) | Setup, use, configuration, tests, troubleshooting (this file) |
| [DESIGN.md](./DESIGN.md) | Implemented architecture, tool contract, states, recovery, limits |
| [CLAUDE.md](./CLAUDE.md) | Contributor instructions and the rules of this machine |
| [COMPARISON.md](./COMPARISON.md) / [COMPARISON.html](./COMPARISON.html) | Every IDM-MCP Core line beside its Stream-Downloader line |
| [CHANGELOG.md](./CHANGELOG.md) | Release history |

## What it does

```text
stream_download(url, destination)
   -> validate URL and destination, commit a job, return at once
   -> background: wait for a download slot sized from THIS machine
   -> yt-dlp resolves the page, downloads video + audio, FFmpeg merges them
   -> ffprobe verifies container, streams, height cap and duration
   -> SHA-256 recorded, then the file is moved into place - never overwriting anything
stream_download_wait / stream_download_status  ->  completed_verified (or an honest failure)
```

* **YouTube and every other site yt-dlp supports.** Separate HD video and audio streams are merged.
* **Exact destinations.** A folder (`C:\Videos`) or an exact file (`C:\Videos\Talk.mp4`); a filename's
  extension sets the container. Existing files are never overwritten; a newcomer gets ` (1)`.
* **Verified, not hoped.** A job is only `completed_verified` after ffprobe checked the output and its
  SHA-256 was recorded. A crash in the middle of a move is recovered from saved evidence on restart.
* **Built for the machine it runs on.** Simultaneous downloads, concurrent fragments and child-process
  priority are sized at run time from the processors, the memory and whether the destination disk
  pays a seek penalty - and the reason is reported. Explicit settings are obeyed exactly.
* **Independent.** It acquires its **own** official yt-dlp (SHA-256 checked before it ever runs) into
  `%LOCALAPPDATA%\Stream-Downloader\tools`, and keeps its **own** DPAPI-encrypted job history in
  `%LOCALAPPDATA%\Stream-Downloader\state`.

DRM-protected streams are not supported and are not circumvented. No cookies or accounts are read.

## Requirements

| Needed for | What |
| --- | --- |
| Running | Windows 10/11 x64. The executable is self-contained (it carries its .NET runtime). |
| Downloading | **FFmpeg and ffprobe** on PATH or configured (`choco install ffmpeg`, `winget install Gyan.FFmpeg`). |
| YouTube | **Node.js or Deno** on PATH for yt-dlp's JavaScript challenges (no runtime is installed automatically). |
| yt-dlp | Nothing: acquired automatically on first use, checksum-verified. Or install it yourself / set `YtDlpPath`. |
| Building | .NET SDK **10.0.401** (pinned by `global.json`, latest patch roll-forward). |
| Tests | Python **3.11+** with PyYAML, Pillow, Playwright and psutil for Tlamatini's agents; Google Chrome; a Tlamatini checkout. |

## Build

```powershell
& 'C:\Development\XAIHT\Stream-Downloader\scripts\Build-Local.ps1'
```

Restores the locked dependency graph (`packages.lock.json`; the first build creates it) and publishes
`artifacts\local\StreamDownloader.McpHost.exe` with its `appsettings.json` and a `manifest.json` of
hashes. An existing deployed `appsettings.json` is preserved across rebuilds unless
`-UseSourceConfiguration` is passed. Nothing is uploaded anywhere.

## Connect it to Tlamatini

[`Stream-Downloader.json`](./Stream-Downloader.json) is the External-MCP spec Tlamatini loads:

```json
{
  "mcpServers": {
    "Stream-Downloader": {
      "command": "C:\\Development\\XAIHT\\Stream-Downloader\\artifacts\\local\\StreamDownloader.McpHost.exe",
      "args": [],
      "env": {},
      "transport": "stdio",
      "description": "Stream-Downloader - private one-call video downloader ..."
    }
  }
}
```

1. Build once (above).
2. In Tlamatini open **External ▸ MCPs** and **drag `Stream-Downloader.json` onto the dialog** (or ask
   Tlamatini to run `external_mcp_import` with the file's content).
3. Tick **Stream-Downloader** to activate it (at most five External MCPs are active at once).
4. Its tools appear to the model as `ext__Stream-Downloader__stream_download`, and so on.

`tests/verify_tlamatini_load.py` proves this path with Tlamatini's own importer and client, including
a real download driven through Tlamatini - without touching Angela's catalog.

## Connect it to Claude Code (or any MCP client)

The same JSON is the standard `mcpServers` shape. Merge the entry into the client's configuration
without replacing other servers, or with Claude Code:

```powershell
claude mcp add Stream-Downloader -- "C:\Development\XAIHT\Stream-Downloader\artifacts\local\StreamDownloader.McpHost.exe"
```

Then call `stream_capabilities`: expect server `Stream-Downloader`, version `1.0.0`, six tools.
The executable is not an interactive CLI: its STDIN/STDOUT belong to the MCP protocol; diagnostics go
to STDERR.

## The one call: `stream_download`

```json
{
  "url": "https://www.youtube.com/watch?v=vlm3xgiIZms",
  "destination": "C:\\Videos\\Tlamatini.mp4",
  "quality": "1080p",
  "format": "mp4"
}
```

| Argument | Meaning |
| --- | --- |
| `url` | Absolute HTTP/HTTPS URL of a watch page or a direct media file. Credentials in URLs are refused. |
| `destination` | A directory (`C:\Videos`) **or** an exact file path (`C:\Videos\Talk.mp4`). Omit for the default Downloads folder. Add a trailing `\` for a new directory whose name contains a dot. |
| `fileName` | Optional name when `destination` is a directory; its extension (if any) overrides `format`. Without a name the file is named from the video title: `Title [id].mp4`. |
| `quality` | `best` (default), `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`, `240p`, `144p`, or `audio`. A **maximum** height, not an exact rendition. |
| `format` | Video: `mp4` (default), `mkv`, `webm`. Audio: `mp3` (default), `m4a`, `flac`, `wav`, `opus`, `aac`. A destination filename's extension wins. |
| `idempotencyKey` | Optional 8-128 characters. The same key with the same request returns the same job; with a different request it is refused. Omitted keys are derived from the request. |

The call returns as soon as the job is committed. Then follow it:

* `stream_download_wait(jobId, afterVersion)` waits up to 45 s for the next change; pass the last
  `stateVersion` you saw. Repeat until `terminal` is true.
* `stream_download_status(jobId)` returns the current job at once.

Check the response envelope (`ok`, `code`, `message`, `data`), not only the MCP transport. Read the
final path from `data.destination`. Only `completed_verified` is a finished download.

### All six tools

| Tool | What it does |
| --- | --- |
| `stream_capabilities` | yt-dlp/FFmpeg/JavaScript readiness, quality presets, roots, state folder, and the measured machine profile with the decisions it drove. Never downloads or spawns anything. |
| `stream_download` | The one-call download above. |
| `stream_download_status` | Durable job state, progress of the stream in transfer (`streamPercent`, `observedBytes`), media facts, SHA-256, failure details, recent events. Detects a completed file that has since vanished or changed size. |
| `stream_download_wait` | Bounded long-poll (1-45 s) for the next state change. |
| `stream_download_list` | Jobs created by this server, newest first, optional exact state filter, limit 1-100. |
| `stream_download_retry` | A new job for a definitively failed one (`failed_download`, `failed_finalization`), with a new key. Refuses unknown, running and completed jobs. |

### Job states

```text
queued -> preparing -> downloading -> verifying -> finalizing -> completed_verified
            (waiting_for_slot events while the machine is busy)
terminal: completed_verified | failed_download | failed_finalization | outcome_unknown
```

`outcome_unknown` means the truth cannot be proven (for example the host was killed mid-transfer); it
is never retried automatically. Inspect the destination and old processes, then use a new key.

### Quality and formats

* Quality caps every fallback of the format selection and is verified on the result: a rendition
  taller than the cap fails instead of being delivered.
* `mp4` prefers MP4 video with M4A audio. On YouTube today that MP4 video is often **AV1**, which Windows
  plays with the AV1 Video Extension (built into current Windows 11) and every modern browser.
* `mkv` and `webm` take the best streams under the cap; WebM needs VP9/AV1 + Opus/Vorbis, and an
  impossible combination fails explicitly - a file is never just renamed.
* `audio` extracts the best audio and converts it to the requested format with FFmpeg.
* For videos with automatic dubs, yt-dlp's selection keeps the **original-language** track.

## Built for the machine it runs on

At startup the server asks the machine three questions - logical processors, physical memory, and
what kind of volume the default download folder is on (NVMe, SATA SSD, mechanical disk, removable,
network), read with `IOCTL_STORAGE_QUERY_PROPERTY` on a zero-access handle (no administrator rights).
Each job re-asks for its own destination volume. The rules (ported from Angela's reference in
Lumen-Book-Reader):

| Measurement | Simultaneous downloads | Fragments per download | yt-dlp/FFmpeg priority |
| --- | --- | --- | --- |
| Mechanical disk or removable media | 1 | 1 (one head serves one stream) | by processors |
| 4 or fewer logical processors | 1 | 2 | BelowNormal |
| Less than 8 GiB of memory | 1 | 2 | by processors |
| 8 or fewer logical processors | by processors and memory | 4 | BelowNormal (the desktop keeps a core) |
| Larger machines | one per 4 processors and per 4 GiB, at most 4 | 4 (the original's fixed value, now a ceiling) | Normal |

Unknown storage is not treated as the worst case: the processor and memory rules still apply.
Priority is never raised above Normal. The decision and the measurement are reported by
`stream_capabilities` and in every job's `yt_dlp_started` event, and logged at startup, e.g.
*"3 downloads at a time for 22 logical processors, 15.5 GiB RAM, NVMe solid-state ..."*.
`ConcurrentFragments`, `MaxConcurrentDownloads` and `ChildProcessPriority` override the automatic
choice and are **obeyed exactly**.

## Configuration

Edit `artifacts\local\appsettings.json` (section `StreamDownloader`) and restart the host; settings are
not live-reloaded. Environment variables override JSON, e.g. `StreamDownloader__ConcurrentFragments=2`.
Path and runtime strings expand `%VARIABLES%`.

| Key | Default | Effect |
| --- | --- | --- |
| `StateDirectory` | `%LOCALAPPDATA%\Stream-Downloader\state` | `jobs.json`, its `.bak`, and the single-owner `owner.lock` |
| `DefaultDownloadDirectory` | `%USERPROFILE%\Downloads` | Destination when omitted; first allowed root; the volume probed at startup |
| `AdditionalAllowedRoots` | project `downloads`, `C:\Videos`, `C:\Downloads` (JSON) | Only enforced with `RestrictToAllowedRoots` |
| `RestrictToAllowedRoots` | `false` | Confine destinations to the default directory plus additional roots |
| `MaximumSingleFileBytes` | `107374182400` (100 GiB) | yt-dlp `--max-filesize` and the output ceiling |
| `BlockPrivateNetworkTargets` | `true` | Refuse URLs that resolve to private, loopback, link-local or special addresses |
| `AutoAcquireYtDlp` | `true` | Download the official yt-dlp on first use after verifying its SHA-256; never silently updates |
| `YtDlpPath` | empty | Use this yt-dlp; otherwise the tools directory, then PATH, then acquisition |
| `FfmpegPath` | empty | Use this FFmpeg; otherwise PATH and common install folders. ffprobe must sit beside it or on PATH |
| `JavaScriptRuntime` | `auto` | Installed Deno, then Node; an explicit yt-dlp spec (`node:C:\...\node.exe`); `none` for yt-dlp's default |
| `YtDlpDownloadUrl` | GitHub latest `yt-dlp.exe` | Acquisition source |
| `YtDlpChecksumsUrl` | GitHub latest `SHA2-256SUMS` | Must list exactly one matching `yt-dlp.exe` entry |
| `ToolsDirectory` | `%LOCALAPPDATA%\Stream-Downloader\tools` | Where the acquired yt-dlp lives |
| `DefaultQuality` | `best` | Used when a request's quality is empty |
| `SiteDownloadTimeoutSeconds` | `3600` | Per-download yt-dlp time budget (minimum 60) |
| `ConcurrentFragments` | `0` (auto) | Fragments per download; any positive value is obeyed exactly |
| `MaxConcurrentDownloads` | `0` (auto) | Simultaneous downloads; any positive value is obeyed exactly |
| `ChildProcessPriority` | `auto` | `normal` or `below_normal` obeyed exactly; anything else means auto; never above Normal |

## Files on disk

| Path | What |
| --- | --- |
| `%LOCALAPPDATA%\Stream-Downloader\state\jobs.json` | Job history; source URLs are DPAPI-protected for the current Windows user |
| `%LOCALAPPDATA%\Stream-Downloader\state\owner.lock` | Held while a host runs: only one host may own a state folder |
| `%LOCALAPPDATA%\Stream-Downloader\tools\yt-dlp.exe` | Stream-Downloader's own yt-dlp |
| `<destination>\.stream-downloader-staging\<jobId>\` | Per-job staging on the destination volume; removed when empty, kept for failed jobs |

Keep the host open while jobs run; a clean shutdown stops and records its children
(`failed_download` / `host_stopped`). Never delete `jobs.json` to get past an error: a malformed or
incomplete state file stops startup on purpose so history is not lost.

## Tests - visible, one command

```powershell
Start-Process pwsh -ArgumentList '-NoExit','-File','C:\Development\XAIHT\Stream-Downloader\scripts\Test-Local.ps1'
```

Every phase runs in that one **visible** window; nothing is headless or hidden. Each test ends with a
**full-desktop photo taken by Tlamatini's Shoter**, after Tlamatini's **Windower** brought the window
under test forward and pinned it; the harness then confirms the window really is in front and refuses
to count any photo it cannot confirm. Browser checks are driven, headed in the real Chrome, by
Tlamatini's **Playwrighter**. Because this desktop's taskbar is set to auto-hide, the time is shown in
the photographed window's title and on the browser evidence page.

| Phase | What it proves |
| --- | --- |
| `build` | Locked restore, self-contained publish, hash manifest |
| `unit` | 81 C# checks: paths, network policy, format selectors, progress parsing, state store, DPAPI, checksum, process isolation, real Windows priority inheritance, and **injected hardware** (4-core laptop with HDD, low memory, card reader, unknown disk, workstation) |
| `decoupling` | No source/config/test file names the original; the executable carries no trace of its COM interop; the live server uses only its own folders and tools |
| `comparison` | COMPARISON.md holds every line of both projects exactly once, verbatim, and is current; Chrome renders it |
| `local` | 19 MCP integration tests on generated fixtures: every container, idempotency, caps, naming, collisions, input validation, encrypted state, download slots, resource plan, restart recovery, fault injection, retry, missing output, single owner, private-network default, graceful stop, a **real** unclean crash, incomplete state |
| `tlamatini` | `Stream-Downloader.json` loaded by Tlamatini's own External-MCP client, with a real download through it |
| `myvideos` | Angela's own videos from [tests/my_videos.json](./tests/my_videos.json): downloaded together (slots exercised), each checked against an independent yt-dlp oracle (streams, height, original-language audio, duration), SHA-256 recomputed, every frame decoded by FFmpeg, then **played in Chrome** and photographed |

Run part of it with `-Only local,myvideos`, or skip the network phase with `-SkipMyVideos`.
Evidence is kept under `test-artifacts\suite-*\` (`suite.log`, each phase's `live.log`,
`results.json`, `photos\`). Exit codes are named so a red run says which edge broke:

| Code | Meaning |
| --: | --- |
| 0 | Passed, with confirmed photos |
| 1 | A check failed |
| 3 | Every check passed, but a photo is missing or could not be confirmed |
| 10-21 | Preflight: executable missing (10), spec invalid (11), FFmpeg (12), ffprobe (13), yt-dlp (14), JavaScript runtime (15), network (16), Tlamatini agents (17), Chrome (18), original source for the comparison (19), host refused to start (20), a command-line path outside `test-artifacts` / `tests` (21) |

Every `--run`, `--dir`, `--name` and `--config` a test receives is confined by `tests/harness/paths.py`:
run folders must resolve - links and junctions followed - inside `test-artifacts`, file names must be
bare names, and the videos list must live inside `tests`.

Tests never use `taskkill /T` (it can kill the test's own console): every test host runs inside a
Windows Job Object, and only that job is ever terminated.

## The comparison with IDM-MCP Core

```powershell
python tools\generate_comparison.py          # rewrite COMPARISON.md and COMPARISON.html
python tools\generate_comparison.py --check  # exit 1 if either is stale
```

The generator reads `C:\Development\IDM` (or `--idm` / `IDM_ROOT`) without modifying it, aligns every
file with its counterpart, and refuses to run if any file of either project is unaccounted for.
Regenerate after every change to this repository.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| `stream_capabilities` says `not_ready` | FFmpeg not found, or yt-dlp missing with `AutoAcquireYtDlp=false`. Install FFmpeg or set `FfmpegPath`. |
| `tool_unavailable` | First-use acquisition failed (network, checksum) or ffprobe is missing; read `failureMessage`. A checksum failure never executes the binary. |
| `yt_dlp_failed` mentioning JavaScript or signatures | Install Node.js or Deno, or set `JavaScriptRuntime`. |
| Job stays `queued` with `waiting_for_slot` | The machine-sized slots are busy; see `stream_capabilities.machine`. Raise `MaxConcurrentDownloads` to override. |
| Host exits: *another host owns this state directory* | Another Stream-Downloader host is running on that state folder (e.g. Tlamatini and Claude Code both started it). Close one, or give one a different `StateDirectory`. |
| Host exits: *jobs.json is incomplete / inconsistent* | Keep the file and its `.bak`; restore a verified copy deliberately. Never reset it to `{}`. |
| `outcome_unknown` | Inspect the destination and any old yt-dlp/FFmpeg process before downloading again with a new key. |
| `completed_output_changed` / `completed_file_missing` | The verified file was moved or edited after completion; history is kept. Use a new key to download again. |
| `idempotency_key_conflict` | That key belongs to a different request; choose a new key. |
| Destination refused | Use an absolute local path, a valid Windows filename, no UNC path or reparse point. |

## Limits (honest)

* yt-dlp performs the transfers; site changes can break extraction until yt-dlp is updated
  (replace `tools\yt-dlp.exe` deliberately - it is never silently updated).
* No cookies, logins, playlists, subtitles, cancellation or pause tools. DRM is not circumvented.
* Quality is a ceiling; codec, frame rate and HDR are whatever the best stream under the cap is.
* Audio formats other than the source's are produced by conversion, not copied.
* Private-address checks cover the requested URL, not every request yt-dlp makes; path checks do not
  defeat a hostile same-user process. This is a trusted, single-user local tool.
* Whole-file JSON state suits a personal tool, not a high-volume service.

## Author

Stream-Downloader, its design and IDM-MCP Core are by **Angela López Mendoza**.
