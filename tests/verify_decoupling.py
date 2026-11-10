"""Proves Stream-Downloader is decoupled from, and independent of, the program it mirrors.

* Source: no code, script, test or configuration file names the original program, its COM
  interop, its tool names, its state/tools directories, its staging folder or its folder on disk.
  The only files allowed to name it are the comparison document and its generator, the human
  documentation that explains the origin, and the two verifiers that must read it.
* Binary: the published folder holds only Stream-Downloader's own files, and the self-contained
  executable carries no trace of the COM interop assembly or of the original namespaces.
* Runtime: the real server, started over MCP, reports its own identity, its own tools and its own
  yt-dlp in its own tools directory.

Runs visibly; ends with a full-desktop photo by Tlamatini's Shoter.
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
from harness.console import Console
from harness.mcp_client import DEFAULT_EXECUTABLE, EXPECTED_TOOLS, ROOT, TOOLS_DIRECTORY, McpClient
from harness.tlamatini import Tlamatini, TlamatiniMissing

FORBIDDEN = {
    "the original product name": re.compile(r"\bIDM\b"),
    "the COM interop / IDM executable": re.compile(r"IDMan", re.IGNORECASE),
    "the COM link transmitter": re.compile(r"CIDMLinkTransmitter", re.IGNORECASE),
    "the original namespaces/types": re.compile(r"\bIdm[A-Z]|IdmMcp"),
    "the original product folders": re.compile(r"IDM[-_]MCP", re.IGNORECASE),
    "the original tool names": re.compile(r"\bidm_[a-z]"),
    "the original staging folder": re.compile(r"\.idm-mcp-staging", re.IGNORECASE),
    "the original progress markers": re.compile(r"IDMMCP_", re.IGNORECASE),
    "the vendor product": re.compile(r"Internet Download Manager", re.IGNORECASE),
    "the original source folder": re.compile(r"Development[\\/]+IDM\b", re.IGNORECASE),
}
ALLOWED = {"COMPARISON.md", "COMPARISON.html", "README.md", "CLAUDE.md", "CHANGELOG.md", "DESIGN.md",
           "tools/generate_comparison.py", "tests/verify_decoupling.py", "tests/verify_comparison.py"}
SKIP_DIRECTORIES = {".git", "bin", "obj", "artifacts", "test-artifacts", "downloads", "__pycache__", ".vs", ".idea"}
TEXT_SUFFIXES = {".cs", ".csproj", ".props", ".json", ".py", ".ps1", ".md", ".html", ".txt", ".gitignore", ".yaml", ".yml"}
BINARY_MARKERS = ["IDMan", "CIDMLinkTransmitter", "IdmMcp", "IDM-MCP-Core", "idm_download"]


def source_files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRECTORIES for part in relative.parts[:-1]) or not path.is_file():
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name == ".gitignore":
            found.append(path)
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default=None, help="evidence directory inside test-artifacts")
    parser.add_argument("--executable", type=Path, default=DEFAULT_EXECUTABLE)
    args = parser.parse_args()
    try:
        run = paths.evidence_dir(args.run, "decoupling")
    except paths.UnsafePath as exc:
        print(f"UNSAFE_PATH ({preflight.EXIT_UNSAFE_PATH}): {exc}", flush=True)
        return preflight.EXIT_UNSAFE_PATH
    console = Console(run / "live.log", f"Stream-Downloader decoupling {run.name}")
    console.banner("Stream-Downloader - decoupled and independent", "Angela, this proves the mirror needs nothing from the original.")
    results: list[dict] = []
    exit_code = preflight.EXIT_PASS

    def check(name: str, condition, detail=None) -> None:
        results.append(dict(name=name, passed=bool(condition), detail=None if detail is None else str(detail)[:2000]))
        (console.ok if condition else console.fail)(name + ("" if condition or detail is None else f": {str(detail)[:400]}"))

    try:
        executable = preflight.executable(args.executable)
        console.section("source, scripts, tests and configuration")
        files = source_files()
        scanned, offenders = 0, []
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            if relative in ALLOWED:
                continue
            scanned += 1
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for what, pattern in FORBIDDEN.items():
                    if pattern.search(line):
                        offenders.append(f"{relative}:{number} names {what}: {line.strip()[:120]}")
        console.info(f"scanned {scanned} files; documented-origin files allowed to name the original: {len(ALLOWED)}")
        check(f"none of {scanned} source/config/test files references the original program", not offenders, offenders)
        csproj = (ROOT / "src" / "StreamDownloader.McpHost" / "StreamDownloader.McpHost.csproj").read_text(encoding="utf-8")
        check("the project references no COM interop and vendors no lib folder",
              "<Reference " not in csproj and not (ROOT / "src" / "StreamDownloader.McpHost" / "lib").exists())
        spec = preflight.spec()
        check("Stream-Downloader.json launches Stream-Downloader's own executable",
              Path(spec["command"]).resolve() == executable.resolve() and spec.get("args") == [], spec["command"])

        console.section("published binary")
        published = sorted(p.name for p in executable.parent.iterdir())
        check("the publish folder holds only Stream-Downloader's own files",
              published == sorted(["StreamDownloader.McpHost.exe", "appsettings.json", "manifest.json"]), published)
        blob = executable.read_bytes()
        traces = [marker for marker in BINARY_MARKERS
                  if marker.encode("utf-8") in blob or marker.encode("utf-16-le") in blob]
        console.info(f"searched {len(blob):,} bytes for {', '.join(BINARY_MARKERS)} (UTF-8 and UTF-16)")
        check("the self-contained executable carries no trace of the original or its COM interop", not traces, traces)
        deployed = json.loads((executable.parent / "appsettings.json").read_text(encoding="utf-8-sig"))
        section = deployed.get("StreamDownloader", {})
        check("deployed settings use Stream-Downloader's own section, state and tools directories",
              list(deployed) == ["StreamDownloader"]
              and section.get("StateDirectory") == r"%LOCALAPPDATA%\Stream-Downloader\state"
              and section.get("ToolsDirectory") == r"%LOCALAPPDATA%\Stream-Downloader\tools", section)

        console.section("running server")
        client = McpClient(run, executable)
        try:
            tools = sorted(t["name"] for t in client.request("tools/list", {})["tools"])
            caps = client.call("stream_capabilities")["data"]
        finally:
            client.close()
        check("the live server identifies itself as Stream-Downloader with its own six tools",
              client.initialize["serverInfo"]["name"] == "Stream-Downloader" and tools == sorted(EXPECTED_TOOLS), tools)
        yt_dlp = Path(caps["siteExtraction"]["ytDlpPath"] or "")
        check("the server runs its own yt-dlp from its own tools directory", yt_dlp.parent == TOOLS_DIRECTORY, yt_dlp)
        check("no reported path points into another program's folders",
              not any(FORBIDDEN["the original product folders"].search(str(value)) or FORBIDDEN["the original source folder"].search(str(value))
                      for value in [caps["stateDirectory"], *caps["allowedRoots"], caps["siteExtraction"]["ytDlpPath"]]),
              caps["allowedRoots"])
    except preflight.PreflightFailure as exc:
        exit_code = exc.code
        console.fail(f"PREFLIGHT {exc}")
    finally:
        failed = [r for r in results if not r["passed"]]
        if exit_code == preflight.EXIT_PASS and failed:
            exit_code = preflight.EXIT_TEST_FAILURE
        photo, evidence = None, None
        try:
            evidence = Tlamatini().console_evidence(run / "photos", "decoupling.png", console.title)
            photo = evidence["path"] if evidence["verified"] else None
        except TlamatiniMissing as exc:
            console.warn(str(exc))
        if photo:
            console.info(f"photo (Shoter; Windower brought this window forward, confirmed in front): {photo}")
        else:
            console.warn(f"photo not confirmed as evidence: {evidence}")
            if exit_code == preflight.EXIT_PASS:
                exit_code = preflight.EXIT_EVIDENCE_INCOMPLETE
        console.banner(f"{'PASS' if exit_code == 0 else 'FAIL'} - {len(results) - len(failed)}/{len(results)} decoupling checks",
                       f"Evidence retained: {run}")
        (run / "results.json").write_text(json.dumps(dict(suite="decoupling", exitCode=exit_code, photo=photo, evidence=evidence,
                                                          checks=results), indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
