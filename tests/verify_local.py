"""Isolated local integration suite for Stream-Downloader, driven over real MCP STDIO.

It carries over the original suite's site-extraction coverage (its direct-file checks have no
counterpart in a video-only mirror) and adds what the mirror introduced: hardware-sized download
slots and child priority, a REAL unclean crash in the middle of a transfer, and first-use
acquisition of Stream-Downloader's own checksum-verified yt-dlp.

It creates its own fixtures, loopback HTTP listener, state and output under a unique run
directory, never touches Angela's real job history, downloads or cookies, and contacts no public
video site (only GitHub, once, if yt-dlp must be acquired). Every check prints live in this
window; every test ends with a full-desktop photo taken by Tlamatini's Shoter.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from harness import paths, preflight
from harness.console import Console, human_bytes
from harness.fixtures import FixtureServer, make_media
from harness.mcp_client import (DEFAULT_EXECUTABLE, EXPECTED_TOOLS, ROOT, TOOLS_DIRECTORY, HostRefused,
                                McpClient, descendants_of, sha256_file)
from harness.tlamatini import Tlamatini, TlamatiniMissing


class Context:
    def __init__(self, run: Path, executable: Path, console: Console, tlamatini: Tlamatini | None):
        self.run = run
        self.out = run / "output"
        self.executable = executable
        self.console = console
        self.tlamatini = tlamatini
        self.results: list[dict] = []
        self.tests: list[dict] = []
        self.jobs: dict[str, dict] = {}
        self.server: FixtureServer | None = None
        self.media: dict = {}
        self.ffmpeg = ""
        self.ffprobe = ""
        self.payload = b""

    # ---- recording ----
    def check(self, name: str, condition, detail=None) -> None:
        if not condition:
            raise AssertionError(f"{name}: {detail}")
        self.results.append(dict(name=name, passed=True, detail=None if detail is None else str(detail)[:600]))
        self.console.ok(name)

    @contextmanager
    def host(self, **overrides):
        client = McpClient(self.run, self.executable, **overrides)
        try:
            yield client
        finally:
            client.close()

    @property
    def state_path(self) -> Path:
        return self.run / "state" / "jobs.json"

    def load_state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def need(self, key: str) -> dict:
        if key not in self.jobs:
            raise AssertionError(f"prerequisite job '{key}' was not produced by an earlier test")
        return self.jobs[key]

    # ---- independent evidence ----
    def verify_output(self, job: dict) -> Path:
        path = Path(job["destination"])
        assert path.is_file(), f"missing output {path}"
        assert path.stat().st_size == job["observedBytes"], (path.stat().st_size, job["observedBytes"])
        assert sha256_file(path) == job["observedSha256"], "SHA-256 differs from the job record"
        return path

    def probe(self, path: Path) -> dict:
        completed = subprocess.run([self.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=True)
        data = json.loads(completed.stdout)
        streams = data.get("streams", [])
        video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        return dict(format=data["format"]["format_name"], duration=float(data["format"].get("duration") or 0),
                    height=max((int(s.get("height") or 0) for s in video), default=None),
                    video=[s.get("codec_name") for s in video], audio=[s.get("codec_name") for s in audio])

    def decodes(self, path: Path) -> tuple[bool, str]:
        completed = subprocess.run([self.ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        return completed.returncode == 0 and not completed.stderr.strip(), completed.stderr.strip()[:300]


def ok(response: dict) -> dict:
    assert response["ok"], response
    return response["data"]


def report_photo(console: Console, evidence: dict | None) -> None:
    if not evidence:
        console.warn("photo missing: Tlamatini's agents are unavailable")
    elif evidence["verified"]:
        console.info(f"photo (Shoter; Windower brought this window forward, confirmed in front): {evidence['path']}")
    elif evidence["path"]:
        console.warn(f"photo NOT counted as evidence - the window in front was '{evidence['foreground']}': {evidence['path']}")
    else:
        console.warn(f"photo missing: Shoter left no image ({evidence.get('error', 'see the Shoter log')})")


# =================================================================================== tests
def test_first_use_acquisition(ctx: Context) -> None:
    target = TOOLS_DIRECTORY / "yt-dlp.exe"
    if target.is_file():
        ctx.console.info(f"Stream-Downloader already owns its yt-dlp: {target} ({human_bytes(target.stat().st_size)}).")
        ctx.check("Stream-Downloader's own yt-dlp is present in its own tools directory", True, target)
        return
    ctx.console.step("No yt-dlp in Stream-Downloader's own tools directory: the server acquires the official release now; "
                     "its SHA-256 is checked before it is ever executed.")
    with ctx.host(AutoAcquireYtDlp="true") as client:
        job = ok(client.call("stream_download", url=ctx.server.base + "/index.html", destination=str(ctx.out / "acquisition.mp4"),
                             quality="360p", idempotencyKey="first-use-acquisition"))
        finished = client.wait_for(job, ctx.console, timeout=900)
    ctx.check("first use acquired, verified and ran the official yt-dlp", target.is_file() and finished["state"] == "completed_verified")
    request = urllib.request.Request("https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS",
                                     headers={"User-Agent": "Stream-Downloader-tests"})
    sums = urllib.request.urlopen(request, timeout=60).read().decode("utf-8", "replace")
    expected = [parts[0] for parts in (line.split() for line in sums.splitlines()) if parts[1:] == ["yt-dlp.exe"]]
    ctx.check("acquired binary matches the official SHA2-256SUMS (checked independently by the test)",
              len(expected) == 1 and sha256_file(target).lower() == expected[0].lower(), expected)


def test_protocol_and_capabilities(ctx: Context) -> None:
    with ctx.host() as client:
        tools = client.request("tools/list", {})["tools"]
        names = sorted(tool["name"] for tool in tools)
        ctx.check("MCP handshake lists exactly the six Stream-Downloader tools", names == sorted(EXPECTED_TOOLS), names)
        ctx.check("every tool publishes a description and an input schema",
                  all(tool.get("description") and tool.get("inputSchema") for tool in tools))
        caps = ok(client.call("stream_capabilities"))
        site = caps["siteExtraction"]
        ctx.check("capabilities ready: yt-dlp and FFmpeg found", site["ytDlpAvailable"] and site["ffmpegAvailable"], site)
        ctx.check("server identity and version agree with the handshake",
                  client.initialize["serverInfo"]["name"] == caps["serverName"] == "Stream-Downloader"
                  and client.initialize["serverInfo"]["version"] == caps["serverVersion"], caps["serverVersion"])
        ctx.check("capabilities tool list matches tools/list", sorted(caps["tools"]) == names)
        yt_dlp = Path(site["ytDlpPath"])
        ctx.check("yt-dlp is Stream-Downloader's own copy (or one on PATH)",
                  yt_dlp.parent == TOOLS_DIRECTORY or shutil.which("yt-dlp") == str(yt_dlp), yt_dlp)
        ctx.check("job state is isolated inside this run", Path(caps["stateDirectory"]).resolve() == (ctx.run / "state").resolve())
        machine = caps["machine"]
        ctx.check("this machine was measured and the decision explained",
                  machine["logicalProcessors"] >= 1 and machine["maxConcurrentDownloads"] >= 1
                  and machine["concurrentFragments"] >= 1 and "logical processors" in machine["summary"]
                  and len(machine["reason"]) > 40, machine)
        ctx.console.info("machine : " + machine["summary"])
        ctx.console.info("decided : " + machine["reason"])
        ctx.console.info("js      : " + str(site.get("javaScriptRuntime")))


VIDEO = dict(mp4=("mp4", "h264", "aac"), mkv=("matroska", "h264", "aac"), webm=("webm", "vp9", "opus"))
AUDIO = dict(mp3=("mp3", "mp3"), m4a=("mp4", "aac"), opus=("ogg", "opus"))


def test_site_formats(ctx: Context) -> None:
    cases = [("mp4", "360p", "index.html"), ("mkv", "360p", "index.html"), ("webm", "360p", "webm.html"),
             ("mp3", "audio", "index.html"), ("m4a", "audio", "index.html"), ("opus", "audio", "index.html")]
    with ctx.host() as client:
        for fmt, quality, page in cases:
            ctx.console.step(f"{fmt} at {quality} from {page}")
            destination = ctx.out / f"Video Unicode ñ ' {fmt}.{fmt}"
            request = dict(url=f"{ctx.server.base}/{page}", destination=str(destination), fileName="ignored-name.bin",
                           format="mp4" if fmt == "webm" else "webm", quality=quality, idempotencyKey="site-test-" + fmt)
            site = ok(client.call("stream_download", **request))
            repeat = ok(client.call("stream_download", **request))
            ctx.check(f"{fmt}: an identical call replays the same job", repeat["jobId"] == site["jobId"])
            finished = client.wait_for(site, ctx.console)
            path = ctx.verify_output(finished)
            ctx.check(f"{fmt}: exact requested filename; size and SHA-256 recomputed by the test match",
                      str(path) == str(destination), path)
            facts = ctx.probe(path)
            if fmt in VIDEO:
                demuxer, video_codec, audio_codec = VIDEO[fmt]
                ctx.check(f"{fmt}: independent ffprobe sees {demuxer}, {video_codec} 180p video and {audio_codec} audio",
                          demuxer in facts["format"].split(",") and facts["video"] == [video_codec]
                          and facts["audio"] == [audio_codec] and facts["height"] == 180 == finished["media"]["height"], facts)
            else:
                demuxer, audio_codec = AUDIO[fmt]
                ctx.check(f"{fmt}: independent ffprobe sees {demuxer} with {audio_codec} audio and no video",
                          demuxer in facts["format"].split(",") and facts["audio"] == [audio_codec] and not facts["video"]
                          and finished["media"]["hasAudio"], facts)
            ctx.check(f"{fmt}: duration about 2 s (server and test agree)",
                      1.5 < facts["duration"] < 2.6 and abs(facts["duration"] - finished["media"]["durationSeconds"]) < 0.2, facts["duration"])
            decoded, errors = ctx.decodes(path)
            ctx.check(f"{fmt}: every frame decodes without error (full FFmpeg decode)", decoded, errors)
            changed = dict(request, quality="best") if quality != "audio" else dict(request, url=f"{ctx.server.base}/other.html")
            conflict = client.call("stream_download", **changed)
            ctx.check(f"{fmt}: same key with a different request is refused", not conflict["ok"] and conflict["code"] == "idempotency_key_conflict")
            ctx.jobs[fmt] = finished


def test_unsupported_combination(ctx: Context) -> None:
    with ctx.host() as client:
        job = ok(client.call("stream_download", url=ctx.server.base + "/index.html", destination=str(ctx.out / "h264-into.webm"),
                             quality="360p", idempotencyKey="unsupported-combination"))
        failed = client.wait_for(job, ctx.console, expected="failed_download")
    ctx.check("H.264/AAC cannot be relabelled as WebM: the job fails explicitly", failed["failureCode"] == "yt_dlp_failed", failed["failureMessage"])
    ctx.check("no mislabelled output was finalized", not (ctx.out / "h264-into.webm").exists())


def test_quality_cap(ctx: Context) -> None:
    with ctx.host() as client:
        job = ok(client.call("stream_download", url=ctx.server.base + "/index.html", destination=str(ctx.out / "cap.mp4"), quality="144p"))
        failed = client.wait_for(job, ctx.console, expected="failed_download")
    ctx.check("a 180p source under a 144p cap is rejected by verification", "exceeds requested maximum 144" in (failed["failureMessage"] or ""), failed["failureMessage"])
    ctx.check("the over-cap rendition was never finalized", not (ctx.out / "cap.mp4").exists())


def test_destination_semantics(ctx: Context) -> None:
    index = ctx.server.base + "/index.html"
    with ctx.host() as client:
        named = client.wait_for(ok(client.call("stream_download", url=index, destination=str(ctx.out / "named-dir"),
                                                fileName="custom name", quality="360p")), ctx.console)
        ctx.check("directory + extensionless fileName gains the produced extension",
                  Path(named["destination"]) == ctx.out / "named-dir" / "custom name.mp4" and ctx.verify_output(named))
        auto = client.wait_for(ok(client.call("stream_download", url=index, destination=str(ctx.out / "auto-named"), quality="360p")), ctx.console)
        ctx.check("an unnamed download is named from the page title",
                  Path(auto["destination"]).parent == ctx.out / "auto-named" and auto["sourceTitle"].startswith("Local verification")
                  and Path(auto["destination"]).suffix == ".mp4", auto["destination"])
        dotted = client.wait_for(ok(client.call("stream_download", url=index, destination=str(ctx.out / "v1.2 folder") + "\\",
                                                 fileName="dotted", quality="360p")), ctx.console)
        ctx.check("a trailing separator makes a dotted name a directory",
                  Path(dotted["destination"]) == ctx.out / "v1.2 folder" / "dotted.mp4")
        exact = ctx.out / "collision.mp4"
        first = client.wait_for(ok(client.call("stream_download", url=index, destination=str(exact), quality="360p",
                                                idempotencyKey="collision-first")), ctx.console)
        before = sha256_file(exact)
        second = client.wait_for(ok(client.call("stream_download", url=index, destination=str(exact), quality="360p",
                                                 idempotencyKey="collision-second")), ctx.console)
        ctx.check("an existing file is never overwritten: the newcomer is suffixed",
                  Path(second["destination"]) == ctx.out / "collision (1).mp4" and sha256_file(exact) == before
                  and Path(first["destination"]) == exact, second["destination"])
    # Failed jobs deliberately keep their partial data; every COMPLETED job must leave no staging behind.
    state = ctx.load_state()
    leftovers = [state["jobs"][job["jobId"]]["stagingDirectory"] for job in (named, auto, dotted, first, second)
                 if Path(state["jobs"][job["jobId"]]["stagingDirectory"]).exists()]
    ctx.check("each completed job removed its own staging folder", not leftovers, leftovers)


def test_input_validation(ctx: Context) -> None:
    index = ctx.server.base + "/index.html"
    with ctx.host() as client:
        def refused(name: str, code: str | None = None, **arguments) -> None:
            response = client.call("stream_download", **arguments)
            ctx.check(name, not response["ok"] and (code is None or response["code"] == code), response.get("message"))
        refused("an unknown quality is refused", url=index, quality="999p")
        refused("a Windows device filename is refused", url=index, destination=str(ctx.out / "CON.foo.mp4"))
        refused("a non-HTTP source is refused", url="file:///C:/Windows/win.ini", destination=str(ctx.out / "invalid.mp4"))
        refused("a URL with embedded credentials is refused", url="https://user:secret@example.com/watch", destination=str(ctx.out / "c.mp4"))
        refused("a relative destination is refused", url=index, destination="relative\\folder")
        refused("a UNC destination is refused", url=index, destination=r"\\server\share\video.mp4")
        refused("an idempotency key shorter than 8 characters is refused", "invalid_idempotency_key", url=index, idempotencyKey="short")
        refused("an audio container that does not exist is refused", url=index, quality="audio", format="exe")
        listing = ok(client.call("stream_download_list", limit=1))
        ctx.check("list honours its limit and reports the total", len(listing["jobs"]) == 1 and listing["total"] >= 6, listing["total"])
        ctx.check("list can filter by state", all(job["state"] == "completed_verified"
                                                   for job in ok(client.call("stream_download_list", stateFilter="completed_verified", limit=100))["jobs"]))
        for tool, arguments in (("stream_download_wait", dict(afterVersion=0, timeoutSeconds=1)),
                                ("stream_download_status", {}), ("stream_download_retry", dict(newIdempotencyKey="whatever-key"))):
            response = client.call(tool, jobId="job_does_not_exist", **arguments)
            ctx.check(f"{tool} on an unknown job answers job_not_found", not response["ok"] and response["code"] == "job_not_found")
        completed = ctx.need("mp4")
        response = client.call("stream_download_retry", jobId=completed["jobId"], newIdempotencyKey="retry-a-success")
        ctx.check("a completed job cannot be retried", not response["ok"] and response["code"] == "job_not_retryable")


def test_secrets_at_rest(ctx: Context) -> None:
    text = ctx.state_path.read_text(encoding="utf-8")
    state = json.loads(text)
    ctx.check("persisted state never contains a source URL in clear text",
              ctx.server.base not in text and ctx.server.base.removeprefix("http://") not in text)
    ctx.check("every job keeps its URL only in DPAPI-protected form",
              all(job["protectedUrl"] and "://" not in job["protectedUrl"] for job in state["jobs"].values()), len(state["jobs"]))


def test_download_slots(ctx: Context) -> None:
    with ctx.host(MaxConcurrentDownloads="1") as client:
        caps = ok(client.call("stream_capabilities"))
        ctx.check("MaxConcurrentDownloads=1 is obeyed exactly and said so",
                  caps["machine"]["maxConcurrentDownloads"] == 1 and "obeyed exactly" in caps["machine"]["reason"], caps["machine"]["reason"])
        first = ok(client.call("stream_download", url=ctx.server.slow_url("slot-a"), destination=str(ctx.out / "slot-a.mp4"),
                               quality="360p", idempotencyKey="slot-test-a"))
        assert ctx.server.started("slot-a").wait(90), "the first job never reached the server"
        second = ok(client.call("stream_download", url=ctx.server.slow_url("slot-b"), destination=str(ctx.out / "slot-b.mp4"),
                                quality="360p", idempotencyKey="slot-test-b"))
        waiting = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            waiting = ok(client.call("stream_download_status", jobId=second["jobId"]))
            if any(event["code"] == "waiting_for_slot" for event in waiting["events"]):
                break
            time.sleep(0.5)
        ctx.console.job(waiting)
        ctx.check("the second job queues for the slot instead of competing for the machine",
                  waiting["state"] == "queued" and any(e["code"] == "waiting_for_slot" for e in waiting["events"])
                  and not ctx.server.started("slot-b").is_set(), [e["code"] for e in waiting["events"]])
        ctx.server.release("slot-a")
        ctx.server.release("slot-b")
        done_a = client.wait_for(first, ctx.console)
        done_b = client.wait_for(second, ctx.console)
        finished_a = next(e["at"] for e in done_a["events"] if e["state"] == "completed_verified")
        started_b = next(e["at"] for e in done_b["events"] if e["code"] == "yt_dlp_started")
        ctx.check("both complete, strictly one after the other", started_b >= finished_a, (finished_a, started_b))


def test_resource_plan(ctx: Context) -> None:
    index = ctx.server.base + "/index.html"
    with ctx.host(ConcurrentFragments="3", ChildProcessPriority="below_normal") as client:
        done = client.wait_for(ok(client.call("stream_download", url=index, destination=str(ctx.out / "explicit-resources.mp4"),
                                              quality="360p", idempotencyKey="explicit-resources")), ctx.console)
    started = next(e["message"] for e in done["events"] if e["code"] == "yt_dlp_started")
    ctx.console.info(started)
    ctx.check("explicit fragments and priority are obeyed exactly and announced in the job",
              "3 concurrent fragment(s) at BelowNormal priority" in started and started.count("obeyed exactly") == 2, started)
    with ctx.host() as client:
        done = client.wait_for(ok(client.call("stream_download", url=index, destination=str(ctx.out / "auto-resources.mp4"),
                                              quality="360p", idempotencyKey="auto-resources")), ctx.console)
    started = next(e["message"] for e in done["events"] if e["code"] == "yt_dlp_started")
    ctx.console.info(started)
    ctx.check("the automatic plan names the measurement that decided it",
              "concurrent fragment" in started and ("logical processors" in started or "seek penalty" in started or "GiB" in started), started)


def test_restart_recovery(ctx: Context) -> None:
    job = ctx.need("mp4")
    state = ctx.load_state()
    state["jobs"][job["jobId"]]["state"] = "finalizing"
    ctx.save_state(state)
    ctx.console.step("simulated crash AFTER the move but BEFORE completion was committed")
    with ctx.host() as client:
        recovered = client.wait_for(job, ctx.console)
    ctx.check("restart completes a moved file by its saved hash",
              ctx.verify_output(recovered) and any(e["code"] == "finalization_recovered" for e in recovered["events"]))
    state = ctx.load_state()
    pending = state["jobs"][job["jobId"]]
    pending["state"] = "finalizing"
    source = Path(pending["stagingDirectory"]) / pending["fileName"]
    source.parent.mkdir(parents=True, exist_ok=True)
    Path(recovered["destination"]).rename(source)
    pending["finalizingSource"] = str(source)
    ctx.save_state(state)
    ctx.console.step("simulated crash BEFORE the durable move was performed")
    with ctx.host() as client:
        recovered = client.wait_for(job, ctx.console)
    ctx.check("restart performs the saved move and verifies it", ctx.verify_output(recovered) and not source.exists())
    ctx.jobs["mp4"] = recovered


def test_fault_injection(ctx: Context) -> None:
    template = ctx.need("mp4")
    state = ctx.load_state()

    def fault_job(name: str, status: str) -> dict:
        record = copy.deepcopy(state["jobs"][template["jobId"]])
        record.update(jobId="job_fault_" + name, idempotencyKey="fault-key-" + name, state=status, fileName=name + ".mp4",
                      stagingDirectory=str(ctx.out / ".stream-downloader-staging" / ("job_fault_" + name)),
                      requestFingerprint="fault-" + name, observedBytes=None, observedSha256=None,
                      finalizingSource=None, events=[])
        state["jobs"][record["jobId"]] = record
        state["idempotency"][record["idempotencyKey"]] = record["jobId"]
        return record

    interrupted = fault_job("interrupted", "downloading")
    queued = fault_job("queued", "queued")
    corrupt = fault_job("hash-mismatch", "finalizing")
    corrupt_path = ctx.out / corrupt["fileName"]
    corrupt_path.write_bytes(ctx.payload)
    corrupt.update(observedBytes=len(ctx.payload), observedSha256="0" * 64)
    ctx.save_state(state)
    with ctx.host() as client:
        result = client.wait_for(interrupted, ctx.console, expected="outcome_unknown")
        ctx.check("a download interrupted by an unclean restart becomes unknown, never failed or done",
                  result["failureCode"] == "interrupted_by_restart")
        ctx.check("a queued job from the dead host is unknown too",
                  client.wait_for(queued, ctx.console, expected="outcome_unknown")["failureCode"] == "interrupted_by_restart")
        tampered = client.wait_for(corrupt, ctx.console, expected="outcome_unknown")
        ctx.check("tampered finalization evidence can never report success, and the file is left untouched",
                  tampered["failureCode"] == "recovery_failed" and corrupt_path.read_bytes() == ctx.payload)
        refused = client.call("stream_download_retry", jobId=interrupted["jobId"], newIdempotencyKey="must-not-retry-unknown")
        ctx.check("an unknown outcome is never retried automatically", not refused["ok"] and refused["code"] == "job_not_retryable")


def test_retry(ctx: Context) -> None:
    job = ctx.need("mkv")
    moved = Path(job["destination"]).with_name(Path(job["destination"]).name + ".moved-for-retry")
    Path(job["destination"]).rename(moved)
    state = ctx.load_state()
    state["jobs"][job["jobId"]].update(state="failed_download", failureCode="injected_failure", failureMessage="Injected by the test.")
    ctx.save_state(state)
    with ctx.host() as client:
        refused = client.call("stream_download_retry", jobId=job["jobId"], newIdempotencyKey=job_key(state, job))
        ctx.check("a retry must use a new idempotency key", not refused["ok"] and refused["code"] == "new_key_required")
        retry = ok(client.call("stream_download_retry", jobId=job["jobId"], newIdempotencyKey="retry-new-key-001"))
        ctx.check("a definite failure is retried as a fresh job", retry["jobId"] != job["jobId"])
        done = client.wait_for(retry, ctx.console)
        ctx.check("the retried download completes at the original destination", Path(done["destination"]) == Path(job["destination"]) and ctx.verify_output(done))
        again = ok(client.call("stream_download_retry", jobId=job["jobId"], newIdempotencyKey="retry-new-key-001"))
        ctx.check("repeating the retry key replays the retry job", again["jobId"] == retry["jobId"])


def job_key(state: dict, job: dict) -> str:
    return state["jobs"][job["jobId"]]["idempotencyKey"]


def test_missing_output(ctx: Context) -> None:
    job = ctx.need("mp4")
    destination = Path(job["destination"])
    destination.rename(ctx.run / "completed-output-removed-for-test.mp4")
    with ctx.host() as client:
        status = client.call("stream_download_status", jobId=job["jobId"])
        ctx.check("a vanished completed file is not reported as current success",
                  not status["ok"] and status["code"] == "completed_output_changed")
        replay = client.call("stream_download", url=ctx.server.base + "/index.html", destination=str(destination),
                             fileName="ignored-name.bin", format="webm", quality="360p", idempotencyKey="site-test-mp4")
        ctx.check("replaying the old key keeps history and asks for a new key",
                  not replay["ok"] and replay["code"] == "completed_file_missing")
        fresh = client.wait_for(ok(client.call("stream_download", url=ctx.server.base + "/index.html", destination=str(destination),
                                                quality="360p", idempotencyKey="site-test-mp4-again")), ctx.console)
        ctx.check("a new key downloads it again", ctx.verify_output(fresh) and Path(fresh["destination"]) == destination)


def test_single_owner(ctx: Context) -> None:
    with ctx.host() as client:
        try:
            McpClient(ctx.run, ctx.executable).close()
            refused, reason = False, "a second host started"
        except HostRefused as exc:
            refused, reason = True, str(exc)
        ctx.console.info(reason[:220])
        ctx.check("a second host cannot own the same state directory", refused and "owns this state directory" in reason, reason)
        ctx.check("the first host keeps working", ok(client.call("stream_capabilities"))["serverName"] == "Stream-Downloader")


def test_private_network_default(ctx: Context) -> None:
    with ctx.host(BlockPrivateNetworkTargets="true") as client:
        denied = client.call("stream_download", url=ctx.server.base + "/index.html", destination=str(ctx.out / "private.mp4"))
    ctx.check("the private-network default refuses a loopback target", not denied["ok"] and "private" in denied["message"], denied["message"])


def test_graceful_shutdown(ctx: Context) -> None:
    client = McpClient(ctx.run, ctx.executable)
    active = ok(client.call("stream_download", url=ctx.server.slow_url("graceful"), destination=str(ctx.out / "interrupted.mp4"), quality="360p"))
    assert ctx.server.started("graceful").wait(90), "yt-dlp never reached the slow fixture"
    host_pid = client.process.pid
    ctx.console.step("closing the MCP connection while yt-dlp is mid-download")
    graceful = client.close()
    ctx.server.release("graceful")
    record = ctx.load_state()["jobs"][active["jobId"]]
    ctx.check("an active download stops gracefully and records host_stopped",
              graceful and record["state"] == "failed_download" and record["failureCode"] == "host_stopped", record["state"])
    survivors = descendants_of(host_pid)
    ctx.check("no yt-dlp or FFmpeg descendant outlives the host", not survivors, survivors)


def test_unclean_crash(ctx: Context) -> None:
    client = McpClient(ctx.run, ctx.executable)
    active = ok(client.call("stream_download", url=ctx.server.slow_url("crash"), destination=str(ctx.out / "crashed.mp4"),
                            quality="360p", idempotencyKey="unclean-crash"))
    assert ctx.server.started("crash").wait(90), "yt-dlp never reached the slow fixture"
    ctx.console.step("killing the host and its children mid-download (no shutdown code runs)")
    client.crash()
    ctx.server.release("crash")
    with ctx.host() as client:
        unknown = client.wait_for(active, ctx.console, expected="outcome_unknown")
        ctx.check("after a REAL crash the transfer is reported unknown, not failed and not done",
                  unknown["failureCode"] == "interrupted_by_restart")
        refused = client.call("stream_download_retry", jobId=active["jobId"], newIdempotencyKey="retry-after-crash")
        ctx.check("the unknown outcome is not retried automatically", not refused["ok"] and refused["code"] == "job_not_retryable")
        again = client.wait_for(ok(client.call("stream_download", url=ctx.server.base + "/index.html", destination=str(ctx.out / "crashed.mp4"),
                                                quality="360p", idempotencyKey="after-crash-new-key")), ctx.console)
        ctx.check("a deliberate new request afterwards succeeds", ctx.verify_output(again))


def test_incomplete_state(ctx: Context) -> None:
    saved = ctx.state_path.read_text(encoding="utf-8")
    ctx.state_path.write_text("{}", encoding="utf-8")
    try:
        McpClient(ctx.run, ctx.executable).close()
        refused, reason = False, "the host started"
    except HostRefused as exc:
        refused, reason = True, str(exc)
    ctx.console.info(reason[:220])
    ctx.check("incomplete state refuses startup instead of being reset",
              refused and "incomplete" in reason and ctx.state_path.read_text(encoding="utf-8") == "{}", reason)
    ctx.state_path.write_text(saved, encoding="utf-8")
    with ctx.host() as client:
        ctx.check("the preserved state opens again once restored", ok(client.call("stream_download_list", limit=100))["total"] > 0)


TESTS = [test_first_use_acquisition, test_protocol_and_capabilities, test_site_formats, test_unsupported_combination,
         test_quality_cap, test_destination_semantics, test_input_validation, test_secrets_at_rest, test_download_slots,
         test_resource_plan, test_restart_recovery, test_fault_injection, test_retry, test_missing_output, test_single_owner,
         test_private_network_default, test_graceful_shutdown, test_unclean_crash, test_incomplete_state]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, default=DEFAULT_EXECUTABLE)
    parser.add_argument("--run", default=None, help="evidence directory inside test-artifacts (default: a new local-*)")
    parser.add_argument("--protocol-only", action="store_true")
    args = parser.parse_args()
    try:
        run = paths.evidence_dir(args.run, "local")
    except paths.UnsafePath as exc:
        print(f"UNSAFE_PATH ({preflight.EXIT_UNSAFE_PATH}): {exc}", flush=True)
        return preflight.EXIT_UNSAFE_PATH
    console = Console(run / "live.log", f"Stream-Downloader local suite {run.name}")
    console.banner("Stream-Downloader - local integration suite (MCP over STDIO)",
                   f"Angela, every check prints here as it runs. Evidence: {run}")
    started = time.time()
    ctx: Context | None = None
    exit_code = preflight.EXIT_PASS
    try:
        executable = preflight.executable(args.executable)
        preflight.spec()
        ffmpeg, ffprobe = preflight.ffmpeg_pair()
        try:
            tlamatini = Tlamatini()
            console.info(f"photos by Shoter, browser by Playwrighter, from {tlamatini.root}")
        except TlamatiniMissing as exc:
            tlamatini = None
            console.warn(f"{exc} - checks still run, but photos will be reported missing")
        ctx = Context(run, executable, console, tlamatini)
        ctx.ffmpeg, ctx.ffprobe = ffmpeg, ffprobe
        ctx.media = make_media(run / "fixtures", ffmpeg)
        ctx.payload = ctx.media["bin"].read_bytes()
        ctx.server = FixtureServer(run / "fixtures")
        console.info(f"fixtures served at {ctx.server.base}")
        selected = TESTS[:2] if args.protocol_only else TESTS
        for number, test in enumerate(selected, 1):
            name = test.__name__.removeprefix("test_").replace("_", " ")
            console.section(f"test {number}/{len(selected)}: {name}")
            record = dict(test=name, passed=False, photo=None)
            before = len(ctx.results)
            try:
                test(ctx)
                record["passed"] = True
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["traceback"] = traceback.format_exc()
                console.fail(f"{name}: {type(exc).__name__}: {exc}")
                ctx.results.append(dict(name=name, passed=False, error=record["error"]))
                if isinstance(exc, HostRefused):
                    console.info("host STDERR: " + str(exc)[:400])
            record["checks"] = len(ctx.results) - before
            if tlamatini:
                record["evidence"] = tlamatini.console_evidence(run / "photos", f"{number:02d}-{test.__name__}.png", console.title)
                record["photo"] = record["evidence"]["path"] if record["evidence"]["verified"] else None
            report_photo(console, record.get("evidence"))
            ctx.tests.append(record)
        failed = [t for t in ctx.tests if not t["passed"]]
        missing = [t for t in ctx.tests if not t["photo"]]
        exit_code = preflight.EXIT_TEST_FAILURE if failed else preflight.EXIT_EVIDENCE_INCOMPLETE if missing else preflight.EXIT_PASS
        checks = sum(1 for r in ctx.results if r["passed"])
        console.banner(("PASS" if exit_code == 0 else "FAIL" if failed else "PASS (evidence incomplete)")
                       + f" - {len(ctx.tests) - len(failed)}/{len(ctx.tests)} tests, {checks} checks passed",
                       f"{len(ctx.tests) - len(missing)} confirmed photos by Shoter; {time.time() - started:.0f} s")
    except preflight.PreflightFailure as exc:
        exit_code = exc.code
        console.fail(f"PREFLIGHT {exc}")
    finally:
        if ctx and ctx.server:
            ctx.server.close()
        summary = dict(suite="local", executable=str(args.executable),
                       executableSha256=sha256_file(args.executable) if Path(args.executable).is_file() else None,
                       exitCode=exit_code, exitName=preflight.NAMES.get(exit_code), seconds=round(time.time() - started, 1),
                       tests=ctx.tests if ctx else [], checks=ctx.results if ctx else [])
        (run / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        console.info(f"Evidence retained: {run}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
