"""
The password-reset link's redirect_to must be built from the EXTERNAL url
(brain/ui/auth.py::external_base_url).

Prod bug this pins: /auth/forgot built redirect_to from request.base_url, which
is http:// behind Railway — uvicorn only honours x-forwarded-proto when the peer
is in forwarded_allow_ips (default 127.0.0.1) and the edge never is. GoTrue
matches redirect_to against the project's allowlist INCLUDING the scheme, and an
unlisted value is not an error: it silently falls back to SITE_URL. So the
recovery link dropped the user on the app root (→ /login) with the token
stranded in the fragment, and the password was never changed. Verified live
against GoTrue: https://elyceum.app/auth/reset redirects to the reset page,
http://elyceum.app/auth/reset redirects to https://elyceum.app.

The scheme is the whole bug, so these tests assert on it directly.
"""

from __future__ import annotations

from starlette.requests import Request

from brain.ui.auth import external_base_url


def _request(headers: dict[str, str], scheme: str = "http") -> Request:
    """A request as it actually arrives: TLS is terminated upstream, so the
    transport scheme is http and the truth is in the forwarded headers."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": scheme,
            "path": "/auth/forgot",
            "query_string": b"",
            "root_path": "",
            "server": ("0.0.0.0", 8080),
            "client": ("100.64.0.5", 54321),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_forwarded_proto_wins_over_transport_scheme():
    # The prod shape: Railway forwards https traffic to us over plain http.
    req = _request({"host": "elyceum.app", "x-forwarded-proto": "https"})
    assert external_base_url(req) == "https://elyceum.app"
    assert str(req.base_url).startswith("http://"), (
        "guard: if base_url is https here, uvicorn started trusting the proxy and "
        "this test no longer reproduces the prod shape"
    )


def test_reset_url_is_the_allowlisted_origin():
    req = _request({"host": "elyceum.app", "x-forwarded-proto": "https"})
    # Exactly the URL the live GoTrue allowlist accepts — an http:// variant is
    # dropped for SITE_URL and the reset page never loads.
    assert external_base_url(req) + "/auth/reset" == "https://elyceum.app/auth/reset"


def test_first_value_of_a_forwarded_chain_is_used():
    # Multiple proxies append; the client-facing hop is first.
    req = _request({"host": "elyceum.app", "x-forwarded-proto": "https, http"})
    assert external_base_url(req) == "https://elyceum.app"


def test_forwarded_host_preferred_over_host_header():
    req = _request(
        {
            "host": "brain-internal.railway.internal",
            "x-forwarded-host": "elyceum.app",
            "x-forwarded-proto": "https",
        }
    )
    assert external_base_url(req) == "https://elyceum.app"


def test_local_dev_without_a_proxy_is_unchanged():
    # No forwarded headers: fall back to the transport scheme. Local dev must not
    # start emitting https links to a server that only speaks http.
    req = _request({"host": "localhost:8765"})
    assert external_base_url(req) == "http://localhost:8765"


def test_public_url_override_pins_the_canonical_origin(monkeypatch):
    # elyceum.app is canonical; a reset requested on a secondary domain must not
    # mint a link to an origin that isn't in the allowlist.
    monkeypatch.setenv("BRAIN_PUBLIC_URL", "https://elyceum.app")
    req = _request({"host": "elyceum.online", "x-forwarded-proto": "https"})
    assert external_base_url(req) == "https://elyceum.app"


def test_public_url_override_tolerates_a_trailing_slash(monkeypatch):
    monkeypatch.setenv("BRAIN_PUBLIC_URL", "https://elyceum.app/")
    req = _request({"host": "elyceum.app", "x-forwarded-proto": "https"})
    assert external_base_url(req) + "/auth/reset" == "https://elyceum.app/auth/reset"


def test_blank_override_falls_back_to_headers(monkeypatch):
    # An env var set to "" (the Railway-empty-value shape) must not win.
    monkeypatch.setenv("BRAIN_PUBLIC_URL", "  ")
    req = _request({"host": "elyceum.app", "x-forwarded-proto": "https"})
    assert external_base_url(req) == "https://elyceum.app"
