"""
The vault is keyed by ORG, not by user (migration 045).

Why this file exists at all: tests/security/test_org_scoping.py's AST guard only
walks `.table()` chains, so `.rpc()` calls are invisible to it. The vault is
entirely RPC-based, which means it gets no automatic tenancy safety net — these
tests are the net.

The bug being guarded against: `fetch_*` was called with an ORG id while the
underlying table was keyed by `auth.users.id`. That resolved only because every
org was a "personal org" seeded with id == its owner's user id. An org with a
fresh uuid read nothing, the spawn gate saw no Anthropic key, and the gateway
redirected to /keys forever.
"""

from __future__ import annotations

import pytest

import brain.vault as vault


class _FakeRPC:
    def __init__(self, parent, name, params):
        self._parent = parent
        self._name = name
        self._params = params

    def execute(self):
        self._parent.calls.append((self._name, self._params))
        if self._name in self._parent.missing:
            raise RuntimeError(
                f"Could not find the function public.{self._name} in the schema cache (PGRST202)"
            )
        if self._name in self._parent.raises:
            raise self._parent.raises[self._name]
        import types

        return types.SimpleNamespace(data=self._parent.data.get(self._name, {}))


class _FakeClient:
    """Records every RPC name + params, and can simulate a function that does not
    exist yet (the pre-045 deploy window)."""

    def __init__(self, data=None, missing=(), raises=None):
        self.data = data or {}
        self.missing = set(missing)
        self.raises = raises or {}
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name, params):
        return _FakeRPC(self, name, params)


@pytest.fixture
def fake_sb(monkeypatch):
    """Patch the service-role client brain.vault reaches for."""
    holder = {}

    def _install(client):
        import brain.second_brain.supabase_client as sc

        monkeypatch.setattr(sc, "get_client", lambda: client)
        holder["client"] = client
        return client

    return _install


# ── read path ────────────────────────────────────────────────────────────────


def test_fetch_org_keys_asks_for_the_org(fake_sb):
    c = fake_sb(_FakeClient(data={"get_org_api_keys": {"anthropic": "sk-x"}}))
    assert vault.fetch_org_keys("org-uuid") == {"anthropic": "sk-x"}
    assert c.calls == [("get_org_api_keys", {"p_org_id": "org-uuid"})]


def test_fetch_org_keys_falls_back_once_before_the_migration(fake_sb):
    """Code may deploy BEFORE 045 is pushed. get_org_api_keys does not exist yet,
    so the read retries 003's entry point exactly once and returns its answer."""
    c = fake_sb(
        _FakeClient(
            data={"get_user_api_keys": {"anthropic": "legacy"}},
            missing=["get_org_api_keys"],
        )
    )
    assert vault.fetch_org_keys("org-uuid") == {"anthropic": "legacy"}
    assert [name for name, _ in c.calls] == ["get_org_api_keys", "get_user_api_keys"]
    assert c.calls[1][1] == {"p_uid": "org-uuid"}


