# Stream-Downloader 1.0.0 - implemented design

This document describes the code built into `artifacts\local\StreamDownloader.McpHost.exe`. Use the
[README](./README.md) for setup and configuration, [CLAUDE.md](./CLAUDE.md) for contributor rules and
[COMPARISON.md](./COMPARISON.md) for the line-by-line relation to the original. Author: Angela López Mendoza.

## 1. Product contract and scope

A private, single-user Windows x64 MCP server that accepts an HTTP/HTTPS URL, creates a persisted
job, lets yt-dlp transfer the media into per-job staging, verifies it with ffprobe, records its
SHA-256 and moves it - without ever overwriting - to a local destination.

| Input | Path | Boundary |
| --- | --- | --- |
| Watch page (YouTube and every site yt-dlp supports) | yt-dlp + FFmpeg + ffprobe | yt-dlp's extractors decide what a site offers |
| Direct media URL (e.g. `https://host/clip.mp4`) | yt-dlp's generic extractor | Verified like any other media; non-media files fail verification |

An accepted job is not a successful download. Clients read `ok`, `code`, `state`, `terminal` and the
failure fields, and report completion only from `completed_verified`. That state records what was
verified at finalization; it does not promise the file cannot change later (status re-checks
existence and size).

### Relation to the original

The original (IDM-MCP Core 0.3.1) routed **direct files to Internet Download Manager through COM**
and **watch pages to yt-dlp**. Stream-Downloader keeps the second half - the video downloader - with
the same code, and removes the first: no IDM executable, no `IDMan.Interop` COM interop, no
inspect/plan/start tools, no staged-file monitor, no browser-panel handoff, no HTTP probe deciding
between the two engines. It shares nothing at run time with the original: separate configuration
section, state, tools, staging folder, DPAPI entropy, progress markers, server identity and tool
names. What it adds is mandatory on Angela's machines: hardware-aware sizing (section 6).

## 2. Runtime architecture

```text
MCP client (Tlamatini External MCP, Claude Code, ...)
    |
    | STDIO: six tools; diagnostics on STDERR only
    v
StreamDownloader.McpHost.exe   (one owner per state directory)
    |
    +-- StreamCoreService: requests, idempotency, download slots, workers, recovery, lifecycle
    |     +-- NetworkInspector + PathPolicy : URL and destination policy
    |     +-- StateStore + SecretVault      : transactional JSON + DPAPI-protected URLs
    |     +-- MachineProfile                : processors, memory, seek penalty -> sizing decisions
    |     +-- YtDlpService ---------------> yt-dlp ---> FFmpeg      (child processes, own stdin)
    |                          \-----------> ffprobe                 (verification)
    |
    +-- <destination>\.stream-downloader-staging\<jobId>\
          --> ffprobe --> SHA-256 + move intent persisted --> final file (no overwrite)
```

.NET 10 self-contained single-file executable. `Program.cs` registers `StreamCoreService` as a
singleton hosted service before the MCP STDIO transport, so startup recovery completes before any
request is accepted. There is no Windows service, HTTP endpoint, named pipe, database or broker.

## 3. Public MCP interface

Server `Stream-Downloader`, version `1.0.0`. Arguments are camelCase. Every response is the envelope
`ok`, `code`, `message`, `data`, `serverTime`; transport success alone is not application success.

| Tool | Arguments (defaults) | Result |
| --- | --- | --- |
| `stream_capabilities` | none | Tool readiness, quality presets, roots, state folder, machine profile and decisions; no spawn, no download |
| `stream_download` | `url`; `destination=null`, `fileName=null`, `quality="best"`, `format=null`, `idempotencyKey=null` | Creates or replays a job; returns once committed |
| `stream_download_status` | `jobId` | Job view; detects a vanished/resized completed file (`completed_output_changed`) |
| `stream_download_list` | `stateFilter=null`, `limit=25` (1-100) | Jobs, newest first, and the total |
| `stream_download_wait` | `jobId`, `afterVersion`; `timeoutSeconds=30` (1-45) | Returns at the next state change, at a terminal state, or `unchanged` |
| `stream_download_retry` | `jobId`, `newIdempotencyKey` | A new job for `failed_download` / `failed_finalization` only |

