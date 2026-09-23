"""Structured, concise logging.

Two rules from the approved design (``docs/architecture.md`` §6):

* Raw provider transcripts and credentials are **never** logged.
* Run logs live outside every target repository; this logger only emits short
  operator-facing lines.

``--log-format json`` produces one JSON object per line for journald/CI capture;
the default is a readable ``LEVEL event key=value`` line.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}

#: Keys whose values are never emitted, even if a caller passes them by mistake.
_REDACT_KEYS = {"token", "gh_token", "github_token", "password", "secret", "authorization"}


class Logger:
    def __init__(self, *, fmt: str = "text", stream: TextIO | None = None, verbose: bool = False) -> None:
        self.fmt = fmt
        self.stream = stream if stream is not None else sys.stderr
        self.verbose = verbose

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        if level == "debug" and not self.verbose:
            return
        safe = {
            key: ("<redacted>" if key.lower() in _REDACT_KEYS else value)
            for key, value in fields.items()
            if value is not None
        }
        if self.fmt == "json":
            payload = {"level": level, "event": event, **safe}
            line = json.dumps(payload, sort_keys=False, default=str)
        else:
            rendered = " ".join(f"{key}={_render(value)}" for key, value in safe.items())
            line = f"{level.upper():<7} {event}" + (f" {rendered}" if rendered else "")
        print(line, file=self.stream, flush=True)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("debug", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, **fields)


def _render(value: Any) -> str:
    text = str(value)
    if " " in text or text == "":
        return json.dumps(text)
    return text


def make_logger(fmt: str = "text", *, verbose: bool = False) -> Logger:
    return Logger(fmt=fmt, verbose=verbose)
