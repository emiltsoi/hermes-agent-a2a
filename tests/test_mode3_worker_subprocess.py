"""Mode 3 Worker Subprocess Lifecycle Tests — Issue 17.

Tests cover:
1. Worker subprocess starts and registers with task_id
2. Worker subprocess times out after configured timeout
3. Zombie subprocesses are cleaned up by cleanup_zombie_processes()
4. Non-zero exit code is captured and returned as failed state
"""

import subprocess
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hermes_agent_a2a.worker_registry import (
    cancel_worker,
    cleanup_zombie_processes,
    register_worker,
    unregister_worker,
    _processes,
    _lock,
)


# Test constant — avoids hardcoded home path in public test code
TEST_HERMES_HOME = str(Path.home() / ".hermes")


class TestWorkerRegistrySubprocessLifecycle:
    """Unit tests for worker_registry subprocess lifecycle management."""

    def teardown_method(self):
        """Clean up any registered workers after each test."""
        with _lock:
            for task_id in list(_processes.keys()):
                proc = _processes.get(task_id)
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                _processes.pop(task_id, None)

    def test_register_worker_adds_process_to_registry(self):
        """Test that register_worker correctly adds a subprocess to the registry."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None  # Process is running

        task_id = "test-task-001"
        register_worker(task_id, proc)

        with _lock:
            assert task_id in _processes
            assert _processes[task_id] is proc

    def test_register_worker_cleans_up_zombie_before_registering(self):
        """Test that register_worker removes a zombie entry for the same task_id before adding new process."""
        # Create a zombie process (already finished)
        zombie_proc = MagicMock(spec=subprocess.Popen)
        zombie_proc.poll.return_value = 0  # Process has exited

        task_id = "test-task-zombie"

        # Register the zombie first
        register_worker(task_id, zombie_proc)

        # Now create a new process for the same task_id
        new_proc = MagicMock(spec=subprocess.Popen)
        new_proc.poll.return_value = None  # Process is running

        register_worker(task_id, new_proc)

        # Should have only one entry for task_id, and it should be the new process
        with _lock:
            assert task_id in _processes
            assert _processes[task_id] is new_proc

    def test_unregister_worker_removes_process(self):
        """Test that unregister_worker removes a subprocess from the registry."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None

        task_id = "test-task-002"
        register_worker(task_id, proc)
        unregister_worker(task_id)

        with _lock:
            assert task_id not in _processes

    def test_cancel_worker_terminates_running_process(self):
        """Test that cancel_worker sends SIGTERM to a running process."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None  # Process is running

        task_id = "test-task-003"
        register_worker(task_id, proc)

        result = cancel_worker(task_id, timeout=3.0)

        assert result is True
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=3.0)

    def test_cancel_worker_returns_false_for_unknown_task(self):
        """Test that cancel_worker returns False for unknown task_id."""
        result = cancel_worker("nonexistent-task", timeout=3.0)
        assert result is False

    def test_cancel_worker_returns_false_for_already_finished_process(self):
        """Test that cancel_worker returns False when process has already finished."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = 0  # Process has already exited

        task_id = "test-task-finished"
        register_worker(task_id, proc)

        result = cancel_worker(task_id, timeout=3.0)
        assert result is False

    def test_cancel_worker_kills_process_on_timeout(self):
        """Test that cancel_worker kills the process if it doesn't terminate gracefully."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None  # Process is running
        # First wait raises (graceful terminate times out), second wait returns normally (after kill)
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 3.0), None]

        task_id = "test-task-stuck"
        register_worker(task_id, proc)

        result = cancel_worker(task_id, timeout=3.0)

        assert result is True
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert proc.wait.call_count == 2

    def test_cleanup_zombie_processes_removes_finished_processes(self):
        """Test that cleanup_zombie_processes() removes processes that have exited."""
        # Create a zombie process
        zombie_proc = MagicMock(spec=subprocess.Popen)
        zombie_proc.poll.return_value = 0  # Exited with code 0

        # Create a running process
        running_proc = MagicMock(spec=subprocess.Popen)
        running_proc.poll.return_value = None  # Still running

        zombie_task_id = "zombie-task"
        running_task_id = "running-task"

        register_worker(zombie_task_id, zombie_proc)
        register_worker(running_task_id, running_proc)

        # Run cleanup
        cleaned = cleanup_zombie_processes()

        assert cleaned == 1
        with _lock:
            assert zombie_task_id not in _processes
            assert running_task_id in _processes
            assert _processes[running_task_id] is running_proc

    def test_cleanup_zombie_processes_returns_count(self):
        """Test that cleanup_zombie_processes() returns the count of cleaned zombies."""
        # Create multiple zombie processes
        for i in range(3):
            proc = MagicMock(spec=subprocess.Popen)
            proc.poll.return_value = i  # All exited

            register_worker(f"zombie-task-{i}", proc)

        cleaned = cleanup_zombie_processes()
        assert cleaned == 3

    def test_cleanup_zombie_processes_with_no_zombies(self):
        """Test that cleanup_zombie_processes() handles no zombies gracefully."""
        # Create only running processes
        for i in range(2):
            proc = MagicMock(spec=subprocess.Popen)
            proc.poll.return_value = None

            register_worker(f"running-task-{i}", proc)

        cleaned = cleanup_zombie_processes()
        assert cleaned == 0

    def test_cleanup_zombie_processes_empty_registry(self):
        """Test that cleanup_zombie_processes() handles empty registry."""
        cleaned = cleanup_zombie_processes()
        assert cleaned == 0


class TestMode3WorkerSubprocessTimeout:
    """Tests for Mode 3 worker subprocess timeout behavior."""

    def teardown_method(self):
        """Clean up any registered workers after each test."""
        with _lock:
            for task_id in list(_processes.keys()):
                proc = _processes.get(task_id)
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                _processes.pop(task_id, None)

    def test_mode3_worker_times_out_after_configured_timeout(self):
        """Test that Mode 3 worker subprocess is killed after timeout expires."""
        from hermes_agent_a2a.tool_handlers import _handle_task_send_mode3

        # Create a mock subprocess that hangs (never produces output)
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired("cmd", 1)
        mock_proc.returncode = None

        task_id = "test-mode3-timeout"
        timeout = 1

        with patch("hermes_agent_a2a.tool_handlers.subprocess.Popen", return_value=mock_proc):
            with patch("hermes_agent_a2a.tool_handlers.register_worker"):
                with patch("hermes_agent_a2a.tool_handlers.unregister_worker"):
                    with patch("hermes_agent_a2a.tool_handlers._derive_hermes_home", return_value=TEST_HERMES_HOME):
                        result = _handle_task_send_mode3(
                            params={"id": task_id, "name": "testagent"},
                            metadata={"timeout": timeout},
                            user_text="test message",
                        )

        # Should return failed state
        assert result["status"]["state"] == "failed"
        assert "timed out" in result["artifacts"][0]["parts"][0]["text"].lower()

        # Should have killed the process
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called()

    def test_mode3_worker_timeout_cleanup(self):
        """Test that cleanup_zombie_processes is called after Mode 3 timeout."""
        from hermes_agent_a2a.tool_handlers import _handle_task_send_mode3

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired("cmd", 5)
        mock_proc.returncode = None

        task_id = "test-mode3-cleanup"

        with patch("hermes_agent_a2a.tool_handlers.subprocess.Popen", return_value=mock_proc):
            with patch("hermes_agent_a2a.tool_handlers.register_worker"):
                with patch("hermes_agent_a2a.tool_handlers.unregister_worker"):
                    with patch("hermes_agent_a2a.tool_handlers._derive_hermes_home", return_value=TEST_HERMES_HOME):
                        with patch("hermes_agent_a2a.worker_registry.cleanup_zombie_processes") as mock_cleanup:
                            mock_cleanup.return_value = 0

                            result = _handle_task_send_mode3(
                                params={"id": task_id, "name": "testagent"},
                                metadata={"timeout": 5},
                                user_text="test",
                            )

        # Cleanup should be called in finally block
        mock_cleanup.assert_called()


class TestMode3WorkerNonZeroExit:
    """Tests for Mode 3 worker subprocess non-zero exit handling."""

    def teardown_method(self):
        """Clean up any registered workers after each test."""
        with _lock:
            for task_id in list(_processes.keys()):
                proc = _processes.get(task_id)
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                _processes.pop(task_id, None)

    def test_mode3_worker_non_zero_exit_returns_failed_state(self):
        """Test that non-zero exit code is captured and returned as failed state."""
        from hermes_agent_a2a.tool_handlers import _handle_task_send_mode3

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.communicate.return_value = ("", "Worker script error: something went wrong")
        mock_proc.returncode = 42

        task_id = "test-mode3-error"

        with patch("hermes_agent_a2a.tool_handlers.subprocess.Popen", return_value=mock_proc):
            with patch("hermes_agent_a2a.tool_handlers.register_worker"):
                with patch("hermes_agent_a2a.tool_handlers.unregister_worker"):
                    with patch("hermes_agent_a2a.tool_handlers._derive_hermes_home", return_value=TEST_HERMES_HOME):
                        result = _handle_task_send_mode3(
                            params={"id": task_id, "name": "testagent"},
                            metadata={"timeout": 300},
                            user_text="test message",
                        )

        assert result["status"]["state"] == "failed"
        assert "error" in result["artifacts"][0]["parts"][0]["text"].lower()

    def test_mode3_worker_zero_exit_returns_completed_state(self):
        """Test that zero exit code is captured and returned as completed state."""
        from hermes_agent_a2a.tool_handlers import _handle_task_send_mode3

        worker_response = '{"response": "Task completed successfully"}'

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.communicate.return_value = (worker_response, "")
        mock_proc.returncode = 0

        task_id = "test-mode3-success"

        with patch("hermes_agent_a2a.tool_handlers.subprocess.Popen", return_value=mock_proc):
            with patch("hermes_agent_a2a.tool_handlers.register_worker"):
                with patch("hermes_agent_a2a.tool_handlers.unregister_worker"):
                    with patch("hermes_agent_a2a.tool_handlers._derive_hermes_home", return_value=TEST_HERMES_HOME):
                        result = _handle_task_send_mode3(
                            params={"id": task_id, "name": "testagent"},
                            metadata={"timeout": 300},
                            user_text="test message",
                        )

        assert result["status"]["state"] == "completed"
        assert "Task completed successfully" in result["artifacts"][0]["parts"][0]["text"]

    def test_mode3_worker_non_json_stdout_returns_failed_state(self):
        """Test that non-JSON worker output is handled gracefully as failed state."""
        from hermes_agent_a2a.tool_handlers import _handle_task_send_mode3

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.communicate.return_value = ("This is not JSON output", "")
        mock_proc.returncode = 0

        task_id = "test-mode3-nonjson"

        with patch("hermes_agent_a2a.tool_handlers.subprocess.Popen", return_value=mock_proc):
            with patch("hermes_agent_a2a.tool_handlers.register_worker"):
                with patch("hermes_agent_a2a.tool_handlers.unregister_worker"):
                    with patch("hermes_agent_a2a.tool_handlers._derive_hermes_home", return_value=TEST_HERMES_HOME):
                        result = _handle_task_send_mode3(
                            params={"id": task_id, "name": "testagent"},
                            metadata={"timeout": 300},
                            user_text="test message",
                        )

        assert result["status"]["state"] == "failed"
        assert "non-JSON" in result["artifacts"][0]["parts"][0]["text"]


class TestMode2WorkerSubprocess:
    """Tests for Mode 2 worker subprocess lifecycle (same patterns as Mode 3)."""

    def teardown_method(self):
        """Clean up any registered workers after each test."""
        with _lock:
            for task_id in list(_processes.keys()):
                proc = _processes.get(task_id)
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                _processes.pop(task_id, None)

    def test_mode2_worker_non_zero_exit_returns_error(self):
        """Test that Mode 2 worker non-zero exit is returned as error dict."""
        from hermes_agent_a2a.tool_handlers import _handle_call_mode2

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.communicate.return_value = ("", "Error: profile not found")
        mock_proc.returncode = 1

        task_id = "test-mode2-error"

        with patch("hermes_agent_a2a.tool_handlers.subprocess.Popen", return_value=mock_proc):
            with patch("hermes_agent_a2a.tool_handlers.register_worker"):
                with patch("hermes_agent_a2a.tool_handlers.unregister_worker"):
                    with patch("hermes_agent_a2a.tool_handlers._derive_hermes_home", return_value=TEST_HERMES_HOME):
                        with patch("os.path.isdir", return_value=True):
                            result = _handle_call_mode2(
                                name="testagent",
                                message="do something",
                                task_id=task_id,
                                timeout=60,
                            )

        assert "error" in result
        assert "Mode 2 worker error" in result["error"]

    def test_mode2_worker_timeout_returns_error(self):
        """Test that Mode 2 worker timeout is returned as error dict."""
        from hermes_agent_a2a.tool_handlers import _handle_call_mode2

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired("cmd", 60)
        mock_proc.returncode = None

        task_id = "test-mode2-timeout"

        with patch("hermes_agent_a2a.tool_handlers.subprocess.Popen", return_value=mock_proc):
            with patch("hermes_agent_a2a.tool_handlers.register_worker"):
                with patch("hermes_agent_a2a.tool_handlers.unregister_worker"):
                    with patch("hermes_agent_a2a.tool_handlers._derive_hermes_home", return_value=TEST_HERMES_HOME):
                        with patch("os.path.isdir", return_value=True):
                            result = _handle_call_mode2(
                                name="testagent",
                                message="do something",
                                task_id=task_id,
                                timeout=60,
                            )

        assert "error" in result
        assert "timed out" in result["error"]

    def test_mode2_worker_success_returns_completed_result(self):
        """Test that Mode 2 worker success returns proper result dict."""
        from hermes_agent_a2a.tool_handlers import _handle_call_mode2

        worker_response = '{"response": "Hello from Mode 2"}'

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None
        mock_proc.communicate.return_value = (worker_response, "")
        mock_proc.returncode = 0

        task_id = "test-mode2-success"

        with patch("hermes_agent_a2a.tool_handlers.subprocess.Popen", return_value=mock_proc):
            with patch("hermes_agent_a2a.tool_handlers.register_worker"):
                with patch("hermes_agent_a2a.tool_handlers.unregister_worker"):
                    with patch("hermes_agent_a2a.tool_handlers._derive_hermes_home", return_value=TEST_HERMES_HOME):
                        with patch("os.path.isdir", return_value=True):
                            result = _handle_call_mode2(
                                name="testagent",
                                message="do something",
                                task_id=task_id,
                                timeout=60,
                            )

        assert result.get("state") == "completed"
        assert result.get("response") == "Hello from Mode 2"
