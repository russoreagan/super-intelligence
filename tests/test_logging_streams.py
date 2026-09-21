"""Log severity is decided by the STREAM in a container, so the split has to hold.

A bare logging.basicConfig() sends every record to stderr, which made Railway tag
INFO lines as errors — the dashboard's error filter matched 100% of lines and so
filtered nothing. brain/logging_setup.py splits the handlers; these tests pin that,
and pin that the secret-redacting filter still reaches BOTH of them.
"""

from __future__ import annotations

import logging
import subprocess
import sys

from brain import logging_setup


def _run(snippet: str) -> tuple[str, str]:
    r = subprocess.run(
        [sys.executable, "-c", snippet], capture_output=True, text=True, timeout=120
    )
    return r.stdout, r.returncode and r.stderr or r.stderr


def test_info_and_warning_go_to_stdout_errors_go_to_stderr():
    out, err = _run(
        "import logging\n"
        "from brain import logging_setup\n"
        "logging_setup.configure()\n"
        "log = logging.getLogger('brain.t')\n"
        "log.info('heartbeat'); log.warning('warn'); log.error('boom')\n"
    )
    assert "heartbeat" in out and "warn" in out
    assert "boom" not in out, "an ERROR reached stdout — it would be tagged info"
    assert "boom" in err
    assert "heartbeat" not in err, "an INFO reached stderr — it would be tagged an error"


def test_no_record_is_emitted_on_both_streams():
    out, err = _run(
        "import logging\n"
        "from brain import logging_setup\n"
        "logging_setup.configure()\n"
        "logging.getLogger('brain.t').error('once')\n"
    )
    assert out.count("once") + err.count("once") == 1


def test_configure_wins_over_a_handler_installed_earlier():
    """basicConfig is a no-op once any handler exists; libraries install their own on
    import, which would silently leave everything back on stderr."""
    out, err = _run(
        "import logging, sys\n"
        "logging.basicConfig(level='INFO')  # the default: everything to stderr\n"
        "from brain import logging_setup\n"
        "logging_setup.configure()\n"
        "logging.getLogger('brain.t').info('after')\n"
    )
    assert "after" in out and "after" not in err


def test_redaction_reaches_both_handlers(monkeypatch):
    """install_secret_redaction walks root.handlers, so a second handler must not be
    a hole that leaks secrets to the log."""
    from brain import security

    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sk-live-TOPSECRET-value-123456")
    logging_setup.configure()
    security.install_secret_redaction()
    handlers = logging.getLogger().handlers
    assert len(handlers) == 2, "expected a stdout and a stderr handler"
    for h in handlers:
        assert any(isinstance(f, security.SecretRedactingFilter) for f in h.filters), h


def test_both_entrypoints_use_the_shared_setup():
    """The gateway and the tenant brain must not drift back to a bare basicConfig."""
    from pathlib import Path

    root = Path(logging_setup.__file__).resolve().parent
    for rel in ("gateway/server.py", "run.py"):
        src = (root / rel).read_text()
        assert "logging_setup.configure()" in src, rel
        assert "logging.basicConfig(" not in src, f"{rel} still calls basicConfig directly"
