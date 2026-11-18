"""Desktop launcher: one click starts every Invoice Processor service.

Start applies the database migrations, then runs the web application and the
Outlook worker side by side, streams their output into one log (also saved to
``runtime_data/launcher.log``) and opens the browser once the web app answers.
Closing the window stops everything.
"""

from __future__ import annotations

import argparse
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = PROJECT_ROOT / "runtime_data" / "launcher.log"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
GRACEFUL_SHUTDOWN_SECONDS = 2
PROCESS_STOP_TIMEOUT_SECONDS = 4
WEB_READY_TIMEOUT_SECONDS = 60

MIGRATIONS = "Database"
WEB = "Web application"
WORKER = "Outlook worker"
SERVICES = (MIGRATIONS, WEB, WORKER)


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
    """The long-running services, built without starting them (testable)."""
    executable = python or sys.executable
    return {
        WEB: [
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
        WORKER: [executable, "-m", "app.outlook_worker"],
    }


def migration_command(*, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "app.postgres_migrate"]


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def web_is_up(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1).close()
    except urllib.error.HTTPError:
        return True  # any HTTP answer (a login redirect, a 401) means it is up
    except (OSError, ValueError):
        return False
    return True


def line_level(line: str) -> str:
    """'error', 'warning' or 'info', used to colour the log."""
    text = line.casefold()
    if any(word in text for word in ("error", "traceback", "exception", "failed", "critical")):
        return "error"
    if "warn" in text:
        return "warning"
    return "info"


class ProcessManager:
    """Runs the services; safe to call from the GUI thread."""

    def __init__(
        self, configuration: LauncherConfiguration, *, log_file: Path = LOG_FILE
    ) -> None:
        self.configuration = configuration
        self.log_file = log_file
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.states: dict[str, str] = {name: "stopped" for name in SERVICES}
        self.output: queue.Queue[tuple[str, str]] = queue.Queue()
        self.web_ready = threading.Event()
        self._busy = False
        self._stopping = False
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()

    # -- state ---------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._busy or any(p.poll() is None for p in self.processes.values())

    def log(self, source: str, text: str) -> None:
        line = text.rstrip("\n")
        self.output.put((source, line))
        with self._log_lock:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                with self.log_file.open("a", encoding="utf-8") as handle:
                    handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{source}] {line}\n")
            except OSError:
                pass  # the on-screen log still works

    # -- start ---------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self.active:
                return
            self._busy = True
            self._stopping = False
        threading.Thread(target=self._start_all, daemon=True).start()

    def _start_all(self) -> None:
        try:
            self._start_sequence()
        finally:
            self._busy = False

    def _start_sequence(self) -> None:
        self.web_ready.clear()
        self.states = {name: "stopped" for name in SERVICES}
        self.log("Launcher", "Starting Invoice Processor…")
        config = self.configuration
        if not port_is_free(config.host, config.port):
            self.states[WEB] = "failed"
            self.log(
                "Launcher",
                f"Error: port {config.port} is already in use. Another copy of "
                "the web app may be running; stop it and press Start again.",
            )
            return

        self.states[MIGRATIONS] = "running"
        self.log(MIGRATIONS, "Applying database migrations…")
        migrate = self._spawn(MIGRATIONS, migration_command())
        code = migrate.wait()
        if self._stopping:
            return
        if code != 0:
            self.states[MIGRATIONS] = "failed"
            self.log(MIGRATIONS, f"Error: migrations failed (exit code {code}); nothing else was started.")
            return
        self.states[MIGRATIONS] = "done"

        with self._lock:  # Stop pressed during migrations must win
            if self._stopping:
                return
            for name, command in process_commands(config).items():
                self.states[name] = "starting"
                self.log(name, f"Starting {name}…")
                self._spawn(name, command)
            self.states[WORKER] = "running"

        deadline = time.monotonic() + WEB_READY_TIMEOUT_SECONDS
        web = self.processes[WEB]
        while not self._stopping and web.poll() is None and time.monotonic() < deadline:
            if web_is_up(config.web_url):
                self.states[WEB] = "running"
                self.log(WEB, f"Ready at {config.web_url}")
                self.web_ready.set()
                return
            time.sleep(0.5)
        if not self._stopping and web.poll() is None:
            self.log(WEB, f"Warning: no answer from {config.web_url} after {WEB_READY_TIMEOUT_SECONDS} seconds.")

    def _spawn(self, name: str, command: list[str]) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.processes[name] = process
        threading.Thread(target=self._read_output, args=(name, process), daemon=True).start()
        return process

    def _read_output(self, name: str, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                self.log(name, line)
        code = process.wait()
        if name == MIGRATIONS or self._stopping:
            return
        self.states[name] = "failed" if code else "stopped"
        self.log(name, f"Error: {name} stopped unexpectedly (exit code {code}).")

    # -- stop ----------------------------------------------------------------

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            running = {n: p for n, p in self.processes.items() if p.poll() is None}
            for name, process in running.items():
                self.log("Launcher", f"Stopping {name}…")
                process.terminate()
            deadline = time.monotonic() + PROCESS_STOP_TIMEOUT_SECONDS
            for name, process in running.items():
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    self.log("Launcher", f"{name} did not stop in time; forcing it to stop.")
                    process.kill()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.log("Launcher", f"Error: {name} could not be stopped.")
            self.processes.clear()
            self.states = {name: "stopped" for name in SERVICES}
            self.web_ready.clear()
            if running:
                self.log("Launcher", "All services stopped.")

    def stop_in_background(self) -> None:
        threading.Thread(target=self.stop, daemon=True).start()


# ---------------------------------------------------------------------------
# Desktop window
# ---------------------------------------------------------------------------

COLOURS = {
    "bg": "#f4f5f7",
    "card": "#ffffff",
    "border": "#e3e5e9",
    "text": "#1d2330",
    "muted": "#6b7280",
    "primary": "#2f6fed",
    "primary_hover": "#2559c4",
    "danger": "#d64545",
    "danger_hover": "#b83838",
    "button": "#e9ebef",
    "button_hover": "#dcdfe5",
    "disabled": "#c4c8cf",
    "log_bg": "#151a23",
    "log_text": "#d6dae1",
    "log_time": "#5d6676",
    "log_error": "#ff7b7b",
    "log_warning": "#f0b45a",
}
STATE_STYLE = {  # dot colour, label
    "stopped": ("#b4b9c2", "Stopped"),
    "running": ("#2fb36d", "Running"),
    "starting": ("#f0b45a", "Starting…"),
    "done": ("#2fb36d", "Up to date"),
    "failed": ("#d64545", "Failed"),
}
SOURCE_COLOURS = {
    "Launcher": "#9aa4b5",
    MIGRATIONS: "#c39bff",
    WEB: "#6fb6ff",
    WORKER: "#6fe0b0",
}


def run_gui(configuration: LauncherConfiguration) -> int:
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except ImportError as error:
        raise RuntimeError(
            "This Python installation has no Tk desktop toolkit. Run "
            "'python3 -m app.launcher --headless' instead."
        ) from error

    c = COLOURS
    manager = ProcessManager(configuration)
    root = tk.Tk()
    root.title("Invoice Processor")
    root.geometry("900x620")
    root.minsize(680, 460)
    root.configure(bg=c["bg"])

    base = tkfont.nametofont("TkDefaultFont").actual("family")
    title_font = (base, 20, "bold")
    small_font = (base, 11)
    bold_font = (base, 12, "bold")
    mono = "Menlo" if sys.platform == "darwin" else "Consolas" if os.name == "nt" else "Monospace"
    log_font = (mono, 11)

    def flat_button(parent, text, command, colour, hover, fg="#ffffff"):
        """A label styled as a button: native macOS buttons ignore colours."""
        button = tk.Label(
            parent, text=text, bg=colour, fg=fg, font=bold_font,
            padx=18, pady=8, cursor="hand2",
        )
        button.enabled = True  # type: ignore[attr-defined]
        button.colours = (colour, hover, fg)  # type: ignore[attr-defined]

        def click(_event):
            if button.enabled:  # type: ignore[attr-defined]
                command()

        button.bind("<Button-1>", click)
        button.bind("<Enter>", lambda _e: button.enabled and button.configure(bg=button.colours[1]))  # type: ignore[attr-defined]
        button.bind("<Leave>", lambda _e: button.enabled and button.configure(bg=button.colours[0]))  # type: ignore[attr-defined]
        return button

    def set_button(button, *, text=None, enabled=True, colour=None, hover=None, fg=None):
        normal, over, text_colour = button.colours
        normal, over, text_colour = colour or normal, hover or over, fg or text_colour
        button.colours = (normal, over, text_colour)
        button.enabled = enabled
        button.configure(
            text=text or button.cget("text"),
            bg=normal if enabled else c["disabled"],
            fg=text_colour if enabled else "#ffffff",
            cursor="hand2" if enabled else "arrow",
        )

    def toggle():
        if manager.active:
            manager.stop_in_background()
        else:
            manager.start()

    # Header
    header = tk.Frame(root, bg=c["bg"])
    header.pack(fill="x", padx=24, pady=(20, 12))
    titles = tk.Frame(header, bg=c["bg"])
    titles.pack(side="left")
    tk.Label(titles, text="Invoice Processor", font=title_font, bg=c["bg"], fg=c["text"]).pack(anchor="w")
    tk.Label(
        titles, text=f"Database, web app and Outlook worker · {configuration.web_url}",
        font=small_font, bg=c["bg"], fg=c["muted"],
    ).pack(anchor="w")

    actions = tk.Frame(header, bg=c["bg"])
    actions.pack(side="right")
    open_button = flat_button(
        actions, "Open web app", lambda: webbrowser.open(configuration.web_url),
        c["button"], c["button_hover"], fg=c["text"],
    )
    open_button.pack(side="right", padx=(8, 0))
    main_button = flat_button(actions, "Start", toggle, c["primary"], c["primary_hover"])
    main_button.pack(side="right")

    # Service cards
    cards = tk.Frame(root, bg=c["bg"])
    cards.pack(fill="x", padx=24)
    descriptions = {
        MIGRATIONS: "Applies migrations to PostgreSQL",
        WEB: "The screens Purchase Ledger uses",
        WORKER: "Outlook intake and SharePoint filing",
    }
    indicators: dict[str, tuple[tk.Canvas, int, tk.Label]] = {}
    for column, name in enumerate(SERVICES):
        cards.columnconfigure(column, weight=1, uniform="card")
        card = tk.Frame(cards, bg=c["card"], highlightthickness=1, highlightbackground=c["border"])
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
        top = tk.Frame(card, bg=c["card"])
        top.pack(fill="x", padx=14, pady=(12, 2))
        dot_canvas = tk.Canvas(top, width=12, height=12, bg=c["card"], highlightthickness=0)
        dot = dot_canvas.create_oval(1, 1, 11, 11, fill=STATE_STYLE["stopped"][0], outline="")
        dot_canvas.pack(side="left", padx=(0, 8))
        tk.Label(top, text=name, font=bold_font, bg=c["card"], fg=c["text"]).pack(side="left")
        state_label = tk.Label(top, text="Stopped", font=small_font, bg=c["card"], fg=c["muted"])
        state_label.pack(side="right")
        tk.Label(
            card, text=descriptions[name], font=small_font, bg=c["card"], fg=c["muted"],
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 12))
        indicators[name] = (dot_canvas, dot, state_label)

    # Log
    log_header = tk.Frame(root, bg=c["bg"])
    log_header.pack(fill="x", padx=24, pady=(18, 6))
    tk.Label(log_header, text="Logs", font=bold_font, bg=c["bg"], fg=c["text"]).pack(side="left")

    def open_log_file():
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.touch(exist_ok=True)
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if os.name == "nt":
            os.startfile(LOG_FILE)  # type: ignore[attr-defined]
        else:
            subprocess.Popen([opener, str(LOG_FILE)])

    def clear_log():
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")

    for label, command in (("Open log file", open_log_file), ("Clear", clear_log)):
        link = tk.Label(log_header, text=label, font=small_font, bg=c["bg"], fg=c["primary"], cursor="hand2")
        link.pack(side="right", padx=(12, 0))
        link.bind("<Button-1>", lambda _e, run=command: run())

    log_frame = tk.Frame(root, bg=c["log_bg"])
    log_frame.pack(fill="both", expand=True, padx=24, pady=(0, 20))
    scrollbar = tk.Scrollbar(log_frame)
    scrollbar.pack(side="right", fill="y")
    log = tk.Text(
        log_frame, wrap="word", state="disabled", bg=c["log_bg"], fg=c["log_text"],
        font=log_font, relief="flat", borderwidth=0, padx=14, pady=12,
        highlightthickness=0, yscrollcommand=scrollbar.set, insertbackground=c["log_text"],
    )
    log.pack(fill="both", expand=True)
    scrollbar.configure(command=log.yview)
    log.tag_configure("time", foreground=c["log_time"])
    log.tag_configure("error", foreground=c["log_error"])
    log.tag_configure("warning", foreground=c["log_warning"])
    for source, colour in SOURCE_COLOURS.items():
        log.tag_configure(f"source:{source}", foreground=colour)

    def append(source: str, line: str) -> None:
        at_bottom = log.yview()[1] >= 0.999
        log.configure(state="normal")
        log.insert("end", f"{datetime.now():%H:%M:%S}  ", "time")
        log.insert("end", f"{source:<16}", f"source:{source}")
        level = line_level(line)
        log.insert("end", line + "\n", level if level != "info" else ())
        if int(log.index("end-1c").split(".")[0]) > 5000:
            log.delete("1.0", "1000.0")
        log.configure(state="disabled")
        if at_bottom:
            log.see("end")

    browser_opened = False

    def refresh() -> None:
        nonlocal browser_opened
        for _ in range(500):
            try:
                source, line = manager.output.get_nowait()
            except queue.Empty:
                break
            append(source, line)
        for name, (canvas, dot, label) in indicators.items():
            state = manager.states[name]
            colour, text = STATE_STYLE.get(state, STATE_STYLE["stopped"])
            canvas.itemconfigure(dot, fill=colour)
            label.configure(text=text, fg=c["muted"] if state == "stopped" else colour)
        if manager.active:
            set_button(main_button, text="Stop", colour=c["danger"], hover=c["danger_hover"])
        else:
            set_button(main_button, text="Start", colour=c["primary"], hover=c["primary_hover"])
            browser_opened = False
        ready = manager.web_ready.is_set()
        set_button(open_button, enabled=ready)
        if ready and not browser_opened:
            browser_opened = True
            webbrowser.open(configuration.web_url)
        root.after(200, refresh)

    def close() -> None:
        root.title("Invoice Processor - stopping…")
        root.update_idletasks()
        manager.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    if sys.platform == "darwin":
        root.createcommand("::tk::mac::Quit", close)
    append("Launcher", "Press Start to run the database migrations, web app and Outlook worker.")
    refresh()
    root.mainloop()
    return 0


def run_headless(configuration: LauncherConfiguration, *, open_browser: bool) -> int:
    manager = ProcessManager(configuration)
    manager.start()

    def stop_processes(signum: int, frame: object) -> None:
        del signum, frame
        manager.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop_processes)
    signal.signal(signal.SIGTERM, stop_processes)
    opened = False
    try:
        while True:
            while not manager.output.empty():
                source, line = manager.output.get()
                print(f"[{source}] {line}", flush=True)
            if open_browser and not opened and manager.web_ready.is_set():
                opened = True
                webbrowser.open(configuration.web_url)
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
        help="Start all services without the desktop launcher window.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the web application once it is ready in headless mode.",
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
