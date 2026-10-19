"""The packaging script: layout, pruning and the launcher (no real Python copy or pip)."""

from __future__ import annotations

import importlib.util
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_zip", ROOT / "packaging" / "build_zip.py")
assert _spec and _spec.loader
build_zip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_zip)


def fake_python(home: Path) -> Path:
    for rel in (
        "python.exe",
        "python312.dll",
        "vcruntime140.dll",
        "Lib/os.py",
        "Lib/json/__init__.py",
        "Lib/test/test_os.py",
        "Lib/tkinter/__init__.py",
        "Lib/site-packages/pip/__init__.py",
        "Lib/__pycache__/os.cpython-312.pyc",
        "DLLs/_sqlite3.pyd",
        "DLLs/_tkinter.pyd",
        "DLLs/tcl86t.dll",
        "tcl/tcl8.6/init.tcl",
        "Scripts/pip.exe",
    ):
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    return home


def test_zip_layout(tmp_path: Path) -> None:
    home = fake_python(tmp_path / "py")
    code = build_zip.main(
        [
            "--version", "v0.1.0-pilot",
            "--python-home", str(home),
            "--work", str(tmp_path / "work"),
            "--out", str(tmp_path / "dist"),
            "--skip-install",
            "--skip-self-test",
        ]
    )  # fmt: skip
    assert code == 0
    archive = tmp_path / "dist" / "Marcelorian-v0.1.0-pilot-windows.zip"
    names = set(zipfile.ZipFile(archive).namelist())
    for kept in (
        "Marcelorian/Start Marcelorian.bat",
        "Marcelorian/How to use Marcelorian.txt",
        "Marcelorian/python/python.exe",
        "Marcelorian/python/Lib/os.py",
        "Marcelorian/python/DLLs/_sqlite3.pyd",
        "Marcelorian/python/Lib/site-packages/marcelorian/app_main.py",
        "Marcelorian/python/Lib/site-packages/marcelorian/ui/templates/base.html",
        "Marcelorian/python/Lib/site-packages/marcelorian/db/alembic.ini",
    ):
        assert kept in names, kept
    for dropped in (
        "Lib/test/",
        "Lib/tkinter/",
        "site-packages/pip/",
        "_tkinter",
        "tcl",
        "Scripts/",
        ".pyc",
    ):
        assert not any(dropped in n for n in names), dropped
    digest = (archive.parent / (archive.name + ".sha256")).read_text()
    assert re.match(r"^[0-9a-f]{64}  Marcelorian-v0.1.0-pilot-windows.zip\n$", digest)
    launcher = zipfile.ZipFile(archive).read("Marcelorian/Start Marcelorian.bat").decode()
    assert "-I -m marcelorian" in launcher and "\r\n" in launcher


@pytest.mark.parametrize("bad", ["0.1", "v1", "v0.1.0 && calc", "latest"])
def test_version_label_is_validated(tmp_path: Path, bad: str) -> None:
    with pytest.raises(SystemExit):
        build_zip.main(["--version", bad, "--python-home", str(tmp_path), "--skip-install"])


def test_runtime_lock_matches_the_dev_lock() -> None:
    def pins(path: Path) -> dict[str, str]:
        found = re.findall(r"^([A-Za-z0-9_.-]+)==([^ ;\\\n]+)", path.read_text(), re.M)
        return {name.lower(): version for name, version in found}

    runtime = pins(ROOT / "requirements" / "runtime-windows.txt")
    lock = pins(ROOT / "requirements" / "lock.txt")
    assert runtime and all(lock.get(name) == version for name, version in runtime.items())
    assert "pytest" not in runtime and "anthropic" in runtime
