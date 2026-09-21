"""Process logging configuration — one place, because the stream decides the severity.

Railway (and most container log collectors) derive a line's severity from the STREAM
it arrived on, not from anything in the text: stdout is info, stderr is error. Python's
`logging.basicConfig()` sends every record to stderr, so a default setup tags INFO,
WARNING and ERROR identically as errors. In production that made the dashboard's error
filter match 100% of lines — it filtered nothing, and a tenant whose DMN had been
failing for hours looked exactly like a healthy one.

`brain/provisioner.py:relay_level` already fixed half of this, re-levelling each
relayed TENANT line to the level the child logged at. But the gateway's own handler
still wrote everything to stderr, so the re-levelled records were re-tagged as errors
on the way out. This is the other half.

Split the handlers instead: INFO/DEBUG/WARNING to stdout, ERROR/CRITICAL to stderr.
The collector's severity then matches the record's own level, and `level:error` means
something again.
"""

from __future__ import annotations

import logging
import os
import sys

DEFAULT_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class _BelowError(logging.Filter):
    """Everything the stderr handler will NOT take, so no record is emitted twice."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def configure(level: str | None = None, fmt: str | None = DEFAULT_FORMAT) -> None:
    """Install the split-stream handlers on the root logger.

    Call once, early, BEFORE brain.security.install_secret_redaction() — that walks
    root.handlers to attach the redacting filter, so both handlers must exist first.

    `force=True` because basicConfig is otherwise a no-op once any handler exists
    (uvicorn and some libraries install their own on import), which would silently
    leave everything back on stderr.
    """
    lvl = (level or os.environ.get("BRAIN_LOG_LEVEL") or "INFO").upper()

    out = logging.StreamHandler(sys.stdout)
    out.addFilter(_BelowError())
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.ERROR)

    kwargs: dict = {"level": lvl, "handlers": [out, err], "force": True}
    if fmt:
        kwargs["format"] = fmt
    logging.basicConfig(**kwargs)
