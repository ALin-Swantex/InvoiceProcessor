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


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handling is POSIX-only")
def test_stop_lets_the_worker_finish_its_current_invoice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = fake_manager(tmp_path, monkeypatch, migrate_exit=0)
    python = sys.executable
    # Like the real worker: on SIGTERM it finishes the current item (here,
    # five seconds - longer than the web app's stop timeout) and exits 0.
    finishing_worker = [
        python,
        "-c",
        "import signal, sys, time\n"
        "def finish(*_):\n"
        "    time.sleep(5)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, finish)\n"
        "print('up', flush=True)\n"
        "time.sleep(60)\n",
    ]
    sleeper = [python, "-c", "import time; print('up', flush=True); time.sleep(30)"]
    monkeypatch.setattr(
        launcher,
        "process_commands",
        lambda configuration: {launcher.WEB: sleeper, launcher.WORKER: finishing_worker},
    )
    manager.start()
    wait_until(manager.web_ready.is_set)
    wait_until(lambda: "[Outlook worker] up" in (tmp_path / "launcher.log").read_text())
    worker = manager.processes[launcher.WORKER]

    manager.stop()

    assert worker.returncode == 0  # finished, not killed


def test_port_check_detects_a_wildcard_listener() -> None:
    # On macOS/BSD a probe with SO_REUSEADDR can bind 127.0.0.1 next to a
    # 0.0.0.0 listener, so the check must not rely on bind alone.
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        assert not port_is_free("127.0.0.1", busy.getsockname()[1])


def test_large_log_is_rotated_when_services_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = fake_manager(tmp_path, monkeypatch, migrate_exit=1)
    log_file = tmp_path / "launcher.log"
    log_file.write_text("old line\n" * 10)
    (tmp_path / "launcher.log.1").write_text("older\n")
    monkeypatch.setattr(launcher, "LOG_ROTATE_BYTES", 20)

    manager.start()
    wait_until(lambda: not manager.active)

    assert (tmp_path / "launcher.log.1").read_text() == "old line\n" * 10
    assert "old line" not in log_file.read_text()


def headless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(launcher, "LOG_FILE", tmp_path / "launcher.log")
    monkeypatch.setattr(launcher.signal, "signal", lambda *args: None)
    return launcher.run_headless(LauncherConfiguration(port=0), open_browser=False)


def test_headless_exits_non_zero_when_migrations_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_manager(tmp_path, monkeypatch, migrate_exit=1)

    assert headless(tmp_path, monkeypatch) == 1
    assert "migrations failed" in (tmp_path / "launcher.log").read_text()


def test_headless_exits_non_zero_when_the_web_app_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_manager(tmp_path, monkeypatch, migrate_exit=0)
    python = sys.executable
    sleeper = [python, "-c", "import time; print('up', flush=True); time.sleep(30)"]
    monkeypatch.setattr(
        launcher,
        "process_commands",
        lambda configuration: {
            launcher.WEB: [python, "-c", "raise SystemExit(3)"],
            launcher.WORKER: sleeper,
        },
    )
    monkeypatch.setattr(launcher, "web_is_up", lambda url: False)

    assert headless(tmp_path, monkeypatch) == 1
    assert "All services stopped." in (tmp_path / "launcher.log").read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="SO_REUSEADDR is only set on POSIX")
def test_port_left_in_time_wait_by_the_last_stop_counts_as_free() -> None:
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        connection, _ = server.accept()
        connection.close()  # the server closes first: its side enters TIME_WAIT
        client.close()

    assert port_is_free("127.0.0.1", port)
