"""Build the no-install Windows zip for testers (owner decision N5).

Run on a Windows machine (the GitHub ``Package`` workflow) with the pinned Python 3.12:

    python packaging/build_zip.py --version v0.1.0-pilot

Layout of ``Marcelorian-<version>-windows.zip``::

    Marcelorian/
      Start Marcelorian.bat      double-click to start
      How to use Marcelorian.txt the tester guide (docs/TESTER_GUIDE.md)
      python/                    a private copy of Python with every dependency inside

* Python comes from the build machine's own installation (the workflow pins it with
  ``actions/setup-python``); nothing is downloaded outside the hash-checked lock.
* Dependencies are installed from ``requirements/runtime-windows.txt`` with
  ``--require-hashes`` and wheels only, straight into the private copy.
* The launcher runs Python in isolated mode (``-I``), so other Python installations or
  environment variables on the tester's computer cannot interfere.
* Before zipping, the private copy runs ``python -m marcelorian --self-test``.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"^v\d+\.\d+\.\d+(-[a-z0-9.]+)?$")
TOP = "Marcelorian"

#: Parts of the Python installation testers never need (relative to its home).
SKIP_DIRS = {
    "Doc",
    "Scripts",
    "Tools",
    "include",
    "libs",
    "tcl",
    "Lib/test",
    "Lib/idlelib",
    "Lib/tkinter",
    "Lib/turtledemo",
    "Lib/ensurepip",
    "Lib/venv",
}
SKIP_FILES = re.compile(r"(?i)^(_tkinter\.pyd|tcl\d+t?\.dll|tk\d+t?\.dll|.*\.pyc|.*\.pdb)$")

LAUNCHER = """@echo off
title Marcelorian
cd /d "%~dp0"
echo Starting Marcelorian. Keep this window open while you use it.
"%~dp0python\\python.exe" -I -m marcelorian %*
if errorlevel 1 (
  echo.
  echo Marcelorian stopped with a problem. Take a photo of this window for the program owner.
  pause
)
"""


def _ignore_python(home: Path) -> Callable[[str, list[str]], set[str]]:
    def ignore(directory: str, names: list[str]) -> set[str]:
        rel = Path(directory).relative_to(home).as_posix()
        skipped = set()
        for name in names:
            path = f"{rel}/{name}" if rel != "." else name
            if path in SKIP_DIRS or name == "__pycache__" or SKIP_FILES.match(name):
                skipped.add(name)
            elif path == "Lib/site-packages":
                skipped.add(name)  # recreated empty: only the locked packages go in
        return skipped

    return ignore


def copy_python(home: Path, target: Path) -> Path:
    shutil.copytree(home, target, ignore=_ignore_python(home))
    site = target / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    return site


def install_dependencies(site: Path) -> None:
    subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-compile",
            "--target",
            str(site),
            "-r",
            str(ROOT / "requirements" / "runtime-windows.txt"),
        ],
        check=True,
    )
    for junk in site.glob("*.dist-info/direct_url.json"):
        junk.unlink()


def copy_app(site: Path) -> None:
    shutil.copytree(
        ROOT / "src" / "marcelorian",
        site / "marcelorian",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def write_extras(folder: Path) -> None:
    (folder / "Start Marcelorian.bat").write_text(LAUNCHER.replace("\n", "\r\n"), encoding="ascii")
    guide = (ROOT / "docs" / "TESTER_GUIDE.md").read_text(encoding="utf-8")
    (folder / "How to use Marcelorian.txt").write_text(
        guide.replace("\n", "\r\n"), encoding="utf-8-sig"
    )


def self_test(folder: Path) -> None:
    python = folder / "python" / ("python.exe" if sys.platform == "win32" else "bin/python3")
    subprocess.run(  # noqa: S603 - the copy just built, fixed arguments, no shell
        [str(python), "-I", "-m", "marcelorian", "--self-test"], check=True
    )


def make_zip(folder: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, Path(TOP) / path.relative_to(folder))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_name(out.name + ".sha256").write_text(f"{digest}  {out.name}\n", encoding="ascii")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the no-install Windows zip.")
    parser.add_argument("--version", required=True, help="e.g. v0.1.0-pilot")
    parser.add_argument("--python-home", type=Path, default=Path(sys.base_prefix))
    parser.add_argument("--work", type=Path, default=ROOT / "build")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--skip-install", action="store_true", help="tests only")
    parser.add_argument("--skip-self-test", action="store_true", help="tests only")
    args = parser.parse_args(argv)
    if not VERSION.match(args.version):
        parser.error("version must look like v0.1.0 or v0.1.0-pilot")

    folder = args.work / TOP
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    site = copy_python(args.python_home, folder / "python")
    if not args.skip_install:
        install_dependencies(site)
    copy_app(site)
    write_extras(folder)
    if not args.skip_self_test:
        self_test(folder)
    archive = make_zip(folder, args.out / f"Marcelorian-{args.version}-windows.zip")
    print(f"Built {archive} ({archive.stat().st_size // (1024 * 1024)} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
