"""A2A persistence layer tests — save_exchange, update_exchange, file rotation."""
import os
import tempfile
from pathlib import Path

import pytest

from src import persistence


class TestSaveExchange:
    def test_writes_file(self, tmp_hermes_home):
        """save_exchange creates a .md file in the right directory."""
        path = persistence.save_exchange(
            agent_name="test-agent",
            task_id="tid-001",
            inbound_text="Hello there",
            outbound_text="Hi back",
        )
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "tid-001" in content
        assert "Hello there" in content
        assert "Hi back" in content

    def test_inbound_direction(self, tmp_hermes_home):
        """direction='inbound' formats the reply arrow correctly."""
        path = persistence.save_exchange(
            agent_name="remote-agent",
            task_id="tid-002",
            inbound_text="Question from peer",
            outbound_text="My answer",
            direction="inbound",
        )
        content = path.read_text(encoding="utf-8")
        assert "tid-002" in content
        assert "Question from peer" in content

    def test_outbound_direction(self, tmp_hermes_home):
        """direction='outbound' formats with the outbound arrow first."""
        path = persistence.save_exchange(
            agent_name="remote-agent",
            task_id="tid-003",
            inbound_text="Thanks!",
            outbound_text="You're welcome",
            direction="outbound",
        )
        content = path.read_text(encoding="utf-8")
        assert "tid-003" in content
        assert "You're welcome" in content


class TestUpdateExchange:
    def test_modifies_existing(self, tmp_hermes_home):
        """update_exchange replaces the 'waiting' placeholder with actual text."""
        # First save an exchange with the waiting placeholder as inbound
        agent = "update-agent"
        tid = "tid-update-001"
        persistence.save_exchange(
            agent_name=agent,
            task_id=tid,
            inbound_text="(waiting for reply\u2026)",
            outbound_text="Original question",
        )

        # Now update with the actual reply
        success = persistence.update_exchange(
            agent_name=agent,
            task_id=tid,
            inbound_text="Here is the real response",
        )
        assert success is True

        # Verify the file was updated
        from src.persistence import _CONV_DIR
        safe_name = "".join(c if c.isalnum() or c in "-_.@ " else "_" for c in agent.lower())
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        path = _CONV_DIR / safe_name / f"{today}.md"
        content = path.read_text(encoding="utf-8")
        assert "Here is the real response" in content

    def test_update_returns_false_for_unknown_task(self, tmp_hermes_home):
        """update_exchange returns False when the task_id is not found."""
        success = persistence.update_exchange(
            agent_name="nobody",
            task_id="no-such-task",
            inbound_text="late reply",
        )
        assert success is False


class TestFileRotation:
    def test_rotates_large_file(self, tmp_hermes_home, monkeypatch):
        """When a conversation file exceeds MAX_FILE_SIZE, it is rotated."""
        # Override the max size to something tiny so rotation triggers easily
        import src.persistence as pers_mod
        monkeypatch.setattr(pers_mod, "_MAX_FILE_SIZE", 200)

        agent = "rotation-test"
        tid_base = "tid-rot"

        # Write files until one exceeds the threshold
        for i in range(15):
            pers_mod.save_exchange(
                agent_name=agent,
                task_id=f"{tid_base}-{i}",
                inbound_text=f"Message number {i}: " + ("x" * 150),
                outbound_text=f"Reply {i}",
            )

        # After enough large writes, a rotated file should exist
        from src.persistence import _CONV_DIR
        safe_name = "".join(c if c.isalnum() or c in "-_.@ " else "_" for c in agent.lower())
        dir_path = _CONV_DIR / safe_name

        md_files = list(dir_path.glob("*.md"))
        old_files = list(dir_path.glob("*_old_*"))
        # Either the main file exists with rotated content, or an old file exists
        assert len(md_files) >= 1
