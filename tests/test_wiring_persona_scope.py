"""Per-persona wiring isolation: one brain process binds personas per turn, so Hebbian
updates under one persona must not bleed into another's graph (the fix that lets each
forecasting-council member learn independently)."""

from brain.wiring import Wiring
from brain.second_brain.store import bind_persona


def test_hebbian_update_is_persona_scoped():
    w = Wiring()  # boot/construction persona
    edge = ("a", "b")

    with bind_persona("persona_x"):
        w.add(*edge, weight=1.0)
        w.hebbian_update(list(edge), 0.5)
        wx = w.get_edge_weight(*edge)

    with bind_persona("persona_y"):
        w.add(*edge, weight=1.0)
        wy = w.get_edge_weight(*edge)

    assert wx > 1.0  # X learned
    assert wy == 1.0  # Y independent — untouched by X's update
    assert wx != wy

    # X's learning persists across a re-bind (in-memory state is keyed by persona).
    with bind_persona("persona_x"):
        assert w.get_edge_weight(*edge) == wx


def test_persona_name_follows_bound_persona():
    w = Wiring()
    with bind_persona("persona_z"):
        assert w._persona_name() == "persona_z"
