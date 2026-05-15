# Changelog

All notable changes to this project will be documented in this file.

## [3.1.3] - 2026-05-15

### Security Fixes (CRITICAL)
- CR-1: Simplified resolve_agent to return only safe fields (name, a2a_url, description, role)
  - Removed _strip_secrets function entirely
  - Transports with auth secrets are never included in response
  - Simpler and more secure than stripping secrets from full dict

### Bug Fixes (HIGH)
- HIGH #6: Replaced queue traversal with atomic counter in TaskQueue
  - pending_count() now counter-based: max(0, _enqueue_count - _complete_count - _cancel_count)
  - No singleton access, no re-entrancy path
  - Fixed counter increment timing to prevent drift on queue overflow eviction

### Bug Fixes (MEDIUM)
- MEDIUM #1: Fixed update_exchange placeholder matching in persistence.py
- MEDIUM #2: Fixed queue overflow race condition in server.py
- MEDIUM #3: Fixed metrics logger idempotency in runtime_state.py
- MEDIUM #4: Fixed to_dict mutability in runtime_state.py
- MEDIUM #5: Fixed persistence.py atomicity
- MEDIUM #7: Fixed DEFAULT_PORT collision detection with retry logic in plugin.py
- MEDIUM #8: Fixed A2A_WEBHOOK_SECRET fallback to WEBHOOK_SECRET with warning
- MEDIUM #9: Fixed path traversal prevention for card_path in webhook agent card retrieval
- MEDIUM #10: Fixed AuditLogger exception logging to use logger.warning
- MEDIUM #11: Fixed TOCTOU race condition in audit log rotation
- MEDIUM #12: Disabled email pattern redaction in filter_outbound (too broad)
- MEDIUM #13: Fixed regex capture group comment in hooks.py

### Code Quality (LOW)
- LOW #1: Added sort_keys=True to HMAC json.dumps for canonical signatures
- LOW #2: Removed dead proc.wait() call and fixed SyntaxError in _handle_call_mode2
- LOW #3: Removed redundant cleanup_zombie_processes() call in finally block
- LOW #4: Removed redundant json import from inline import statement
- LOW #5: Removed redundant logging import inside handle_send_session_message
- LOW #6: Added comment explaining GIL guarantee for double-checked locking
- LOW #7: Removed module-level task_queue variable that shadowed TaskQueue class
- LOW #8: Removed unused user_task parameter from handle_help() and handle_list()
- LOW #9: Added warning when hermes_cli.__version import fails
- LOW #10: Changed msg_id from task_id[:12] to full task_id (UUID truncation)
- LOW #11: Added comment documenting daemon thread metrics loss limitation
- LOW #13: Removed self-import in _get_queue_depth method
- LOW #14: Added force parameter to set_runtime_callbacks() to prevent overwriting on reload

### Tests
- All 62 tests pass
- Added tests for _derive_hermes_home fallback and error raising scenarios

## [3.1.2] - 2026-05-15

### Bug Fixes (HIGH)
- HIGH #6: Replace queue traversal with atomic counter in TaskQueue

## [3.1.1] - Previous Release

## [3.1.0] - Previous Release

## [3.0.0] - Previous Release

## [2.0.1] - Previous Release

## [2.0.0] - Previous Release
