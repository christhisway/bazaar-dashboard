"""Setup and uninstall, against a throwaway client and a throwaway site-packages.

``PYTHONUSERBASE`` points the interpreter's user site-packages into ``tmp_path``
and ``$BAZAAR_DASHBOARD_TAP_CONFIG`` moves the tap's config there too, so these
tests install a real hook and start real processes without touching the
machine's own Python.
"""

import json
import os
import site
import subprocess
import sys

import pytest

from bazaar_dashboard import install, settings

pytestmark = pytest.mark.skipif(not site.ENABLE_USER_SITE, reason="needs user site-packages (not a venv)")

CLIENT_MAIN = """
import bazaar_dashboard_tap
print("tapped" if bazaar_dashboard_tap.active() else "not tapped")
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A client directory, a private user site, and a private tap config."""
    client = tmp_path / "client"
    (client / "fakeclient").mkdir(parents=True)
    (client / "fakeclient" / "__init__.py").write_text("")
    (client / "fakeclient" / "__main__.py").write_text(CLIENT_MAIN)
    (client / "fakeclient" / "other.py").write_text(CLIENT_MAIN)

    config = tmp_path / "tap-config.json"
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "userbase"))
    monkeypatch.setenv("BAZAAR_DASHBOARD_TAP_CONFIG", str(config))
    monkeypatch.setattr(settings, "TAP_CONFIG", config)
    usersite = subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                              capture_output=True, text=True, check=True).stdout.strip()
    os.makedirs(usersite, exist_ok=True)
    return client, usersite, config


def run(client_dir, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=client_dir, capture_output=True, text=True, timeout=30)


def test_setup_taps_the_client_command_and_nothing_else(sandbox):
    client, usersite, config = sandbox
    install.setup(str(client), module="fakeclient", python=sys.executable, install=False, site_dir=usersite,
                  tap_port=9, say=lambda _: None)

    assert run(client, "-m", "fakeclient").stdout.strip() == "tapped"
    assert run(client, "-m", "fakeclient.other").stdout.strip() == "not tapped"
    assert run(client.parent, "-c", "import sys; sys.path.insert(0, 'client'); import runpy; "
                                     "runpy.run_module('fakeclient', run_name='__main__')").stdout.strip() == "not tapped"
    written = json.loads(config.read_text())
    assert written["port"] == 9
    assert written["clients"] == [{"dir": str(client.resolve()), "module": "fakeclient", "python": sys.executable}]


def test_setup_again_replaces_the_client_rather_than_adding_a_second(sandbox):
    client, usersite, config = sandbox
    for _ in range(2):
        install.setup(str(client), module="fakeclient", python=sys.executable, install=False, site_dir=usersite,
                      say=lambda _: None)
    assert len(json.loads(config.read_text())["clients"]) == 1


def test_uninstall_removes_the_hook_and_the_config(sandbox):
    client, usersite, config = sandbox
    install.setup(str(client), module="fakeclient", python=sys.executable, install=False, site_dir=usersite,
                  say=lambda _: None)
    install.uninstall(say=lambda _: None)
    assert not os.path.exists(os.path.join(usersite, settings.PTH_NAME))
    assert not config.exists()
    assert run(client, "-m", "fakeclient").returncode != 0  # the tap is no longer importable at all


def test_a_hook_left_behind_by_a_deleted_dashboard_is_silent(sandbox, tmp_path):
    client, usersite, _ = sandbox
    with open(os.path.join(usersite, settings.PTH_NAME), "w") as handle:
        handle.write(install.pth_contents(tmp_path / "gone" / "tap"))
    done = run(client, "-c", "print('fine')")
    assert (done.stdout.strip(), done.stderr) == ("fine", "")


def test_setup_refuses_a_directory_where_the_start_command_would_find_nothing(sandbox):
    client, usersite, _ = sandbox
    with pytest.raises(install.SetupError, match="python -m bazaar"):
        install.setup(str(client), module="bazaar", python=sys.executable, install=False, site_dir=usersite,
                      say=lambda _: None)


def test_a_failed_pip_install_says_what_to_do(monkeypatch):
    def pip_fails(command, **_):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(install, "missing_requirements", lambda: ["protobuf"])
    monkeypatch.setattr(install.subprocess, "run", pip_fails)
    with pytest.raises(install.SetupError, match="Install them yourself"):
        install.ensure_requirements(True, say=lambda _: None)


def test_a_virtualenv_in_the_client_is_the_python_it_runs_under(tmp_path):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    assert install.find_client_python(tmp_path) == str(venv_python)
