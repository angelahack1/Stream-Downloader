"""Downloads Angela's own YouTube videos through the real Stream-Downloader MCP, then proves each one.

Every download listed in tests/my_videos.json is submitted at once over MCP STDIO with production
defaults (private-network blocking on, first-use yt-dlp acquisition allowed), so the
hardware-sized download slots are exercised on real data. Each finished file is then checked
against evidence the server never produced:

* an ORACLE: yt-dlp itself, asked with the very same format selector, says which streams should
  be in the file (height, codecs, original-language audio) and how long the video is;
* the file on disk: size and SHA-256 recomputed by the test, an independent ffprobe, and a FULL
  FFmpeg decode of every frame;
* the eye: Tlamatini's Playwrighter opens it in the real Chrome, HEADED, plays it and measures the
  playback; Windower puts the console (left) and Chrome (right) in front; Shoter photographs the
  whole desktop. A photo whose window could not be confirmed in front is not counted.

Nothing here reads cookies or account data. Output stays inside this run's evidence folder.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from harness import paths, preflight
from harness.console import Console, human_bytes
from harness.mcp_client import DEFAULT_EXECUTABLE, ROOT, TOOLS_DIRECTORY, HostRefused, McpClient, sha256_file
from harness.tlamatini import Tlamatini, TlamatiniMissing

CONFIG = Path(__file__).resolve().parent / "my_videos.json"
VIDEO_FORMATS = {"mp4", "mkv", "webm"}
DEMUXER = {"mp4": "mp4", "mkv": "matroska", "webm": "webm", "mp3": "mp3", "m4a": "mp4", "opus": "ogg",
           "flac": "flac", "wav": "wav", "aac": "aac"}
CODEC_FAMILY = {"av01": "av1", "avc1": "h264", "vp09": "vp9", "vp9": "vp9", "vp8": "vp8", "mp4a": "aac",
                "opus": "opus", "vorbis": "vorbis", "mp3": "mp3", "flac": "flac"}
AUDIO_OUTPUT_CODEC = {"mp3": "mp3", "m4a": "aac", "opus": "opus", "flac": "flac", "wav": "pcm_s16le", "aac": "aac"}


def selector(quality: str, container: str) -> str:
    """The same format selector the server builds (YtDlpService.BuildVideoFormat / audio path)."""
    if quality == "audio":
        return "bestaudio/best"
    cap = 0 if quality == "best" else int(quality.rstrip("p"))
    mp4 = container == "mp4"
    if cap == 0:
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b" if mp4 else "bv*+ba/b"
    f = f"[height<=?{cap}]"
    return f"bv*{f}[ext=mp4]+ba[ext=m4a]/bv*{f}+ba/b{f}" if mp4 else f"bv*{f}+ba/b{f}"


def family(codec: str | None) -> str | None:
    if not codec or codec == "none":
        return None
    return CODEC_FAMILY.get(codec.split(".")[0].lower(), codec.split(".")[0].lower())


def comparable(text: str) -> str:
    """Title equality that survives Windows-safe filename substitutions (e.g. ':' -> '：')."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^0-9a-zà-ÿñ]+", "", text)


class Oracle:
    def __init__(self, yt_dlp: Path, js_runtime: str | None, console: Console):
        self.yt_dlp, self.js, self.console = yt_dlp, js_runtime, console

    def ask(self, url: str, quality: str, container: str) -> dict:
        command = [str(self.yt_dlp), "--ignore-config", "--no-warnings", "--no-playlist", "-J", "-f", selector(quality, container)]
        if self.js:
            command += ["--js-runtimes", self.js]
        completed = subprocess.run(command + ["--", url], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=180)
        if completed.returncode != 0:
            raise AssertionError(f"oracle yt-dlp failed: {completed.stderr.strip()[-400:]}")
        info = json.loads(completed.stdout)
        chosen = info.get("requested_formats") or [info]
        video = next((f for f in chosen if f.get("vcodec") not in (None, "none")), None)
        audio = next((f for f in reversed(chosen) if f.get("acodec") not in (None, "none")), None)
        languages = {f.get("language") for f in info.get("formats", [])
                     if f.get("acodec") not in (None, "none") and f.get("language")}
        return dict(id=info["id"], title=info.get("title", ""), duration=float(info.get("duration") or 0),
                    formatIds="+".join(str(f.get("format_id")) for f in chosen),
                    height=int(video.get("height") or 0) if video else None, vcodec=family(video.get("vcodec")) if video else None,
                    acodec=family(audio.get("acodec")) if audio else None, language=(audio or {}).get("language"),
                    audioNote=(audio or {}).get("format_note") or "", audioLanguages=sorted(languages))


