"""
Outbound-request safety: reject URLs that resolve to internal addresses (SSRF).

A single guard shared by everything that makes a request to a caller-supplied URL —
the motor cortex's `fetch_url` tool and webhook delivery (brain/api/webhooks.py). It
was previously inlined in motor_dispatcher; delivery needs the same check, and a
second copy would inevitably drift.

The important refinement over the inline version: this returns the **resolved IPs**,
so a caller can connect to a pinned IP rather than re-resolving the hostname. The
naive check-then-connect pattern is vulnerable to DNS rebinding — a hostname that
resolves to a public IP during validation and a private one microseconds later, when
the HTTP client resolves it again. Pinning closes that window; delivery also re-runs
the guard on every retry.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import NamedTuple
from urllib.parse import urlparse, urlunparse


class UnsafeUrlError(ValueError):
    """A URL is not safe to request (bad scheme, missing host, or an address in a
    private/reserved/loopback/link-local/multicast range)."""


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 — cloud metadata endpoint lives here
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_url(url: str, *, allow_http: bool = False) -> list[str]:
    """Validate `url` for an outbound request and return its resolved IP strings.

    Raises UnsafeUrlError if the scheme is not permitted, the host is empty or a bare
    `.local` name, DNS resolution fails, or **any** resolved address is internal —
    "any", because a hostname with several A/AAAA records must not slip through on one
    public record while another points inward.

    `allow_http=False` (the default, and what hosted delivery uses) requires https.
    getaddrinfo is blocking; a caller on the event loop should wrap this in an
    executor."""
    parsed = urlparse(url)
    allowed = ("http", "https") if allow_http else ("https",)
    if parsed.scheme not in allowed:
        raise UnsafeUrlError(f"scheme {parsed.scheme!r} not allowed (need {'/'.join(allowed)})")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("userinfo in the URL is not allowed")
    host = (parsed.hostname or "").lower()
    if not host or host.endswith(".local"):
        raise UnsafeUrlError(f"host {host!r} is not permitted")

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UnsafeUrlError(f"could not resolve host {host!r}: {e}") from e

    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        if _ip_is_blocked(ipaddress.ip_address(addr)):
            raise UnsafeUrlError(f"{host!r} resolves to a private/reserved address ({addr})")
        ips.append(addr)
    if not ips:
        raise UnsafeUrlError(f"{host!r} did not resolve to any address")
    return ips


class PinnedRequest(NamedTuple):
    """The pieces needed to make an httpx request against a vetted IP while still
    presenting the intended hostname. `url` has the pinned IP swapped into the
    authority (so the socket connects to the address `validate_url` vetted, not
    whatever DNS returns at connect time); `headers` carries the `Host` the origin
    should see; `extensions` carries `sni_hostname` for TLS so SNI and certificate
    verification run against the real hostname, not the IP."""

    url: str
    headers: dict
    extensions: dict


def pin_request(url: str, ip: str) -> PinnedRequest:
    """Build a `PinnedRequest` for `url` targeting one of its `validate_url`-vetted
    IPs. Pass the result to httpx as::

        r = await client.get(pinned.url, headers={**pinned.headers, ...},
                             extensions=pinned.extensions)

    with `follow_redirects=False` on the client — connecting by IP only closes the
    DNS-rebind window for the URL you validated; a redirect target is a *new* URL that
    must be validated and pinned in turn. Preserves scheme, port, path, query and
    fragment; brackets IPv6 literals."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        netloc_ip += f":{parsed.port}"
    ip_url = urlunparse(parsed._replace(netloc=netloc_ip))
    host_hdr = host
    if parsed.port and parsed.port not in (80, 443):
        host_hdr = f"{host}:{parsed.port}"
    extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}
    return PinnedRequest(ip_url, {"Host": host_hdr}, extensions)
