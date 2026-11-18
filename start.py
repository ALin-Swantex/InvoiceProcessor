"""Start Invoice Processor: database migrations, web app and Outlook worker.

    python start.py              desktop window with Start/Stop and live logs
    python start.py --headless   no window; logs in the terminal, Ctrl+C stops

Works on macOS, Windows and Linux. On Windows you can also double-click it.
"""

from app.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
