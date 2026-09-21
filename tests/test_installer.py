"""Checks for install.sh. The quick ones run everywhere; the full install test needs root,
network access to PyPI and opt-in: RUN_INSTALLER_TESTS=1 sudo -E python -m pytest tests/test_installer.py"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "install.sh")


def run(*args, **kw):
    return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, timeout=300, **kw)


def test_syntax():
    assert subprocess.run(["bash", "-n", SCRIPT]).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_help_lists_commands_and_options():
    r = run("--help")
    assert r.returncode == 0
    for word in ("install", "update", "uninstall", "--dir", "--no-service", "--yes", "--purge", "--ref"):
        assert word in r.stdout


@pytest.mark.parametrize("args,msg", [
    (["--bogus"], "unknown option"),
    (["--dir", "relative/path"], "absolute"),
    (["--dir", "/"], "refusing"),
    (["--dir"], "needs a value"),
])
def test_bad_arguments_are_refused_before_doing_anything(args, msg):
    r = run(*args)
    assert r.returncode != 0 and msg in r.stderr


@pytest.mark.skipif(not os.environ.get("RUN_INSTALLER_TESTS") or os.geteuid() != 0,
                    reason="set RUN_INSTALLER_TESTS=1 and run as root")
def test_full_local_install_update_and_purge(tmp_path):
    dest = str(tmp_path / "inst")
    r = run("--from-dir", ROOT, "--dir", dest, "--no-service", "--yes")
    assert r.returncode == 0, r.stderr
    assert os.path.exists(os.path.join(dest, "venv", "bin", "python"))
    assert os.path.exists(os.path.join(dest, "config.json"))            # created from the example
    out = subprocess.run([os.path.join(dest, "venv", "bin", "python"),
                          os.path.join(dest, "spool_cam_scanner.py"), "--version"], capture_output=True, text=True)
    assert out.stdout.startswith("spool_cam_scanner ")
    assert run("update", "--from-dir", ROOT, "--dir", dest, "--no-service", "--yes").returncode == 0
    assert run("uninstall", "--dir", dest, "--yes", "--purge").returncode == 0
    assert not os.path.exists(dest)
