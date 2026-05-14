# A2A Plugin Refactoring Plan

## Overview

This plan addresses the remaining gaps in the Hermes A2A plugin after the initial code review fixes. The approach prioritizes **tests first** to ensure we have a safety net before refactoring.

## Remaining Gaps

1. **Builtins hack for process state** (server.py) - Architectural issue, requires major refactor
2. **No retry logic for failed webhook delivery** in handle_send_session_message
3. **No monitoring/metrics** for queue depth, success rates, latency
4. **No tests for new code** - path derivation, validation, public API
5. **Limited error handling** - no webhook reachability validation, no echo disable option

## Refactoring Strategy

### Phase 1: Test Coverage (Safety Net)

**Goal:** Establish comprehensive test coverage before any refactoring.

#### 1.1 Unit Tests for New Functions

- **test-1:** Add tests for `_derive_hermes_home()` path derivation logic
  - Test with HERMES_HOME pointing to profile directory
  - Test with HERMES_HOME pointing to root directory
  - Test with HERMES_HOME pointing to non-standard paths
  - Test fallback to ~/.hermes when validation fails
  - Test error handling when no valid path exists

- **test-2:** Add tests for `_validate_agent_webhook_config()`
  - Test with valid webhook URL and secret
  - Test with missing webhook URL
  - Test with missing webhook secret
  - Test with empty/invalid configurations

- **test-3:** Add tests for TaskQueue public API methods
  - Test `get_task_metadata()` for pending tasks
  - Test `get_task_metadata()` for completed tasks
  - Test `get_task_metadata()` for non-existent tasks
  - Test `get_all_task_metadata()` returns all tasks
  - Test `get_processing_tasks()` returns correct list
  - Test `requeue_tasks()` re-queues unprocessed tasks

- **test-4:** Add tests for zombie process cleanup
  - Test `cleanup_zombie_processes()` removes finished processes
  - Test `cleanup_zombie_processes()` doesn't remove active processes
  - Test `cleanup_zombie_processes()` returns correct count

#### 1.2 Integration Tests

- **test-5:** Add integration tests for webhook routing scenarios
  - Test webhook delivery with valid configuration
  - Test webhook delivery with invalid configuration
  - Test webhook delivery with HMAC signature validation
  - Test webhook delivery failure handling

### Phase 2: Safe Refactoring

**Goal:** Refactor critical architectural issues with test coverage in place.

#### 2.1 Refactor Builtins Hack (High Priority)

**Current State:** Uses `builtins._hermes_a2a_runtime_state` for process-wide state sharing.

**Issues:**
- Not thread-safe across plugin reloads
- Hacky implementation using builtins namespace
- Difficult to test and mock

**Proposed Solution:**
- Create a proper singleton class `A2ARuntimeState` with thread-safe access
- Use module-level singleton instance instead of builtins
- Maintain backward compatibility if needed via adapter layer

**Implementation Steps:**
1. Create `A2ARuntimeState` class in new file `runtime_state.py`
2. Implement thread-safe singleton pattern with locks
3. Add methods for state access (get_server, set_server, get_queue, etc.)
4. Update `server.py` to use new runtime state class
5. Update `hooks.py` to use new runtime state class
6. Update `plugin.py` to use new runtime state class
7. Run all existing tests to ensure no breakage
8. Add tests for thread-safety and plugin reload scenarios

**Risk:** Medium - this touches core state management. Test coverage is critical.

#### 2.2 Add Retry Logic for Webhook Delivery (Medium Priority)

**Current State:** `handle_send_session_message` fails immediately if target webhook is unreachable.

**Proposed Solution:**
- Add retry logic with exponential backoff for target webhook delivery
- Make retry count and backoff configurable via env vars
- Log retry attempts for observability

**Implementation Steps:**
1. Extract webhook delivery logic into separate function
2. Add retry loop with exponential backoff
3. Add configurable retry count via `A2A_WEBHOOK_DELIVERY_RETRIES`
4. Add configurable backoff via `A2A_WEBHOOK_DELIVERY_BACKOFF`
5. Add logging for retry attempts
6. Add tests for retry behavior (success on retry, exhaustion)
7. Run integration tests

