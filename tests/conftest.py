"""pytest configuration and shared fixtures for HermesA2A v3 tests."""
import os
import pytest
import shutil
import tempfile


@pytest.fixture(autouse=True)
def clean_hermes_home_env(monkeypatch):
    """Ensure HERMES_HOME is isolated to a temp directory for every test."""
    old_home = os.environ.get("HERMES_HOME")
    tmp_home = tempfile.mkdtemp(prefix="hermes-v3-test-")
    profiles_dir = os.path.join(tmp_home, "profiles")
    os.makedirs(profiles_dir)

    monkeypatch.setenv("HERMES_HOME", tmp_home)
    monkeypatch.delenv("A2A_AGENT_NAME", raising=False)

    yield tmp_home

    monkeypatch.setenv("HERMES_HOME", old_home if old_home is not None else "")
    if old_home is None:
        os.environ.pop("HERMES_HOME", None)
    shutil.rmtree(tmp_home, ignore_errors=True)
