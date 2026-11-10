"""Proves COMPARISON.md contains every line of both projects - exactly once, in order - and is current.

1. CURRENT: the generator rebuilds both documents in memory from today's files; any difference from
   the COMPARISON.md / COMPARISON.html on disk fails ("run tools/generate_comparison.py").
2. COMPLETE: the Markdown is parsed back. For every paired file, the line numbers in its section
   must be exactly 1..N on each side, and every decoded line must equal the file's line.
3. ACCOUNTED: every file of the original's managed set is either compared line by line or listed
   (binary/generated) with its hash; every Stream-Downloader file is compared.
4. VISIBLE: COMPARISON.html is opened in the real Chrome by Playwrighter; Windower puts it beside
   this console; Shoter takes the confirmed photo.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from harness import paths, preflight
from harness.console import Console
from harness.mcp_client import ROOT
from harness.tlamatini import Tlamatini, TlamatiniMissing

_PAIR = re.compile(r'^<!-- pair idm="(?P<idm>[^"]*)" sd="(?P<sd>[^"]*)" -->$')
_CODE = re.compile(r"^<code>(?P<body>.*)</code>$")


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_comparison", ROOT / "tools" / "generate_comparison.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decode(cell: str) -> str | None:
    cell = cell.strip()
    if not cell:
        return None
    match = _CODE.match(cell)
    if not match:
        raise AssertionError(f"unexpected code cell: {cell[:80]}")
    return html.unescape(match.group("body")).replace("\xa0", " ")


def parse(markdown: str) -> dict[tuple[str, str], list[tuple[int | None, str | None, int | None, str | None]]]:
    sections: dict = {}
    current = None
    for line in markdown.split("\n"):
        pair = _PAIR.match(line)
        if pair:
            current = (pair.group("idm"), pair.group("sd"))
            sections[current] = []
            continue
        if current and line.startswith("| ") and "<code>" in line or (current and re.match(r"^\| \d* \| (<code>|\|)", line or "")):
            cells = line.split("|")[1:-1]
            if len(cells) != 6:
                raise AssertionError(f"row with {len(cells)} cells: {line[:120]}")
            idm_no, idm_text, _, sd_no, sd_text, _ = cells
            sections[current].append((int(idm_no) if idm_no.strip() else None, decode(idm_text),
                                      int(sd_no) if sd_no.strip() else None, decode(sd_text)))
    return sections


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", default=None, help="evidence directory inside test-artifacts")
    args = parser.parse_args()
    try:
        run = paths.evidence_dir(args.run, "comparison")
    except paths.UnsafePath as exc:
        print(f"UNSAFE_PATH ({preflight.EXIT_UNSAFE_PATH}): {exc}", flush=True)
        return preflight.EXIT_UNSAFE_PATH
    console = Console(run / "live.log", f"Stream-Downloader comparison {run.name}")
    console.banner("COMPARISON.md - every line of IDM-MCP Core beside Stream-Downloader",
                   "Angela, this proves the comparison is complete, exact and current.")
    results: list[dict] = []
    exit_code = preflight.EXIT_PASS

    def check(name: str, condition, detail=None) -> None:
        results.append(dict(name=name, passed=bool(condition), detail=None if detail is None else str(detail)[:1500]))
        (console.ok if condition else console.fail)(name + ("" if condition or detail is None else f": {str(detail)[:400]}"))

    try:
        generator = load_generator()
        idm_root = generator.DEFAULT_IDM_ROOT
        if not idm_root.is_dir():
            raise preflight.PreflightFailure(preflight.EXIT_ORIGINAL_SOURCE_MISSING, f"{idm_root} is not available to compare against")
        console.step(f"rebuilding both documents in memory from {idm_root} and this repository")
        markdown, page, meta = generator.build(idm_root)
        on_disk_md = (ROOT / "COMPARISON.md").read_text(encoding="utf-8") if (ROOT / "COMPARISON.md").is_file() else ""
        on_disk_html = (ROOT / "COMPARISON.html").read_text(encoding="utf-8") if (ROOT / "COMPARISON.html").is_file() else ""
        check("COMPARISON.md is current with every file of both projects", on_disk_md == markdown,
              "stale - run: python tools/generate_comparison.py")
        check("COMPARISON.html is current too", on_disk_html == page, "stale - run: python tools/generate_comparison.py")
        t = meta["totals"]
        console.info(f"IDM lines {t['idmLines']:,} | Stream-Downloader lines {t['sdLines']:,} | = {t['identical']:,}  "
                     f"≈ {t['renamed']:,}  ✎ {t['modified']:,}  − {t['removed']:,}  + {t['added']:,}")

        sections = parse(on_disk_md)
        idm_checked = sd_checked = 0
        problems = []
        for (idm, sd), rows in sections.items():
            for side, relative, base, index in (("IDM", idm, idm_root, (0, 1)), ("Stream-Downloader", sd, ROOT, (2, 3))):
                if not relative:
                    continue
                lines = generator.read_lines(base / relative)
                numbers = [row[index[0]] for row in rows if row[index[0]] is not None]
                texts = [row[index[1]] for row in rows if row[index[0]] is not None]
                if numbers != list(range(1, len(lines) + 1)):
                    problems.append(f"{side} {relative}: line numbers are not exactly 1..{len(lines)}")
                elif texts != [line.replace("\xa0", " ") for line in lines]:
                    first = next(i for i, (a, b) in enumerate(zip(texts, lines)) if a != b.replace("\xa0", " "))
                    problems.append(f"{side} {relative}: line {first + 1} differs from the file")
                if side == "IDM":
                    idm_checked += len(lines)
                else:
                    sd_checked += len(lines)
        check(f"every one of {idm_checked:,} IDM lines appears exactly once, in order, verbatim", not problems and idm_checked == t["idmLines"], problems)
        check(f"every one of {sd_checked:,} Stream-Downloader lines appears exactly once, in order, verbatim",
              not problems and sd_checked == t["sdLines"], problems)
        idm_files = generator.managed_files(idm_root, generator.MANAGED_FOLDERS_IDM, set())
        listed = {p for p, _, _ in generator.NOT_LINE_COMPARED if p}
        compared = {idm for idm, _ in sections if idm}
        unaccounted = [f for f in idm_files if f not in compared and f not in listed]
        check(f"all {len(idm_files)} files of the original are compared or listed with their hash", not unaccounted, unaccounted)
        sd_files = generator.managed_files(ROOT, generator.MANAGED_FOLDERS_SD, set(generator.OUTPUTS))
        sd_compared = {sd for _, sd in sections if sd} | {s for _, s, _ in generator.NOT_LINE_COMPARED if s}
        missing_sd = [f for f in sd_files if f not in sd_compared]
        check(f"all {len(sd_files)} Stream-Downloader files are compared", not missing_sd, missing_sd)
        notes = [row for rows in sections.values() for row in rows]
        console.info(f"{len(sections)} file sections, {len(notes):,} aligned rows")

        # ---- the eye: the document itself, open in Chrome
        tlamatini = Tlamatini()
        token = uuid.uuid4().hex[:10]
        box: dict = {}

        def drive() -> None:
            try:
                box["result"] = tlamatini.browse(
                    [{"action": "goto", "url": (ROOT / "COMPARISON.html").resolve().as_uri()},
                     {"action": "evaluate", "expression": f"() => {{ document.title = 'SHOWN {token} - ' + document.title; return document.querySelectorAll('tbody tr').length; }}", "name": "rows"},
                     {"action": "wait", "ms": 12000}],
                    run / "_playwrighter", (ROOT / "COMPARISON.html").resolve().as_uri(), hold_open_seconds=1, viewport=(820, 900))
            except Exception as exc:
                box["error"] = f"{type(exc).__name__}: {exc}"

        driver = threading.Thread(target=drive, daemon=True)
        driver.start()
        evidence = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and driver.is_alive():
            if f"SHOWN {token}" in tlamatini.windows(token, run / "photos"):
                tlamatini.arrange(console.title, "left", run / "photos")
                tlamatini.arrange(token, "right", run / "photos")
                evidence = tlamatini.evidence(run / "photos", "comparison.png", token)
                break
            time.sleep(1.0)
        driver.join(timeout=60)
        rendered = int((box.get("result") or {}).get("extracted", {}).get("rows") or 0)
        check(f"Chrome renders COMPARISON.html with all {rendered:,} rows (Playwrighter, headed)", rendered >= len(notes), box.get("error"))
        if evidence and evidence["verified"]:
            console.info(f"photo (Shoter; Windower put the console and Chrome in front, confirmed): {evidence['path']}")
        else:
            console.warn(f"photo not confirmed as evidence: {evidence}")
            exit_code = preflight.EXIT_EVIDENCE_INCOMPLETE
    except preflight.PreflightFailure as exc:
        exit_code = exc.code
        console.fail(f"PREFLIGHT {exc}")
    except TlamatiniMissing as exc:
        exit_code = preflight.EXIT_EVIDENCE_INCOMPLETE
        console.warn(str(exc))
    except Exception as exc:
        results.append(dict(name="suite", passed=False, detail=f"{type(exc).__name__}: {exc}"))
        console.fail(f"{type(exc).__name__}: {exc}")
    finally:
        failed = [r for r in results if not r["passed"]]
        if failed:
            exit_code = preflight.EXIT_TEST_FAILURE
        console.banner(f"{'PASS' if exit_code == 0 else 'FAIL' if failed else 'PASS (evidence incomplete)'} - "
                       f"{len(results) - len(failed)}/{len(results)} checks", f"Evidence retained: {run}")
        (run / "results.json").write_text(json.dumps(dict(suite="comparison", exitCode=exit_code, checks=results), indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
