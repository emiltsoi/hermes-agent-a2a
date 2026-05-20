"""Tests for TaskState enum and state groupings per Google A2A v1.0 spec."""

import pytest

from hermes_agent_a2a.a2a_spec.tasks import (
    TaskState,
    ACTIVE_STATES,
    TERMINAL_STATES,
    AUTH_STATES,
)


class TestTaskStateEnum:
    """TaskState enum must contain all and only the spec-canonical states."""

    @pytest.mark.parametrize(
        "expected",
        [
            "submitted",
            "working",
            "input_required",
            "auth_required",
            "completed",
            "failed",
            "canceled",
            "rejected",
        ],
    )
    def test_enum_has_spec_canonical_state(self, expected):
        assert expected in [s.value for s in TaskState]

    def test_enum_member_count(self):
        assert len(TaskState) == 8

    def test_enum_values_are_lowercase_strings(self):
        for member in TaskState:
            assert isinstance(member.value, str)
            assert member.value == member.value.lower()

    def test_authenticated_not_a_task_state(self):
        """'authenticated' is an auth sub-state, not a canonical TaskState."""
        assert "authenticated" not in [s.value for s in TaskState]

    def test_rejected_is_a_task_state(self):
        """'rejected' is a canonical TaskState (terminal auth state)."""
        assert "rejected" in [s.value for s in TaskState]


class TestStateGroupings:
    """ACTIVE_STATES, TERMINAL_STATES, AUTH_STATES must match the spec."""

    def test_active_states(self):
        assert ACTIVE_STATES == {"submitted", "working", "input_required", "auth_required"}

    def test_terminal_states(self):
        assert TERMINAL_STATES == {"completed", "failed", "canceled", "rejected"}

    def test_auth_states(self):
        assert AUTH_STATES == {"auth_required", "rejected"}

    def test_active_and_terminal_are_disjoint(self):
        assert ACTIVE_STATES.isdisjoint(TERMINAL_STATES)

    def test_auth_required_in_both_active_and_auth(self):
        assert "auth_required" in ACTIVE_STATES
        assert "auth_required" in AUTH_STATES

    def test_rejected_in_auth_and_terminal(self):
        assert "rejected" in AUTH_STATES
        assert "rejected" in TERMINAL_STATES
