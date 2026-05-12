"""pytest configuration and shared fixtures for HermesA2A v3 tests."""
import os
import pytest


@pytest.fixture(autouse=True)
def clean_hermes_home_env(monkeypatch):
    """Ensure HERMES_HOME is set to the isolated dev path for every test."""
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-v3-dev")
    # Clear A2A_AGENT_NAME so path helpers start clean
    monkeypatch.delenv("A2A_AGENT_NAME", raising=False)