def test_fetch_org_keys_does_not_mask_a_real_failure(fake_sb):
    """Only 'the function is missing' may fall back. A permission error or an
    outage must propagate — retrying the legacy RPC would report a live fault as
    a missing migration, and (worse) could answer from the wrong row."""
    c = fake_sb(
        _FakeClient(
            data={"get_user_api_keys": {"anthropic": "legacy"}},
            raises={"get_org_api_keys": RuntimeError("permission denied for function")},
        )
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        vault.fetch_org_keys("org-uuid")
    assert [name for name, _ in c.calls] == ["get_org_api_keys"]


@pytest.mark.parametrize(
    "exc,expected",
    [
        (RuntimeError("Could not find the function public.get_org_api_keys"), True),
        (RuntimeError("PGRST202: not in schema cache"), True),
        (RuntimeError("42883 undefined_function"), True),
        (RuntimeError("permission denied for function get_org_api_keys"), False),
        (RuntimeError("connection refused"), False),
    ],
)
def test_undefined_function_detection_is_narrow(exc, expected):
    assert vault._is_undefined_function(exc) is expected


def test_apply_org_keys_to_env_prefers_brain_org_id(fake_sb, monkeypatch):
    c = fake_sb(_FakeClient(data={"get_org_api_keys": {"anthropic": "sk-env"}}))
    monkeypatch.setenv("BRAIN_ORG_ID", "the-org")
    monkeypatch.setenv("BRAIN_USER_ID", "the-user")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    applied = vault.apply_org_keys_to_env()

    assert applied == ["ANTHROPIC_API_KEY"]
    assert c.calls[0][1] == {"p_org_id": "the-org"}


def test_apply_org_keys_to_env_falls_back_to_brain_user_id(fake_sb, monkeypatch):
    """The provisioner sets both vars to the org id, and BRAIN_USER_ID is the
    older name for it — a brain spawned before the rename must still resolve."""
    c = fake_sb(_FakeClient(data={"get_org_api_keys": {}}))
    monkeypatch.delenv("BRAIN_ORG_ID", raising=False)
    monkeypatch.setenv("BRAIN_USER_ID", "legacy-pin")

    vault.apply_org_keys_to_env()

    assert c.calls[0][1] == {"p_org_id": "legacy-pin"}


def test_apply_org_keys_to_env_without_a_pin_is_a_noop(fake_sb, monkeypatch):
    c = fake_sb(_FakeClient())
    monkeypatch.delenv("BRAIN_ORG_ID", raising=False)
    monkeypatch.delenv("BRAIN_USER_ID", raising=False)

    assert vault.apply_org_keys_to_env() == []
    assert c.calls == []


# ── write path ───────────────────────────────────────────────────────────────


class _FakeUserClient(_FakeClient):
    pass


@pytest.fixture
def fake_user_client(monkeypatch):
    c = _FakeUserClient()
    monkeypatch.setattr(vault, "_user_client", lambda _token: c)
    return c


def test_set_key_targets_the_named_org(fake_user_client):
    vault.set_key("org-b", "tok", "anthropic", "sk-new")
    assert fake_user_client.calls == [
        ("set_org_api_key", {"p_org_id": "org-b", "p_provider": "anthropic", "p_value": "sk-new"})
    ]


def test_delete_key_targets_the_named_org(fake_user_client):
    vault.delete_key("org-b", "tok", "anthropic")
    assert fake_user_client.calls == [
        ("delete_org_api_key", {"p_org_id": "org-b", "p_provider": "anthropic"})
    ]


def test_get_status_reads_the_same_org_the_gate_reads(fake_user_client):
    """The 'two truths' regression: status used to read the USER's row while the
    spawn gate read the ORG's, so /keys could show a key on file while the gate
    refused to spawn and bounced the user back to /keys."""
    vault.get_status("org-b", "tok")
    assert fake_user_client.calls == [("get_org_api_key_status", {"p_org_id": "org-b"})]


@pytest.mark.parametrize("call", ["set", "delete", "status"])
def test_org_id_is_required(fake_user_client, call):
    """Never let a blank org reach the RPC: an empty p_org_id would raise deep in
    Postgres instead of here, where the caller can see which argument was wrong."""
    with pytest.raises(ValueError, match="org_id required"):
        if call == "set":
            vault.set_key("", "tok", "anthropic", "sk")
        elif call == "delete":
            vault.delete_key("", "tok", "anthropic")
        else:
            vault.get_status("", "tok")


def test_unknown_provider_is_refused_before_any_rpc(fake_user_client):
    with pytest.raises(ValueError, match="unknown provider"):
        vault.set_key("org-b", "tok", "not-a-provider", "sk")
    assert fake_user_client.calls == []


def test_blank_value_never_wipes_a_stored_key(fake_user_client):
    """Blank means 'leave unchanged' in the settings convention this mirrors."""
    vault.set_key("org-b", "tok", "anthropic", "   ")
    assert fake_user_client.calls == []
