import pytest
import os
from pathlib import Path


@pytest.fixture
def tmp_vault_dir(tmp_path):
    """Temporary directory structure simulating a profile with a2a vault."""
    profile = tmp_path / "testprofile"
    vault_dir = profile / "a2a"
    vault_dir.mkdir(parents=True)
    return vault_dir
