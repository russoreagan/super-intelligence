"""
Per-brain tier gate in ModelRouter — the single enforcement of local-permission.
A 'lite' brain holds no local pod, so _resolve_model_id remaps any local route to
cloud; a 'full' brain keeps local. The cloud-vs-local truth itself (cell config +
_provider_for) is unchanged — this only gates whether THIS brain may use local.
"""

from __future__ import annotations

from brain.model_router import ModelRouter, _provider_for


def _router(local_disabled: bool) -> ModelRouter:
    # Tests construct via __new__ to skip client/init; the gate only needs the flag.
    r = ModelRouter.__new__(ModelRouter)
    r._local_disabled = local_disabled
    return r


def test_lite_brain_remaps_local_to_cloud():
    r = _router(True)
    _mk, mid = r._resolve_model_id("local-code", cluster="dmn")
    assert _provider_for(mid) != "local"  # forced off the (nonexistent) pod


def test_lite_brain_remaps_runpod_to_cloud():
    r = _router(True)
    _mk, mid = r._resolve_model_id("runpod-general", cluster="dmn")
    assert _provider_for(mid) != "local"


def test_lite_brain_leaves_cloud_models_untouched():
    r = _router(True)
    _mk, mid = r._resolve_model_id("haiku", cluster="frontal")
    assert _provider_for(mid) == "anthropic"


def test_full_brain_keeps_local():
    r = _router(False)
    _mk, mid = r._resolve_model_id("local-code", cluster="dmn")
    assert _provider_for(mid) == "local"


def test_full_brain_runpod_stays_local():
    r = _router(False)
    _mk, mid = r._resolve_model_id("runpod-general", cluster="dmn")
    assert _provider_for(mid) == "local"
