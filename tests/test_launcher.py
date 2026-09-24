from app.launcher import LauncherConfiguration, process_commands


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
