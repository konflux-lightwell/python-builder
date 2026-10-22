"""Ensure stage progress reflects outcomes without exposing exception contents."""

import pytest

from lightwell_python.runtime import log_stage


@pytest.mark.parametrize("fails", [False, True])
def test_stage_reports_duration_and_actual_outcome(monkeypatch, caplog, fails):
    caplog.set_level("INFO")
    times = iter([10.0, 12.5])
    monkeypatch.setattr("lightwell_python.runtime.time.monotonic", lambda: next(times))
    error = ValueError("sensitive-backend-value")

    def operation():
        with log_stage("Build artifacts"):
            if fails:
                raise error

    if fails:
        with pytest.raises(ValueError) as caught:
            operation()
        assert caught.value is error
        assert caplog.messages == [
            "Starting: Build artifacts",
            "Failed: Build artifacts after 2.5s (ValueError)",
        ]
    else:
        operation()
        assert caplog.messages == [
            "Starting: Build artifacts",
            "Completed: Build artifacts in 2.5s",
        ]
    assert "sensitive-backend-value" not in caplog.text
