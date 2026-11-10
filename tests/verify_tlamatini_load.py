"""Proves Tlamatini loads Stream-Downloader.json as an External MCP - with Tlamatini's OWN client.

1. Stream-Downloader.json is read exactly as Tlamatini's External-MCP importer reads a dropped file
   (the ``mcpServers`` map through ``_normalize_imported_server_spec``). Angela's catalog
   (``external_mcps.json``) is never read for writing and never modified.
2. Tlamatini's own ``_StdioMcpClient`` spawns the server from that spec, performs the MCP handshake
   and lists the tools - the very code path her External MCPs use in chat.
3. Every tool becomes a valid, unique ``ext__Stream-Downloader__<tool>`` wrapper name.
4. A real download runs THROUGH Tlamatini's client: stream_download of a local fixture, then
   stream_download_wait until completed_verified, then the file is verified on disk by this test.

The only addition to the spec is an isolated state/output directory (environment variables), so the
test never touches the real job history. Runs visibly; ends with a confirmed Shoter photo.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

from harness import paths, preflight
from harness.console import Console, human_bytes
from harness.fixtures import FixtureServer, make_media
from harness.mcp_client import DEFAULT_EXECUTABLE, EXPECTED_TOOLS, ROOT, SERVER_KEY, SPEC_FILE, sha256_file
from harness.tlamatini import Tlamatini, TlamatiniMissing


def first_json(text: str) -> dict:
    """The first JSON object inside Tlamatini's formatted tool result."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
            if isinstance(value, dict) and "ok" in value:
                return value
        except ValueError:
            continue
    raise AssertionError(f"no MCP response envelope in: {text[:300]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default=None, help="evidence directory inside test-artifacts")
    args = parser.parse_args()
    try:
        run = paths.evidence_dir(args.run, "tlamatini")
    except paths.UnsafePath as exc:
        print(f"UNSAFE_PATH ({preflight.EXIT_UNSAFE_PATH}): {exc}", flush=True)
        return preflight.EXIT_UNSAFE_PATH
    console = Console(run / "live.log", f"Stream-Downloader in Tlamatini {run.name}")
    console.banner("Stream-Downloader.json loaded by Tlamatini's own External-MCP client",
                   "Angela, this is the exact path Tlamatini uses when you drop the file on External > MCPs.")
    results: list[dict] = []
    exit_code = preflight.EXIT_PASS
    client = server = None

    def check(name: str, condition, detail=None) -> None:
        results.append(dict(name=name, passed=bool(condition), detail=None if detail is None else str(detail)[:1500]))
        (console.ok if condition else console.fail)(name + ("" if condition or detail is None else f": {str(detail)[:400]}"))

    try:
        executable = preflight.executable()
        ffmpeg, _ = preflight.ffmpeg_pair()
        try:
            tlamatini = Tlamatini()
        except TlamatiniMissing as exc:
            raise preflight.PreflightFailure(preflight.EXIT_TLAMATINI_MISSING, str(exc))
        sys.path.insert(0, str(tlamatini.root / "Tlamatini"))
        import agent.external_mcp_manager as manager  # Tlamatini's own External-MCP engine
        catalog = Path(manager.catalog_path())
        catalog_before = catalog.read_bytes() if catalog.is_file() else None
        console.info(f"Tlamatini engine: {manager.__file__}")

        console.section("the file, as Tlamatini's importer reads it")
        dropped = json.loads(SPEC_FILE.read_text(encoding="utf-8-sig"))
        servers = dropped.get("mcpServers", dropped)
        check("Stream-Downloader.json carries exactly one server, keyed Stream-Downloader", list(servers) == [SERVER_KEY], list(servers))
        spec = manager._normalize_imported_server_spec(SERVER_KEY, servers[SERVER_KEY])
        check("Tlamatini's importer accepts it as a stdio server", spec is not None and spec.get("transport") == "stdio", spec)
        check("it launches Stream-Downloader's own executable", Path(spec["command"]).resolve() == executable.resolve(), spec["command"])
        check("it describes itself and credits its author", "stream_download" in spec.get("description", "")
              and "Angela López Mendoza" in spec.get("description", ""))
        wrappers = [manager._safe_tool_name(SERVER_KEY, name) for name in EXPECTED_TOOLS]
        check("every tool becomes a valid, unique ext__ wrapper name (64 characters at most)",
              len(set(wrappers)) == len(wrappers) and all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", w) for w in wrappers), wrappers)
        console.info("wrapped as: " + ", ".join(wrappers))

        console.section("Tlamatini's own client spawns it and shakes hands")
        environment = dict(spec.get("env") or {})
        environment.update({"StreamDownloader__StateDirectory": str(run / "state"),
                            "StreamDownloader__DefaultDownloadDirectory": str(run / "output"),
                            "StreamDownloader__BlockPrivateNetworkTargets": "false",
                            "StreamDownloader__AutoAcquireYtDlp": "false"})
        client = manager._StdioMcpClient(SERVER_KEY, spec["command"], spec.get("args", []), env=environment, cwd=spec.get("cwd", ""))
        started = time.monotonic()
        client.connect()
        names = sorted(tool["name"] for tool in client.tools)
        check(f"connected in {time.monotonic() - started:.1f} s and listed the six tools", names == sorted(EXPECTED_TOOLS), names)
        caps = first_json(client.call_tool("stream_capabilities", {}))
        check("stream_capabilities answers through Tlamatini", caps["ok"] and caps["data"]["serverName"] == "Stream-Downloader", caps.get("code"))

        console.section("a real download, driven through Tlamatini's client")
        make_media(run / "fixtures", ffmpeg)
        server = FixtureServer(run / "fixtures")
        destination = run / "output" / "downloaded through Tlamatini.mp4"
        submitted = first_json(client.call_tool("stream_download", {"url": server.base + "/index.html", "destination": str(destination),
                                                                    "quality": "360p", "idempotencyKey": "tlamatini-" + uuid.uuid4().hex[:12]}))
        check("stream_download accepted the job", submitted["ok"], submitted.get("message"))
        job = submitted["data"]
        deadline = time.monotonic() + 240
        while not job["terminal"] and time.monotonic() < deadline:
            response = first_json(client.call_tool("stream_download_wait", {"jobId": job["jobId"], "afterVersion": job["stateVersion"], "timeoutSeconds": 10}))
            job = response["data"]
            console.job(job, "via Tlamatini")
        check("the job reached completed_verified", job["state"] == "completed_verified", f"{job['state']} {job.get('failureMessage')}")
        check("the file is on disk with the size and SHA-256 the job reported",
              destination.is_file() and destination.stat().st_size == job["observedBytes"] and sha256_file(destination) == job["observedSha256"],
              human_bytes(job.get("observedBytes")))
        listing = first_json(client.call_tool("stream_download_list", {"limit": 5}))
        check("stream_download_list sees it", listing["ok"] and any(j["jobId"] == job["jobId"] for j in listing["data"]["jobs"]))
        catalog_after = catalog.read_bytes() if catalog.is_file() else None
        check("Angela's Tlamatini catalog was not touched", catalog_after == catalog_before, catalog)
    except preflight.PreflightFailure as exc:
        exit_code = exc.code
        console.fail(f"PREFLIGHT {exc}")
    except Exception as exc:
        results.append(dict(name="suite", passed=False, detail=f"{type(exc).__name__}: {exc}"))
        console.fail(f"{type(exc).__name__}: {exc}")
        if client is not None:
            console.info("server STDERR (head): " + (client.stderr_tail or "")[:600])
    finally:
        if client is not None:
            client.close()
        if server is not None:
            server.close()
        failed = [r for r in results if not r["passed"]]
        if exit_code == preflight.EXIT_PASS and (failed or not results):
            exit_code = preflight.EXIT_TEST_FAILURE
        evidence = None
        try:
            evidence = Tlamatini().console_evidence(run / "photos", "tlamatini-load.png", console.title)
        except TlamatiniMissing as exc:
            console.warn(str(exc))
        if evidence and evidence["verified"]:
            console.info(f"photo (Shoter; Windower brought this window forward, confirmed in front): {evidence['path']}")
        else:
            console.warn(f"photo not confirmed as evidence: {evidence}")
            if exit_code == preflight.EXIT_PASS:
                exit_code = preflight.EXIT_EVIDENCE_INCOMPLETE
        console.banner(f"{'PASS' if exit_code == 0 else 'FAIL'} - {len(results) - len(failed)}/{len(results)} checks",
                       f"Evidence retained: {run}")
        (run / "results.json").write_text(json.dumps(dict(suite="tlamatini-load", exitCode=exit_code, evidence=evidence, checks=results),
                                                     indent=2, default=str), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
