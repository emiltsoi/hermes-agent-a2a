"""Tests for F-A003/F-C001: Message.message_id REQUIRED field in build_task_send_payload.

Per Google A2A spec:
  message Message {
    string message_id = 1 [(google.api.field_behavior) = REQUIRED];
    ...
  }

build_task_send_payload() must include message_id (UUID) in the message dict.
"""
import uuid

import pytest

from hermes_agent_a2a.a2a_spec.tasks import build_task_send_payload


class TestBuildTaskSendPayloadMessageId:
    """Message.message_id must be present and valid in outgoing A2A Message objects."""

    def test_message_has_message_id_key(self):
        """build_task_send_payload() message dict must include message_id key."""
        payload = build_task_send_payload(
            task_id="test-task-1",
            message="hello",
            sender_name="test-sender",
        )
        message = payload["params"]["message"]
        assert "message_id" in message, (
            f"message_id is REQUIRED per A2A spec; got keys: {list(message.keys())}"
        )

    def test_message_id_is_valid_uuid(self):
        """message_id must be a valid UUID string."""
        payload = build_task_send_payload(
            task_id="test-task-2",
            message="hello",
            sender_name="test-sender",
        )
        message_id = payload["params"]["message"]["message_id"]
        # Should not raise ValueError
        parsed = uuid.UUID(message_id)
        assert isinstance(message_id, str), f"message_id must be string, got {type(message_id)}"
        assert str(parsed) == message_id, f"message_id must be valid UUID: {message_id}"

    def test_multiple_calls_produce_different_uuids(self):
        """Each call must generate a fresh UUID (not reused)."""
        payload1 = build_task_send_payload(
            task_id="test-task-3",
            message="hello",
            sender_name="test-sender",
        )
        payload2 = build_task_send_payload(
            task_id="test-task-3",
            message="hello",
            sender_name="test-sender",
        )
        id1 = payload1["params"]["message"]["message_id"]
        id2 = payload2["params"]["message"]["message_id"]
        assert id1 != id2, (
            f"Each call must produce a unique message_id; got duplicate: {id1}"
        )