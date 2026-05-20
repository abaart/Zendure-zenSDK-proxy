"""Rotating file logging and log dashboard helpers for ZendureProxy."""

from __future__ import annotations

from collections import deque
import html
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable


class ProxyFileLogger:
    """Write proxy log lines to a rotating file and expose file contents."""

    def __init__(self, path: str, max_bytes: int, backup_count: int):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_count = max(0, int(backup_count))

        self._logger = logging.getLogger(f"zendure_proxy.file.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        self._handler = RotatingFileHandler(
            self.path,
            maxBytes=max(1, int(max_bytes)),
            backupCount=self.backup_count,
            encoding="utf-8",
        )
        self._handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        self._logger.addHandler(self._handler)

    def log(self, message: str, level: str = "INFO") -> None:
        """Write one line to the rotating log file."""
        log_level = getattr(logging, str(level).upper(), logging.INFO)
        self._logger.log(log_level, message)

    def close(self) -> None:
        """Flush and close the rotating log handler."""
        self._logger.removeHandler(self._handler)
        self._handler.close()

    def read_tail(self, lines: int) -> str:
        """Return the last N lines from the active log file."""
        if not self.path.exists():
            return ""

        tail: deque[str] = deque(maxlen=max(1, int(lines)))
        with self.path.open("r", encoding="utf-8", errors="replace") as log_file:
            for line in log_file:
                tail.append(line.rstrip("\n"))
        return "\n".join(tail)

    def read_all(self) -> str:
        """Return all rotating log files, oldest backup first."""
        chunks: list[str] = []
        for log_path in self._log_files_oldest_first():
            if not log_path.exists():
                continue
            chunks.append(f"===== {log_path.name} =====\n")
            chunks.append(log_path.read_text(encoding="utf-8", errors="replace"))
            if chunks and not chunks[-1].endswith("\n"):
                chunks.append("\n")
        return "".join(chunks)

    def _log_files_oldest_first(self) -> Iterable[Path]:
        for idx in range(self.backup_count, 0, -1):
            yield Path(f"{self.path}.{idx}")
        yield self.path


def render_log_dashboard(title: str, log_text: str, download_url: str) -> str:
    """Render a small AppDaemon UI page for reading and downloading logs."""
    escaped_title = html.escape(title)
    escaped_log = html.escape(log_text or "No log lines available yet.")
    escaped_download_url = html.escape(download_url, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>{escaped_title}</title>
  <style>
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #111827;
      color: #e5e7eb;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 20px;
      border-bottom: 1px solid #374151;
      background: #0f172a;
    }}
    h1 {{
      margin: 0;
      font-size: 18px;
      font-weight: 650;
    }}
    a {{
      color: #93c5fd;
      text-decoration: none;
      font-size: 14px;
    }}
    main {{
      padding: 16px 20px;
    }}
    pre {{
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 0;
      padding: 16px;
      border: 1px solid #374151;
      border-radius: 8px;
      background: #020617;
      color: #d1d5db;
      line-height: 1.45;
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{escaped_title}</h1>
    <a href="{escaped_download_url}">Download log</a>
  </header>
  <main>
    <pre>{escaped_log}</pre>
  </main>
</body>
</html>"""
