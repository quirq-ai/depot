"""Compare two result directories' JUnit XML, ignoring what differs from run to run.

    junit_compare.py LOCAL_OUT CI_OUT

Times, timestamps and host names are dropped; everything else (suites, cases, outcomes,
messages) must be identical, file by file. Exit 1 with the differences, or print a summary.
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

VOLATILE = ("time", "timestamp", "hostname")


def normalized(path: Path) -> str:
    root = ET.parse(path).getroot()
    for el in root.iter():
        for attr in VOLATILE:
            el.attrib.pop(attr, None)
        if el.text is not None and not el.text.strip():
            el.text = None
        if el.tail is not None and not el.tail.strip():
            el.tail = None
    return ET.tostring(root, encoding="unicode")


def results(out: Path) -> dict[str, str]:
    return {str(p.relative_to(out)): normalized(p) for p in sorted((out / "junit").rglob("*.xml"))}


def main(argv: list[str]) -> int:
    local_dir, ci_dir = map(Path, argv)
    local, ci = results(local_dir), results(ci_dir)
    if not local:
        print(f"no JUnit XML under {local_dir}/junit", file=sys.stderr)
        return 1
    problems = [f"only in local: {n}" for n in sorted(set(local) - set(ci))]
    problems += [f"only in CI: {n}" for n in sorted(set(ci) - set(local))]
    problems += [f"differs: {n}" for n in sorted(set(local) & set(ci)) if local[n] != ci[n]]
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        return 1
    cases = sum(len(list(ET.fromstring(x).iter("testcase"))) for x in local.values())
    print(f"PASS: {len(local)} JUnit files, {cases} test cases, identical apart from timings")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