Job view: `jobId`, `state`, `terminal`, `destination`, `stagingDirectory` (while running),
`sourceHost`, `sourceTitle`, `quality`, `container`, `observedBytes`, `streamPercent`,
`observedSha256`, `failureCode`, `failureMessage`, `createdAt`, `updatedAt`, `stateVersion`,
`events` (last 50) and `media` (`container`, `height`, `hasAudio`, `durationSeconds`).

### 3.1 Destinations

Absolute local paths only; UNC paths, reparse points, alternate data streams, reserved device names,
control characters, trailing dots/spaces and names over 180 characters are refused. An existing
directory or a trailing separator means a directory; otherwise a last segment with an extension is an
exact filename. The extension of the destination filename or of `fileName` sets the container. Output
without a name is named `Title [id].ext` by yt-dlp's Windows-safe naming, falling back to
`video_<jobId>.ext`. A name taken at finalization becomes `Name (1).ext`.

### 3.2 Quality and formats

| Request | Selector / processing |
| --- | --- |
| `best`, MP4 | `bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b`, merged/remuxed to MP4 |
| `<N>p`, MP4 | `bv*[height<=?N][ext=mp4]+ba[ext=m4a]/bv*[height<=?N]+ba/b[height<=?N]` |
| `best` / `<N>p`, MKV or WebM | `bv*[..]+ba/b[..]`, merged/remuxed to the container |
| `audio` | `bestaudio/best`, extracted and converted to mp3, m4a, flac, wav, opus or aac |

The cap filters every fallback and ffprobe re-checks the result: taller output fails
(`Output height ... exceeds requested maximum`). The container is verified by ffprobe's demuxer and
the file extension; an impossible pair (e.g. H.264 in WebM) fails instead of being renamed.

## 4. The download pipeline

1. **Request** (under one request gate): normalize quality/format, resolve the destination, derive
   the request fingerprint `SHA-256(url, DIRECTORY, FILENAME, quality, container)`; replay or refuse
   an existing key; validate the URL (HTTP/HTTPS, no credentials, at most 8192 characters, no
   private/loopback/special address when `BlockPrivateNetworkTargets`); require 512 MiB free;
   create per-job staging; protect the URL with DPAPI; persist the job (`queued`); start its worker.
2. **Slot**: wait for one of the machine-sized download slots (`waiting_for_slot` event).
3. **Prepare**: locate yt-dlp (configured path, own tools folder, PATH) or acquire it: download the
   official release, verify exactly one matching `SHA2-256SUMS` entry before execution, run
   `--version`; a binary that does not run is quarantined as `.rejected-<GUID>`. Locate FFmpeg,
   ffprobe and the JavaScript runtime.
4. **Download**: plan fragments and priority for the job's volume (announced in the
   `yt_dlp_started` event); run yt-dlp with user config and plugins disabled, no playlists,
   Windows-safe names, retries, `--max-filesize`, the selector, `--ffmpeg-location`,
   `--js-runtimes`, a progress template (recorded as `observedBytes`/`streamPercent`, at most once a
   second) and an `after_move` print of the exact output path.
5. **Verify**: the reported file must exist inside this job's staging, be non-empty and within the
   size ceiling, and pass ffprobe (container, streams, positive duration, height cap).
6. **Finalize**: choose the final name, take a non-overwriting suffix if needed, hash the staged file
   while denying writes, persist `finalizing` with name, size, SHA-256 and source, move without
   overwrite, commit `completed_verified`, remove empty staging.

Child processes are started from argument lists (never shell strings), with STDIN redirected and
closed immediately. Output-handler exceptions fail the job instead of the host. Timeout
(`max(60, SiteDownloadTimeoutSeconds)`) and cancellation kill and await the whole child tree.
Extractor diagnostics have URLs redacted.

## 5. State, idempotency and recovery

```text
queued -> preparing -> downloading -> verifying -> finalizing -> completed_verified
terminal: completed_verified | failed_download | failed_finalization | outcome_unknown
```

