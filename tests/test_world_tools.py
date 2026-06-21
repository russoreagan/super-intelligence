"""Unit tests for the Google Maps world-grounding motor tools.

All HTTP is mocked — these never hit the network or bill the account. They cover:
  - the motor_enable_world gate (off → blocked, no call made)
  - the missing-key guard
  - response parsing into compact summaries for each tool

Live end-to-end coverage is exercised separately (see the plan's smoke tests).
"""

from __future__ import annotations

import asyncio

import pytest

from brain.clusters.motor_dispatcher import ToolDispatcher


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def raise_for_status(self) -> None:  # noqa: D401 - mimic httpx
        return None

    def json(self) -> dict:
        return self._p


class _FakeClient:
    """Stands in for httpx.AsyncClient; returns one canned payload for GET/POST."""

    def __init__(self, payload: dict) -> None:
        self._p = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return _FakeResp(self._p)

    async def post(self, *a, **k):
        return _FakeResp(self._p)


def _patch_httpx(monkeypatch, payload: dict) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(payload))


def test_world_gate_off_is_blocked(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    d = ToolDispatcher(enable_world=False)
    out = asyncio.run(d._world_geocode("Eiffel Tower"))
    assert out.startswith("[blocked]")


def test_world_missing_key_errors(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    d = ToolDispatcher(enable_world=True)
    out = asyncio.run(d._world_geocode("Eiffel Tower"))
    assert out.startswith("[error]") and "GOOGLE_MAPS_API_KEY" in out


def test_world_geocode_parses(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    _patch_httpx(
        monkeypatch,
        {
            "status": "OK",
            "results": [
                {
                    "formatted_address": "Av. Gustave Eiffel, 75007 Paris, France",
                    "geometry": {"location": {"lat": 48.8584, "lng": 2.2945}},
                }
            ],
        },
    )
    d = ToolDispatcher(enable_world=True)
    out = asyncio.run(d._world_geocode("Eiffel Tower"))
    assert out.startswith("[world:geocode]")
    assert "Paris" in out and "48.85840" in out


def test_world_places_parses(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    _patch_httpx(
        monkeypatch,
        {
            "places": [
                {
                    "displayName": {"text": "Blue Bottle Coffee"},
                    "formattedAddress": "1 Ferry Building, SF",
                    "rating": 4.5,
                    "currentOpeningHours": {"openNow": True},
                }
            ]
        },
    )
    d = ToolDispatcher(enable_world=True)
    out = asyncio.run(d._world_places("coffee", "San Francisco"))
    assert out.startswith("[world:places]")
    assert "Blue Bottle Coffee" in out and "★4.5" in out and "open" in out


def test_world_directions_parses(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    # coords for origin/destination so no geocode round-trip is needed
    _patch_httpx(monkeypatch, {"routes": [{"distanceMeters": 5700, "duration": "4860s"}]})
    d = ToolDispatcher(enable_world=True)
    out = asyncio.run(d._world_directions("40.7,-74.0", "40.75,-73.99", "WALK"))
    assert out.startswith("[world:directions]")
    assert "5.7 km" in out and "81 min" in out and "walk" in out


def test_world_weather_parses_with_coords(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    _patch_httpx(
        monkeypatch,
        {
            "temperature": {"degrees": 17.5},
            "feelsLikeTemperature": {"degrees": 16.0},
            "relativeHumidity": 70,
            "weatherCondition": {"description": {"text": "Mostly sunny"}},
        },
    )
    d = ToolDispatcher(enable_world=True)
    out = asyncio.run(d._world_weather("37.77,-122.41"))
    assert out.startswith("[world:weather]")
    assert "Mostly sunny" in out and "17.5°C" in out


def test_world_air_quality_parses_with_coords(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    _patch_httpx(
        monkeypatch,
        {"indexes": [{"aqi": 80, "category": "Excellent air quality", "dominantPollutant": "o3"}]},
    )
    d = ToolDispatcher(enable_world=True)
    out = asyncio.run(d._world_air_quality("37.77,-122.41"))
    assert out.startswith("[world:air]")
    assert "AQI 80" in out and "o3" in out


def test_world_timezone_parses_with_coords(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    _patch_httpx(
        monkeypatch,
        {
            "status": "OK",
            "timeZoneId": "America/Los_Angeles",
            "timeZoneName": "Pacific Daylight Time",
            "rawOffset": -28800,
            "dstOffset": 3600,
        },
    )
    d = ToolDispatcher(enable_world=True)
    out = asyncio.run(d._world_timezone("37.77,-122.41"))
    assert out.startswith("[world:tz]")
    assert "America/Los_Angeles" in out and "UTC-7.0" in out


def test_world_tools_registered_in_dispatchable_set():
    from brain.clusters import motor_cortex as MC

    assert MC._WORLD_TOOLS <= MC._DISPATCHABLE_TOOLS
    assert "world_geocode" in MC._WORLD_TOOLS


@pytest.mark.parametrize("flag", [0, 1])
def test_world_setting_default_and_plumbing(flag):
    """api_key_google_maps reaches the vault; motor_enable_world defaults off."""
    from brain import settings as S
    from brain import vault

    assert "google_maps" in vault.VALID_PROVIDERS
    assert S.DEFAULTS["motor_enable_world"] == 0
    # the toggle is a normal int setting the dispatcher reads via _eff_enable_world
    d = ToolDispatcher(enable_world=bool(flag))
    assert d._enable_world is bool(flag)