def probe(ffprobe: str, path: Path) -> dict:
    completed = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=True)
    data = json.loads(completed.stdout)
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return dict(format=data["format"]["format_name"], duration=float(data["format"].get("duration") or 0),
                height=max((int(s.get("height") or 0) for s in video), default=None),
                width=max((int(s.get("width") or 0) for s in video), default=None),
                video=[s.get("codec_name") for s in video], audio=[s.get("codec_name") for s in audio],
                audioLanguage=[(s.get("tags") or {}).get("language") for s in audio])


def full_decode(ffmpeg: str, path: Path) -> tuple[bool, str, float]:
    started = time.monotonic()
    completed = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800)
    return completed.returncode == 0 and not completed.stderr.strip(), completed.stderr.strip()[:300], time.monotonic() - started


EVIDENCE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SD evidence {token} - {heading}</title>
<style>
:root{{color-scheme:dark}} body{{margin:0;background:#12121a;color:#ececf4;font:14px/1.45 "Segoe UI",sans-serif}}
header{{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;background:linear-gradient(90deg,#3d1766,#6b3fa0)}}
header h1{{margin:0;font-size:17px}} #clock{{font:600 20px Consolas,monospace}}
#banner{{margin:12px 18px;padding:10px 14px;border-radius:8px;background:#3a3320;font-weight:600}}
#banner.ok{{background:#173d26}} #banner.bad{{background:#4a1d24}}
main{{padding:0 18px 18px}} video,audio{{width:100%;max-height:330px;background:#000;border-radius:8px}}
#play{{margin:0 18px 10px;padding:8px 18px;border:0;border-radius:8px;background:#6b3fa0;color:#fff;font-weight:600;cursor:pointer}}
table{{width:100%;table-layout:fixed;border-collapse:collapse;margin-top:10px;font-size:12.5px}} td{{padding:3px 6px;border-bottom:1px solid #2a2a38;vertical-align:top;overflow-wrap:anywhere}}
td:first-child{{color:#a8a8c0;width:24%}} code{{font:12px Consolas,monospace;word-break:break-all;white-space:normal}}
</style></head><body>
<header><h1>Stream-Downloader evidence for Angela &mdash; {heading}</h1><div id="clock"></div></header>
<div id="banner">Waiting for the Play click&hellip;</div>
<button id="play" type="button">&#9654; Play the downloaded file</button>
<main>{player}<table>{rows}<tr><td>Chrome playback</td><td id="measured">measuring&hellip;</td></tr></table></main>
<script>
const token = {token_js};
setInterval(() => {{ document.getElementById('clock').textContent = new Date().toLocaleString(); }}, 200);
const media = document.getElementById('media');
window.__playbackDone = new Promise(resolve => {{
  const started = performance.now();
  const finish = (ok, why) => {{
    const quality = media.getVideoPlaybackQuality ? media.getVideoPlaybackQuality() : null;
    const result = {{ ok, why, currentTime: media.currentTime, duration: media.duration, readyState: media.readyState,
      videoWidth: media.videoWidth || 0, videoHeight: media.videoHeight || 0,
      decodedFrames: quality ? quality.totalVideoFrames : null, droppedFrames: quality ? quality.droppedVideoFrames : null,
      error: media.error ? media.error.code + ' ' + (media.error.message || '') : null, waitedMs: Math.round(performance.now() - started) }};
    const banner = document.getElementById('banner');
    banner.className = ok ? 'ok' : 'bad';
    banner.textContent = (ok ? 'PLAYBACK VERIFIED in Chrome: ' : 'PLAYBACK NOT VERIFIED: ') + why;
    document.getElementById('measured').textContent = JSON.stringify(result);
    document.title = (ok ? 'VERIFIED ' : 'FAILED ') + token + ' - ' + document.title;
    resolve(result);
  }};
  const target = () => Math.min(2.5, isFinite(media.duration) ? media.duration * 0.6 : 2.5);
  media.addEventListener('error', () => finish(false, 'media error ' + (media.error && media.error.code)));
  media.addEventListener('timeupdate', () => {{ if (media.currentTime >= target()) {{ finish(true, 'played ' + media.currentTime.toFixed(2) + ' s of ' + media.duration.toFixed(2) + ' s'); }} }});
  media.addEventListener('ended', () => finish(true, 'played to the end (' + media.duration.toFixed(2) + ' s)'));
  setTimeout(() => finish(false, 'no progress within 30 s (currentTime ' + media.currentTime.toFixed(2) + ')'), 30000);
  // Chrome only lets media start after a real user gesture; Playwrighter's click on this button is one.
  document.getElementById('play').addEventListener('click', () => {{
    document.getElementById('banner').textContent = 'Playing the downloaded file in Chrome...';
    media.muted = true;
    media.play().catch(e => finish(false, 'play() rejected: ' + e));
  }});
}});
</script></body></html>"""


def evidence_page(path: Path, token: str, heading: str, media_path: Path, is_video: bool, facts: dict) -> Path:
    source = media_path.resolve().as_uri()
    player = (f'<video id="media" src="{source}" controls playsinline></video>' if is_video
              else f'<audio id="media" src="{source}" controls></audio>')
    rows = "".join(f"<tr><td>{html.escape(k)}</td><td><code>{html.escape(str(v))}</code></td></tr>" for k, v in facts.items())
    path.write_text(EVIDENCE_PAGE.format(token=token, token_js=json.dumps(token), heading=html.escape(heading), player=player, rows=rows),
                    encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default=None, help="evidence directory inside test-artifacts")
    parser.add_argument("--config", default=str(CONFIG), help="a .json list of videos inside tests")
    parser.add_argument("--executable", type=Path, default=DEFAULT_EXECUTABLE)
    args = parser.parse_args()
    try:
        run = paths.evidence_dir(args.run, "myvideos")
        args.config = paths.tests_file(args.config, ".json")
    except paths.UnsafePath as exc:
        print(f"UNSAFE_PATH ({preflight.EXIT_UNSAFE_PATH}): {exc}", flush=True)
        return preflight.EXIT_UNSAFE_PATH
    console = Console(run / "live.log", f"Stream-Downloader my videos {run.name}")
    console.banner("Stream-Downloader - Angela's own videos, end to end",
                   f"Downloaded over MCP, checked against yt-dlp, decoded, played in Chrome. Evidence: {run}")
    started = time.time()
    records: list[dict] = []
    exit_code = preflight.EXIT_PASS
    client = None
    try:
        executable = preflight.executable(args.executable)
        preflight.spec()
        ffmpeg, ffprobe = preflight.ffmpeg_pair()
        js = preflight.javascript_runtime()
        preflight.network("www.youtube.com")
        preflight.chrome()
        try:
            tlamatini = Tlamatini()
        except TlamatiniMissing as exc:
            raise preflight.PreflightFailure(preflight.EXIT_TLAMATINI_MISSING, str(exc))
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        console.info(f"owner: {config.get('owner')} - channel {config.get('channel')}")
        console.info(f"javascript runtime for YouTube: {js}")
        console.info(f"photos by Shoter, windows by Windower, browser by Playwrighter, from {tlamatini.root}")

        out = run / "output"
        client = McpClient(run, executable, BlockPrivateNetworkTargets="true", AutoAcquireYtDlp="true",
                           SiteDownloadTimeoutSeconds="1800")
        caps = client.call("stream_capabilities")["data"]
        console.info(f"machine: {caps['machine']['summary']}")
        console.info(f"decided: {caps['machine']['maxConcurrentDownloads']} downloads at a time, "
                     f"{caps['machine']['concurrentFragments']} fragments, {caps['machine']['childPriority']} priority")

        # ---- submit everything at once: the scheduler decides how many run together
        console.section("submitting every download at once")
        for video_number, video in enumerate(config["videos"], 1):
            for download in video["downloads"]:
                name = download.get("name")
                destination = out / name if name else out / f"video-{video_number}"
                key = f"angela-{uuid.uuid4().hex[:12]}"
                arguments = dict(url=video["url"], destination=str(destination), quality=download["quality"],
                                 format=download["format"], idempotencyKey=key)
                response = client.call("stream_download", **arguments)
                record = dict(label=f"{video['url'].rsplit('=', 1)[-1]} {download['quality']} {download['format']}",
                              why=video.get("why", ""), request=arguments, named=bool(name), checks=[], passed=False)
                if not response["ok"]:
                    record["error"] = f"refused: {response['code']} {response['message']}"
                    console.fail(f"{record['label']}: {record['error']}")
                else:
                    record["job"] = response["data"]
                    console.step(f"{record['label']} -> {record['job']['jobId']} ({record['job']['state']})")
                records.append(record)
        first = next((r for r in records if r.get("job")), None)
        if first:
            replay = client.call("stream_download", **first["request"])
            first["checks"].append(dict(name="repeating the identical call replays the same job",
                                        passed=replay["ok"] and replay["data"]["jobId"] == first["job"]["jobId"]))

        # ---- follow every job live until all are terminal
        console.section("live progress (all jobs)")
        pending = {r["job"]["jobId"]: r for r in records if r.get("job")}
        versions = {job_id: r["job"]["stateVersion"] for job_id, r in pending.items()}
        deadline = time.monotonic() + 3600
        while pending and time.monotonic() < deadline:
            for job_id, record in list(pending.items()):
                view = client.call("stream_download_status", jobId=job_id)["data"]
                if view["stateVersion"] != versions[job_id]:
                    versions[job_id] = view["stateVersion"]
                    console.job(view, record["label"])
                if view["terminal"]:
                    record["job"] = view
                    del pending[job_id]
            time.sleep(1.0)
        waited = [r["label"] for r in records if r.get("job") and any(e["code"] == "waiting_for_slot" for e in r["job"]["events"])]
        console.info(f"jobs that queued for a hardware-sized slot: {len(waited)} {waited}")

        caps = client.call("stream_capabilities")["data"]  # after the downloads yt-dlp is certainly resolved
        oracle = Oracle(Path(caps["siteExtraction"]["ytDlpPath"]), caps["siteExtraction"].get("javaScriptRuntime"), console)
        (run / "evidence").mkdir(exist_ok=True)
        # ---- prove every file
        for number, record in enumerate(records, 1):
            console.section(f"download {number}/{len(records)}: {record['label']} - {record['why']}")
            checks = record["checks"]

            def check(name: str, condition, detail=None) -> None:
                checks.append(dict(name=name, passed=bool(condition), detail=None if detail is None else str(detail)[:500]))
                (console.ok if condition else console.fail)(name + ("" if condition or detail is None else f": {detail}"))

            try:
                job = record.get("job")
                if not job:
                    raise AssertionError(record.get("error", "never submitted"))
                check("job completed_verified", job["state"] == "completed_verified", f"{job['state']} {job.get('failureCode')} {job.get('failureMessage')}")
                if job["state"] != "completed_verified":
                    continue
                request = record["request"]
                path = Path(job["destination"])
                container = request["format"]
                expected = oracle.ask(request["url"], request["quality"], container)
                record["oracle"] = expected
                console.info(f"oracle: {expected['formatIds']}  {expected['height'] or 'audio'}  v={expected['vcodec']} a={expected['acodec']} "
                             f"lang={expected['language']}  {expected['duration']:.1f} s  '{expected['title']}'")
                size = path.stat().st_size if path.is_file() else -1
                digest = sha256_file(path) if path.is_file() else ""
                check("file on disk: size and SHA-256 recomputed by the test match the job",
                      size == job["observedBytes"] and digest == job["observedSha256"], f"{size} {digest[:16]}")
                if record["named"]:
                    check("saved under the exact requested name", path == Path(request["destination"]), path)
                else:
                    check("named from the video title, with its id, Windows-safe",
                          f"[{expected['id']}]" in path.name and path.suffix == "." + container
                          and comparable(job["sourceTitle"] or "") == comparable(expected["title"]), path.name)
                facts = probe(ffprobe, path)
                record["ffprobe"] = facts
                check(f"independent ffprobe: {DEMUXER[container]} container", DEMUXER[container] in facts["format"].split(","), facts["format"])
                if container in VIDEO_FORMATS:
                    check(f"video stream is what yt-dlp selected: {expected['vcodec']} at {expected['height']}p",
                          facts["video"] == [expected["vcodec"]] and facts["height"] == expected["height"] == job["media"]["height"], facts)
                    check(f"audio stream is the selected {expected['acodec']}", facts["audio"] == [expected["acodec"]], facts["audio"])
                    if request["quality"] != "best":
                        check(f"height never exceeds the {request['quality']} cap", facts["height"] <= int(request["quality"].rstrip("p")))
                else:
                    check(f"audio only, as {AUDIO_OUTPUT_CODEC[container]}", not facts["video"] and facts["audio"] == [AUDIO_OUTPUT_CODEC[container]], facts)
                if len(expected["audioLanguages"]) > 1:
                    check(f"of {len(expected['audioLanguages'])} audio languages, the ORIGINAL track was taken, not an automatic dub",
                          "original" in expected["audioNote"].lower(), expected["audioNote"])
                else:
                    console.info(f"single audio language ({expected['audioLanguages'] or 'untagged'}): no dub to avoid")
                check(f"duration matches YouTube's ({expected['duration']:.1f} s) within 1.5 s",
                      abs(facts["duration"] - expected["duration"]) <= 1.5, f"{facts['duration']:.2f}")
                decoded, errors, seconds = full_decode(ffmpeg, path)
                check(f"every frame decodes without error (full FFmpeg decode, {seconds:.1f} s)", decoded, errors)

                # ---- the eye: headed Chrome through Playwrighter, photographed by Shoter
                token = uuid.uuid4().hex[:10]
                is_video = container in VIDEO_FORMATS
                page = evidence_page(run / "evidence" / f"{number:02d}-{token}.html",
                                     token, f"{expected['title'][:80]} ({request['quality']} {container})", path, is_video, {
                                         "Request": f"{request['url']} quality={request['quality']} format={container}",
                                         "Saved as": str(path), "Job": f"{job['jobId']} - {job['state']}",
                                         "Size": f"{size:,} bytes ({human_bytes(size)})",
                                         "SHA-256 (server = test)": f"{job['observedSha256']} = {digest}",
                                         "yt-dlp selected": f"{expected['formatIds']} - {expected['vcodec'] or 'no video'} {expected['height'] or ''} + {expected['acodec']} ({expected['audioNote']})",
                                         "ffprobe": f"{facts['format']} video={facts['video']}"
                                                    + (f" {facts['width']}x{facts['height']}" if facts["video"] else "")
                                                    + f" audio={facts['audio']} {facts['duration']:.2f} s",
                                         "Full decode": "every frame decoded without error" if decoded else errors})
                result_box: dict = {}

                def drive() -> None:
                    try:
                        result_box["result"] = tlamatini.browse(
                            [{"action": "goto", "url": page.resolve().as_uri()},
                             {"action": "click", "selector": "#play"},
                             {"action": "evaluate", "expression": "async () => JSON.stringify(await window.__playbackDone)", "name": "playback"},
                             {"action": "wait", "ms": 14000}],
                            run / "_playwrighter", page.resolve().as_uri(), hold_open_seconds=1, viewport=(820, 900))
                    except Exception as exc:  # the agent could not be launched at all
                        result_box["error"] = f"{type(exc).__name__}: {exc}"

                driver = threading.Thread(target=drive, daemon=True)
                driver.start()
                photo = None
                appear = time.monotonic() + 90
                while time.monotonic() < appear and driver.is_alive():
                    listing = tlamatini.windows(token, run / "photos")
                    if f"VERIFIED {token}" in listing or f"FAILED {token}" in listing:
                        tlamatini.arrange(console.title, "left", run / "photos")
                        tlamatini.arrange(token, "right", run / "photos")
                        photo = tlamatini.evidence(run / "photos", f"{number:02d}-{token}.png", token)
                        break
                    time.sleep(1.0)
                driver.join(timeout=120)
                outcome = result_box.get("result") or {}
                playback = {}
                try:
                    playback = json.loads(outcome.get("extracted", {}).get("playback") or "{}")
                except ValueError:
                    pass
                record["playback"] = playback
                if is_video and container == "mkv" and playback.get("error"):
                    console.warn("Chrome does not play this Matroska file; the full FFmpeg decode above is its playback proof")
                    check("MKV playback proven by the full decode (Chrome lacks Matroska support)", decoded)
                elif is_video:
                    check("Chrome PLAYED it (Playwrighter, headed): time advanced, dimensions match, frames decoded",
                          playback.get("ok") and playback.get("videoHeight") == facts["height"] and (playback.get("decodedFrames") or 0) > 0,
                          playback or result_box.get("error") or outcome.get("status"))
                else:
                    check("Chrome PLAYED it (Playwrighter, headed): time advanced without a media error",
                          playback.get("ok") and not playback.get("error"), playback or result_box.get("error") or outcome.get("status"))
                record["photo"] = photo
                if photo and photo.get("verified"):
                    console.info(f"photo (Shoter; Windower put the console and Chrome in front, confirmed): {photo['path']}")
                else:
                    console.warn(f"photo not confirmed as evidence: {photo}")
            except Exception as exc:
                check("download proven", False, f"{type(exc).__name__}: {exc}")
            finally:
                record["passed"] = bool(checks) and all(c["passed"] for c in checks)

        failed = [r for r in records if not r["passed"]]
        missing = [r for r in records if r["passed"] and not (r.get("photo") or {}).get("verified")]
        total_bytes = sum((r.get("job") or {}).get("observedBytes") or 0 for r in records)
        exit_code = preflight.EXIT_TEST_FAILURE if failed or not records else preflight.EXIT_EVIDENCE_INCOMPLETE if missing else preflight.EXIT_PASS
        console.section("summary")
        for r in records:
            job = r.get("job") or {}
            console.emit(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['label']:<26} {human_bytes(job.get('observedBytes')):>12}  {Path(job.get('destination', '?')).name}")
        console.banner(f"{'PASS' if exit_code == 0 else 'FAIL' if failed else 'PASS (evidence incomplete)'} - "
                       f"{len(records) - len(failed)}/{len(records)} of Angela's downloads proven",
                       f"{human_bytes(total_bytes)} downloaded; {len(records) - len(missing) - len(failed)} confirmed photos; {time.time() - started:.0f} s")
    except preflight.PreflightFailure as exc:
        exit_code = exc.code
        console.fail(f"PREFLIGHT {exc}")
    except HostRefused as exc:
        exit_code = preflight.EXIT_HOST_REFUSED
        console.fail(f"HOST REFUSED: {exc}")
    finally:
        if client:
            client.close()
        (run / "results.json").write_text(json.dumps(dict(suite="my-videos", exitCode=exit_code, exitName=preflight.NAMES.get(exit_code),
                                                          seconds=round(time.time() - started, 1), downloads=records), indent=2, default=str),
                                          encoding="utf-8")
        console.info(f"Evidence retained: {run}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
