"""T1-1c — Verify all 11 push notification models are importable from a2a_spec.

Run with: pytest tests/test_push_schema_exports.py -v
"""
import pytest


def test_all_11_push_models_importable_from_a2a_spec():
    """All 11 push notification models must be importable directly from hermes_agent_a2a.a2a_spec."""
    from hermes_agent_a2a.a2a_spec import (
        AuthenticationInfo,
        TaskPushNotificationConfig,
        TaskPushNotificationConfigList,
        CreateTaskPushNotificationConfigRequest,
        CreateTaskPushNotificationConfigResponse,
        GetTaskPushNotificationConfigRequest,
        GetTaskPushNotificationConfigResponse,
        ListTaskPushNotificationConfigsRequest,
        ListTaskPushNotificationConfigsResponse,
        DeleteTaskPushNotificationConfigRequest,
        DeleteTaskPushNotificationConfigResponse,
    )
    # Sanity-check: each is a class
    assert isinstance(AuthenticationInfo, type)
    assert isinstance(TaskPushNotificationConfig, type)
    assert isinstance(TaskPushNotificationConfigList, type)
    assert isinstance(CreateTaskPushNotificationConfigRequest, type)
    assert isinstance(CreateTaskPushNotificationConfigResponse, type)
    assert isinstance(GetTaskPushNotificationConfigRequest, type)
    assert isinstance(GetTaskPushNotificationConfigResponse, type)
    assert isinstance(ListTaskPushNotificationConfigsRequest, type)
    assert isinstance(ListTaskPushNotificationConfigsResponse, type)
    assert isinstance(DeleteTaskPushNotificationConfigRequest, type)
    assert isinstance(DeleteTaskPushNotificationConfigResponse, type)


def test_push_models_importable_from_a2a_spec_push():
    """TaskPushNotificationConfig must also be importable from hermes_agent_a2a.a2a_spec.push."""
    from hermes_agent_a2a.a2a_spec.push import TaskPushNotificationConfig

    assert isinstance(TaskPushNotificationConfig, type)


def test_no_schemas_dict_uses_inline_push_config():
    """schemas.py must not contain a duplicate inline push config dict.

    If a top-level PUSH_* dict exists, it should reference the class, not redefine it.
    This test documents the expected state: no PUSH_CONFIG or similar dict at module level.
    """
    import hermes_agent_a2a.schemas as schemas

    names = dir(schemas)
    push_dicts = [n for n in names if n.startswith("PUSH") or "push" in n.lower()]
    assert push_dicts == [], (
        f"Unexpected push-related dicts found in schemas.py: {push_dicts}. "
        "Push config schemas should use TaskPushNotificationConfig from a2a_spec."
    )