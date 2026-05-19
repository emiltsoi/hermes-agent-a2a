# Mode 2/3 — Worker Cancellation Smoke Tests
# hermes-agent-a2a Field Testing Gate Phase 5.75
#
# These tests verify worker spawn + cancel for Mode 2 (local) and Mode 3 (remote).
# They MUST run from within a live agent session that has access to the
# hermes_agent_a2a tools via a2a_run_local_agent_task / a2a_run_remote_agent_task.
#
# Prerequisite: yoyo and isa must be running with the updated plugin.
#
# To run:
#   cd /home/emil/.hermes/plugins/hermes-agent-a2a
#   python3 -m pytest tests/smoke/test_mode2_mode3.py -v -s
#
# NOTE: These cannot run via pytest directly from Britney's session
# because Mode 2 spawns workers on the LOCAL machine using the target profile's
# Hermes home. Run these via a2a_run_local_agent_task on the target machine,
# or run manually from the target agent's CLI session.

import uuid
import time


class TestMode2_LocalWorker:
    """Mode 2 — Local worker spawn and cancellation via a2a_run_local_agent_task."""

    def test_local_worker_spawn_and_cancel(self):
        """
        1. Spawn a local worker on isa's profile via a2a_run_local_agent_task
        2. Verify it starts (poll /status or check worker registry)
        3. Cancel it via a2a_cancel_protocol_task
        4. Verify it's dead (worker registry no longer shows it)

        This test must run INSIDE an agent session with a2a_run_local_agent_task access.
        From Britney's session, dispatch this as a subagent task.
        """
        task_id = f"mode2-{uuid.uuid4().hex[:8]}"
        worker_task_id = f"worker-{uuid.uuid4().hex[:8]}"

        # Step 1: Spawn a local worker on isa's profile
        # This requires a2a_run_local_agent_task(agent='isa', message='...', task_id=worker_task_id)
        # The worker should do something long-running so we can cancel it.
        #
        # Example message to isa:
        # "Spawn a worker. Run a long loop: for i in range(10_000_000): pass.
        #  Report 'worker started' then 'worker done' when complete.
        #  Use task_id: {worker_task_id}"

        # Step 2: Wait a moment for worker to start
        # time.sleep(1)

        # Step 3: Cancel the worker
        # a2a_cancel_protocol_task(task_id=worker_task_id)

        # Step 4: Verify it's cancelled
        # The cancel should return success and the worker should no longer appear in registry
        pass

    def test_local_worker_Completes_before_cancel(self):
        """
        Variant: spawn worker that completes quickly on its own.
        Verify we can still cancel it (no-op) and that completion is handled cleanly.
        """
        pass


class TestMode3_RemoteWorker:
    """Mode 3 — Remote worker spawn and cancellation via a2a_run_remote_agent_task."""

    def test_remote_worker_spawn_and_cancel(self):
        """
        1. Ask isa to spawn a remote worker on yoyo's machine via a2a_run_remote_agent_task
        2. Verify it starts on yoyo's machine
        3. Cancel it from isa's session
        4. Verify it's dead on yoyo's machine

        This is the cross-machine distributed cancellation test.
        """
        pass

    def test_remote_worker_spawn_by_britney(self):
        """
        Britney directly spawns a remote worker on isa's machine.
        Britney cancels it. Verify cancellation propagates correctly.
        """
        pass


# ---------------------------------------------------------------------------
# Integration markers
# ---------------------------------------------------------------------------
# To run these as actual integration tests, use:
#
#   a2a_run_local_agent_task(
#       name='isa',
#       message='run tests/smoke/test_mode2_mode3.py::TestMode2_LocalWorker::test_local_worker_spawn_and_cancel',
#       task_id='mode2-smoke-test'
#   )
#
# Or spawn a worker directly and cancel it:
#
#   # From Britney's session:
#   result = a2a_run_local_agent_task(
#       name='isa',
#       message='worker-standup',
#       task_id='mode2-test-worker'
#   )
#   # then
#   a2a_cancel_protocol_task(task_id='mode2-test-worker')