`jobs.json` (schema 1: `schemaVersion`, `jobs`, `idempotency`) is owned exclusively through
`owner.lock` for the host's lifetime. Reads return isolated clones; each mutation clones, writes and
flushes a temporary file, atomically replaces the state (keeping `jobs.json.bak`) and only then
publishes the new copy. Startup refuses malformed, incomplete, unsupported or inconsistent state.

* Same key + same request: the original job is returned, even after completion. If that completed
  file is gone: `completed_file_missing` - history is kept, a new key downloads again.
* Same key + different request: `idempotency_key_conflict`.
* Clean shutdown: running children are killed and awaited; their jobs end `failed_download` /
  `host_stopped` and may be retried with a new key.
* Unclean restart: `finalizing` jobs are completed or rejected from their saved size and hash (both
  sides of the move are handled; a mismatch becomes `outcome_unknown` / `recovery_failed` and the
  file is left untouched); every other non-final job becomes `outcome_unknown` /
  `interrupted_by_restart`, because an old child may still be running. Unknown outcomes are never
  retried automatically.

## 6. Hardware-aware sizing

`MachineProfile` answers four questions, each degrading to a safe "unknown" instead of throwing:
logical processors (`Environment.ProcessorCount`, which honours affinity), physical memory
(`GlobalMemoryStatusEx`), the volume type (`IOCTL_STORAGE_QUERY_PROPERTY` for bus type and seek
penalty on a zero-access handle - no administrator rights) and whether the drive is removable or
remote. Answers are cached per volume.

| Decision | Rule |
| --- | --- |
| Simultaneous downloads (global slots, at startup, from the default download volume) | explicit value; else 1 when seek-bound, at most 4 processors, or under 8 GiB; else `min(4, max(2, processors/4), memory/4 GiB)` |
| Concurrent fragments (per job, from its own volume) | explicit value; else 1 when seek-bound; 2 when at most 4 processors or under 8 GiB; else 4 |
| Child priority (yt-dlp, inherited by FFmpeg) | explicit `normal`/`below_normal`; else BelowNormal at 8 or fewer processors, Normal above; never higher |

Seek-bound means a mechanical disk or removable media. Unknown storage is not treated as the worst
case. Every decision is reported with its measurement (`stream_capabilities.machine`, the
`yt_dlp_started` event, the startup log). The thresholds follow Angela's reference implementation
in Lumen-Book-Reader.

## 7. Verification and safeguards

| Area | Evidence / control | Does not establish |
| --- | --- | --- |
| Output | Exact reported path, size ceiling, ffprobe container/streams/height/duration, SHA-256 | Every frame decoded, origin hash (the visible tests add a full decode and Chrome playback) |
| Paths | Absolute local paths, Windows names, ADS/UNC/reparse checks, no-overwrite move | Isolation from a hostile same-user process |
| URL policy | HTTP/HTTPS, no credentials, length bound, initial-host address check | Control of every request yt-dlp makes |
| Disk | 512 MiB preflight floor, `--max-filesize`, output ceiling | A global reservation for merge scratch space |
| Tools | Checksum before a new yt-dlp runs; no silent updates | Signature attestation of yt-dlp releases |
| Lifecycle | One state owner, persist-before-act, move recovery, clean child stop | A service that survives the host being closed |

## 8. Build, tests, evidence

`global.json` pins SDK 10.0.401 (latest patch). Locked packages: Microsoft.Extensions.Hosting
10.0.0, ModelContextProtocol 2.2.0, System.Security.Cryptography.ProtectedData 10.0.0 - the same
graph as the original. `scripts/Build-Local.ps1` publishes the self-contained executable and a hash
manifest; `scripts/Test-Local.ps1` runs the visible suite (README, "Tests"). Every test photo is a
confirmed full-desktop Shoter photo; every browser check is a headed Playwrighter run in Chrome.

## 9. Not implemented

Cookies and logins, playlists, subtitles, cancellation/pause, exact codec/frame-rate/HDR selection,
DRM circumvention, a persistent multi-client service, and confinement of every network request made
by yt-dlp. These would need their own decisions and tests.
