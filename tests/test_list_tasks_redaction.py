"""Tests for redaction in ListTasks responses — gates CRIT-02 (a2a-review-20260602).

The list-tasks endpoint must return redacted responses (filter_outbound applied).
A regression that returns task.response unredacted leaks credentials and API keys
to anyone with list-tasks access.
"""
import pytest

from hermes_agent_a2a.server import _build_task_list_item, _PendingTask


def _make_pending_task(task_id: str, response, context_id: str = "ctx-1") -> _PendingTask:
    """Build a _PendingTask with a given response (str or None)."""
    task = _PendingTask(task_id=task_id, text="hello", metadata={}, context_id=context_id)
    task.response = response
    return task


class TestListTasksRedaction:
    """filter_outbound must be applied to task responses in list-tasks output."""

    def test_response_with_openai_key_is_redacted(self):
        """An sk-... key in the response must be replaced with [REDACTED]."""
        task = _make_pending_task(
            task_id="redact-1",
            response="Here is the key: sk-abcdefghijklmnopqrstuvwxyz1234",
        )
        item = _build_task_list_item(task, state="completed")
        item_str = str(item)
        assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in item_str, (
            f"OpenAI API key leaked in list-tasks response: {item}"
        )
        assert "[REDACTED]" in item_str, f"Redaction marker missing from item: {item}"

    def test_response_with_github_pat_is_redacted(self):
        """A ghp_... token in the response must be replaced with [REDACTED]."""
        task = _make_pending_task(
            task_id="redact-2",
            response="Token: ghp_abcdefghijklmnopqrstuvwxyz1234",
        )
        item = _build_task_list_item(task, state="completed")
        item_str = str(item)
        assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in item_str, (
            f"GitHub PAT leaked in list-tasks response: {item}"
        )
        assert "[REDACTED]" in item_str

    def test_response_with_slack_bot_token_is_redacted(self):
        """An xoxb-... token in the response must be replaced with [REDACTED]."""
        task = _make_pending_task(
            task_id="redact-3",
            response="Slack: xoxb-1234567890-abcdefghijklmn",
        )
        item = _build_task_list_item(task, state="completed")
        item_str = str(item)
        assert "xoxb-1234567890-abcdefghijklmn" not in item_str, (
            f"Slack bot token leaked in list-tasks response: {item}"
        )
        assert "[REDACTED]" in item_str

    def test_response_with_credential_assignment_is_redacted(self):
        """`api_key=...` / `password=...` patterns must be redacted."""
        task = _make_pending_task(
            task_id="redact-4",
            response="Config dump: api_key=secretvalue123 password=hunter2",
        )
        item = _build_task_list_item(task, state="completed")
        item_str = str(item)
        assert "secretvalue123" not in item_str, (
            f"api_key value leaked: {item}"
        )
        assert "hunter2" not in item_str, (
            f"password value leaked: {item}"
        )
        assert "[REDACTED]" in item_str

    def test_response_without_secrets_passes_through(self):
        """A clean response with no sensitive patterns is unchanged."""
        clean = "This is a normal response with no credentials."
        task = _make_pending_task(task_id="redact-5", response=clean)
        item = _build_task_list_item(task, state="completed")
        item_str = str(item)
        assert clean in item_str, f"Clean response was unexpectedly modified: {item}"

    def test_response_none_is_handled(self):
        """A task with response=None should not crash the builder."""
        task = _make_pending_task(task_id="redact-6", response=None)
        # Should not raise
        item = _build_task_list_item(task, state="completed")
        assert isinstance(item, dict)
