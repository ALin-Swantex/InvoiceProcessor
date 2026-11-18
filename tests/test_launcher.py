import socket
import sys
import time
from pathlib import Path

import pytest

from app import launcher
from app.launcher import (
    LauncherConfiguration,
    ProcessManager,
    line_level,
    migration_command,
    port_is_free,
    process_commands,
)


def test_launcher_builds_web_and_worker_commands() -> None:
    commands = process_commands(
        LauncherConfiguration(host="0.0.0.0", port=9123), python="python-test"
    )

    assert commands["Web application"] == [
        "python-test",
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "9123",
        "--timeout-graceful-shutdown",
        "2",
    ]
    assert commands["Outlook worker"] == [
        "python-test",
        "-m",
        "app.outlook_worker",
    ]


def test_launcher_builds_local_web_url() -> None:
    assert LauncherConfiguration(port=8765).web_url == "http://127.0.0.1:8765"


def test_launcher_runs_migrations_with_the_same_python() -> None:
    assert migration_command(python="python-test") == [
        "python-test",
        "-m",
        "app.postgres_migrate",
    ]


def test_port_check_detects_a_port_in_use() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert not port_is_free("127.0.0.1", busy.getsockname()[1])


def test_log_lines_are_coloured_by_level() -> None:
    assert line_level("Traceback (most recent call last):") == "error"
    assert line_level("WARNING: slow response") == "warning"
    assert line_level("INFO: Application startup complete.") == "info"


def wait_until(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def fake_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, migrate_exit: int
) -> ProcessManager:
    python = sys.executable
    monkeypatch.setattr(
        launcher,
        "migration_command",
        lambda: [python, "-c", f"print('migrated'); raise SystemExit({migrate_exit})"],
    )
    sleeper = [python, "-c", "import time; print('up', flush=True); time.sleep(30)"]
    monkeypatch.setattr(
        launcher,
        "process_commands",
        lambda configuration: {launcher.WEB: sleeper, launcher.WORKER: sleeper},
    )
    monkeypatch.setattr(launcher, "web_is_up", lambda url: True)
    configuration = launcher.LauncherConfiguration(port=0)
    return ProcessManager(configuration, log_file=tmp_path / "launcher.log")


def test_start_runs_migrations_then_every_service_and_stop_ends_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = fake_manager(tmp_path, monkeypatch, migrate_exit=0)

    manager.start()
    wait_until(manager.web_ready.is_set)

    assert manager.states == {
        launcher.MIGRATIONS: "done",
        launcher.WEB: "running",
        launcher.WORKER: "running",
    }
    manager.stop()
    assert not manager.active
    log_text = (tmp_path / "launcher.log").read_text()
    assert "[Database] migrated" in log_text
    assert "All services stopped." in log_text


def test_failed_migrations_start_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = fake_manager(tmp_path, monkeypatch, migrate_exit=1)

    manager.start()
    wait_until(lambda: not manager.active)

    assert manager.states[launcher.MIGRATIONS] == "failed"
    assert set(manager.processes) == {launcher.MIGRATIONS}
    assert "migrations failed" in (tmp_path / "launcher.log").read_text()
