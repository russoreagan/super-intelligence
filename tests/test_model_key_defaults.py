"""Every default model_key must name an entry in MODEL_MAP.

The router resolves a key with MODEL_MAP.get(key, key), so an unknown key is sent
to the provider verbatim. DeidGate and PrivateRuminator defaulted to "claude",
which went out as `model: claude` and 404'd on every call: cross-learning never
admitted a principle, and the fail-closed de-id gate rejected every self-model
rewrite for sessions with end users.
"""

from __future__ import annotations

import inspect

import pytest

from brain.deid_gate import DeidGate
from brain.model_router import MODEL_MAP
from brain.private_rumination import PrivateRuminator


@pytest.mark.parametrize("cls", [DeidGate, PrivateRuminator])
def test_default_model_key_resolves(cls):
    default = inspect.signature(cls.__init__).parameters["model_key"].default
    assert default in MODEL_MAP, f"{cls.__name__} default {default!r} is not a MODEL_MAP key"
