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


@pytest.fixture
def tmp_vault_dir(tmp_path):
    """Temporary directory structure simulating a profile with a2a vault."""
    profile = tmp_path / "testprofile"
    vault_dir = profile / "a2a"
    vault_dir.mkdir(parents=True)
    return vault_dir


@pytest.fixture
def tmp_hermes_home():
    """Return the current HERMES_HOME temp directory path.

    The autouse clean_hermes_home_env fixture sets this before every test.
    """
    return os.environ.get("HERMES_HOME")