**Risk:** Low - isolated to webhook delivery function.

#### 2.3 Add Webhook Reachability Validation (Medium Priority)

**Current State:** No validation that target webhook URL is reachable before attempting delivery.

**Proposed Solution:**
- Add optional pre-flight check to validate webhook is reachable
- Make validation optional via env var to avoid latency overhead
- Return clear error if webhook is unreachable

**Implementation Steps:**
1. Add `_validate_webhook_reachable()` function
2. Add HEAD request to check if webhook is reachable
3. Make validation optional via `A2A_WEBHOOK_REACHABILITY_CHECK` env var
4. Add timeout for reachability check
5. Add tests for reachability validation
6. Update `handle_send_session_message` to call validation

**Risk:** Low - optional feature, backward compatible.

#### 2.4 Add Telegram Echo Disable Option (Low Priority)

**Current State:** Sender-side Telegram echo always attempts if credentials are available.

**Proposed Solution:**
- Add env var `A2A_DISABLE_SENDER_ECHO` to disable echo
- Skip echo attempt if disabled
- Document in README

**Implementation Steps:**
1. Check env var in `handle_send_session_message`
2. Skip echo logic if disabled
3. Add tests for echo enable/disable
4. Update documentation

**Risk:** Very Low - simple conditional logic.

### Phase 3: Monitoring/Metrics (Low Priority)

**Goal:** Add observability for production debugging.

#### 3.1 Basic Metrics Collection

**Proposed Metrics:**
- Task queue depth (pending, processing, completed)
- Webhook delivery success rate
- Webhook delivery latency
- Worker subprocess health

**Implementation Steps:**
1. Create `metrics.py` module with metrics collection
2. Add counters for webhook deliveries (success/failure)
3. Add gauges for queue depth
4. Add histogram for latency
5. Expose metrics via simple API or log output
6. Add tests for metrics collection

**Risk:** Very Low - additive feature, doesn't change existing behavior.

## Execution Order

1. **Phase 1.1:** Unit tests for new functions (test-1, test-2, test-3, test-4)
2. **Phase 1.2:** Integration tests (test-5)
3. **Phase 2.1:** Refactor builtins hack (refactor-1) - requires tests first
4. **Phase 2.2:** Add retry logic for webhook delivery (refactor-2)
5. **Phase 2.3:** Add webhook reachability validation (refactor-4)
6. **Phase 2.4:** Add Telegram echo disable option (refactor-5)
7. **Phase 3.1:** Add monitoring/metrics (refactor-3)

## Testing Strategy

### Before Refactoring
- Run existing test suite to establish baseline
- Ensure all existing tests pass

### During Refactoring
- Run tests after each change
- Use test-driven development for new features
- Add tests alongside refactoring

### After Refactoring
- Run full test suite
- Add integration tests for end-to-end scenarios
- Manual testing of critical paths

## Rollback Plan

Each refactoring step will be done in a separate commit:
- If tests fail, revert the specific commit
- Git bisect can identify the breaking change
- Feature flags can disable new features if needed

## Dependencies

- **Phase 2.1 depends on:** Phase 1 (all tests) - critical for safety
- **Phase 2.2 depends on:** Phase 1.2 (integration tests)
- **Phase 2.3 depends on:** Phase 1.2 (integration tests)
- **Phase 2.4 depends on:** Phase 1.2 (integration tests)
- **Phase 3.1 depends on:** None (independent feature)

## Estimated Effort

- Phase 1 (Tests): 4-6 hours
- Phase 2.1 (Builtins refactor): 6-8 hours (highest complexity)
- Phase 2.2 (Webhook retry): 2-3 hours
- Phase 2.3 (Reachability check): 2-3 hours
- Phase 2.4 (Echo disable): 1 hour
- Phase 3.1 (Metrics): 4-6 hours

**Total:** 19-27 hours

## Success Criteria

- All existing tests pass after refactoring
- New tests provide >80% coverage of new code
- No performance regression
- Code is more maintainable and testable
- Documentation is updated for new features
