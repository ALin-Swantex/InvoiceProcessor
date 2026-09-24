"""Cross-platform desktop launcher for the web application and Outlook worker."""

from __future__ import annotations

import argparse
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
GRACEFUL_SHUTDOWN_SECONDS = 2
PROCESS_STOP_TIMEOUT_SECONDS = 4


@dataclass(frozen=True)
class LauncherConfiguration:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def web_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def process_commands(
    configuration: LauncherConfiguration,
    *,
    python: str | None = None,
) -> dict[str, list[str]]:
    """Return commands without starting them, keeping this launcher testable."""
    executable = python or sys.executable
    return {
        "Web application": [
            executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            configuration.host,
            "--port",
            str(configuration.port),
            "--timeout-graceful-shutdown",
            str(GRACEFUL_SHUTDOWN_SECONDS),
        ],
        "Outlook worker": [executable, "-m", "app.outlook_worker"],
    }


class ProcessManager:
    def __init__(self, configuration: LauncherConfiguration) -> None:
        self.configuration = configuration
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.output: queue.Queue[str] = queue.Queue()
        self._stop_lock = threading.Lock()

    def start(self) -> None:
        for name, command in process_commands(self.configuration).items():
            running = self.processes.get(name)
            if running is not None and running.poll() is None:
                continue
            self.output.put(f"Starting {name}…\n")
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.processes[name] = process
            threading.Thread(
                target=self._read_output,
                args=(name, process),
                daemon=True,
            ).start()

    def stop(self) -> None:
        with self._stop_lock:
            for name, process in self.processes.items():
                if process.poll() is None:
                    self.output.put(f"Stopping {name}…\n")
                    process.terminate()
            deadline = time.monotonic() + PROCESS_STOP_TIMEOUT_SECONDS
            for name, process in self.processes.items():
                if process.poll() is None:
                    try:
                        process.wait(
                            timeout=max(0.1, deadline - time.monotonic())
                        )
                    except subprocess.TimeoutExpired:
                        self.output.put(
                            f"{name} did not stop gracefully; forcing it to stop…\n"
                        )
                        process.kill()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            self.output.put(f"{name} could not be stopped.\n")
            self.processes.clear()

    def stop_in_background(self) -> None:
        """Keep the desktop window responsive while child processes exit."""
        threading.Thread(target=self.stop, daemon=True).start()

    def status(self) -> str:
        states = []
        for name in process_commands(self.configuration):
            process = self.processes.get(name)
            if process is None:
                states.append(f"{name}: stopped")
            elif process.poll() is None:
                states.append(f"{name}: running")
            else:
                states.append(f"{name}: exited ({process.returncode})")
        return " • ".join(states)

    def _read_output(self, name: str, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            self.output.put(f"[{name}] {line}")


def run_gui(configuration: LauncherConfiguration) -> int:
    try:
        import tkinter as tk
        from tkinter import scrolledtext
    except ImportError as error:
        raise RuntimeError(
            "This Python installation has no Tk desktop toolkit. Run "
            "'invoice-processor --headless' instead."
        ) from error

    manager = ProcessManager(configuration)
    root = tk.Tk()
    root.title("Invoice Processor")
    root.geometry("760x480")
    root.minsize(620, 360)

    status = tk.StringVar(value=manager.status())
    tk.Label(root, text="Invoice Processor", font=("Arial", 18, "bold")).pack(
        anchor="w", padx=16, pady=(16, 2)
    )
    tk.Label(
        root,
        text=(
            "Starts the web application and Outlook worker. "
            f"The web app is available at {configuration.web_url}."
        ),
        justify="left",
    ).pack(anchor="w", padx=16)
    tk.Label(root, textvariable=status, justify="left").pack(
        anchor="w", padx=16, pady=(10, 6)
    )

    buttons = tk.Frame(root)
    buttons.pack(anchor="w", padx=16, pady=(0, 10))
    tk.Button(buttons, text="Start", command=manager.start).pack(side="left")
    tk.Button(
        buttons,
        text="Open web app",
        command=lambda: webbrowser.open(configuration.web_url),
    ).pack(side="left", padx=8)
    tk.Button(buttons, text="Stop", command=manager.stop_in_background).pack(
        side="left"
    )

    log = scrolledtext.ScrolledText(root, wrap="word", state="disabled")
    log.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def refresh() -> None:
        status.set(manager.status())
        while True:
            try:
                line = manager.output.get_nowait()
            except queue.Empty:
                break
            log.configure(state="normal")
            log.insert("end", line)
            log.see("end")
            log.configure(state="disabled")
        root.after(250, refresh)

    def close() -> None:
        manager.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh()
    root.mainloop()
    return 0


def run_headless(configuration: LauncherConfiguration, *, open_browser: bool) -> int:
    manager = ProcessManager(configuration)
    manager.start()
    if open_browser:
        webbrowser.open(configuration.web_url)

    def stop_processes(signum: int, frame: object) -> None:
        del signum, frame
        manager.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop_processes)
    signal.signal(signal.SIGTERM, stop_processes)
    try:
        while True:
            while not manager.output.empty():
                print(manager.output.get(), end="", flush=True)
            time.sleep(0.25)
    finally:
        manager.stop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch Invoice Processor")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Start both services without the desktop launcher window.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the web application after starting in headless mode.",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535.")
    configuration = LauncherConfiguration(host=args.host, port=args.port)
    if args.headless:
        return run_headless(configuration, open_browser=args.open_browser)
    return run_gui(configuration)


if __name__ == "__main__":
    raise SystemExit(main())
