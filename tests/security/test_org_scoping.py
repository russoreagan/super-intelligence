"""
Tenant isolation — every Supabase query must carry an org filter IN THE CODE.

Two independent layers are supposed to enforce multi-tenancy:

  1. Postgres RLS, driven by a gateway-minted org JWT whose `sub` IS the org id,
     so `auth.uid() = org_id` is checked by the database itself.
  2. In-query scoping: every PostgREST call hand-writes `.eq("org_id", ...)`.

Layer 1 is INERT in production. The live project migrated to asymmetric JWT
signing, so `_uses_asymmetric_signing()` is True, `mint_org_token()` returns "",
and the provisioner hands each tenant the SERVICE-ROLE key instead — which
bypasses RLS entirely (see brain/gateway/org_token.py and brain/provisioner.py,
both of which say so explicitly). Layer 2 is therefore the only thing standing
between orgs, and layer 2 is hand-written at ~80 call sites: ~80 chances to
forget one.

One was forgotten. GET /v1/mcp/tokens/{end_user_id} selected from
end_user_mcp_tokens filtered ONLY on end_user_id. That id is partner-chosen free
text (emails, "user_1") and not globally unique — the PK is
(org_id, end_user_id, server_name) — so a guessed id returned every org's rows:
which third-party services their end-users had connected, the server URLs, and
expiry. RLS had been silently covering for it; the service-key fallback removed
the cover.

This guard walks the AST of every module under brain/ and fails when a Supabase
query chain reaches a table with no org scoping. It is deliberately structural
rather than a grep: it follows the actual method chain, so it sees through
formatting and ignores same-named non-database calls.

A chain is considered SCOPED when any of:
  * it filters on org_id — `.eq("org_id", ...)` / `.in_("org_id", ...)`;
  * it is an insert/upsert whose literal payload stamps org_id onto every row; or
  * it is an upsert whose `on_conflict` target names org_id — Postgres requires the
    conflict columns to be present in the row, so the match cannot escape the org.

For writes, a payload-carried org_id is accepted because the row is written under
this org's key. The two upsert conflict targets that do NOT name org_id were
checked by hand against their PKs and are safe by construction: agent_jobs is
`job_id text primary key` (021) and speaker_profiles is `id uuid primary key`
(007) — both globally unique, so a conflict can only ever match this org's own
row. Both are allowlisted below rather than inferred, because that argument comes
from the schema, not from the call site.

Anything that can't be proven scoped from the call site must be allowlisted below
with a reason. Keep that list short — each entry is a place where isolation rests
on a human argument rather than on the code.
"""

from __future__ import annotations

import ast
from pathlib import Path

BRAIN_DIR = Path(__file__).resolve().parents[2] / "brain"

# A chain is a real PostgREST query only if it carries one of these verbs. This is
# what keeps brain/clusters/trading/tools.py's `present.table(turn_id, title, cols,
# rows)` — a UI table renderer that merely shares the method name — out of scope.
_QUERY_OPS = {"select", "insert", "update", "upsert", "delete"}
_WRITE_OPS = {"insert", "upsert"}

# Filters that constitute org scoping.
_FILTER_OPS = {"eq", "in_"}


# ── Verified exceptions ────────────────────────────────────────────────────────
# Keyed by (module path relative to brain/, table, operation) — stable across the
# line churn that would make line numbers a maintenance tax. Every entry was read
# and confirmed, not taken on trust.
ALLOWLIST: dict[tuple[str, str, str], str] = {
    # org_id_for_user(): resolves WHICH org a user belongs to. It cannot filter by
    # org_id because its whole job is to produce that value. Scoped instead by
    # user_id (the authenticated subject). Control-plane code; the tenant never
    # calls it to read another org's data.
    ("org.py", "memberships", "select"): "resolves the org id itself; scoped by user_id",
    # org_for_key(): resolves the org FROM an API key hash, same bootstrap problem.
    # The lookup key is a SHA-256 of a high-entropy secret, so it is not guessable
    # the way end_user_id was — this is the gateway deciding which tenant a request
    # belongs to, before any org context exists.
    ("api/auth.py", "api_keys", "select"): "resolves the org id itself; scoped by key_hash",
    # Snapshot pruning deletes ids that came from the org-scoped + persona-scoped
    # select immediately above it (wiring.py ~431: .eq("org_id", uid)), so the id
    # set is already this org's. Verified: `ids` has no other producer.
    ("wiring.py", "wiring_snapshots", "delete"): "ids come from the org-scoped select above",
    # Upserts a row whose org_id is set from get_org_id(); conflict target is `id`,
    # a client-side uuid4 (speaker_store.py ~102) against a globally-unique uuid PK,
    # so it can never collide with another org's row.
    ("second_brain/speaker_store.py", "speaker_profiles", "upsert"): (
        "payload stamps org_id; conflict on a client-side uuid4 PK"
    ),
    # Payload is built one frame up as a list-comp of dicts that each set
    # "org_id": org from get_org_id(); the literal isn't visible at the call site.
    ("agent_usage_store.py", "agent_usage", "insert"): "payload rows carry org_id (built above)",
    # _row(org, record) stamps org_id; conflict on job_id, a global text PK.
    ("agent_jobs_store.py", "agent_jobs", "upsert"): "_row() stamps org_id; job_id is a global PK",
    # rows are built one frame up as {"org_id": org, ...} literals.
    ("skills_registry.py", "agent_skills", "insert"): "payload rows carry org_id (built above)",
    # The webhook retry sweeper runs in the GATEWAY under the service-role key and
    # scans due deliveries ACROSS ALL orgs by design — that is its whole job (the brain
    # sleeps, so retries can't be org-scoped to one running tenant). Each row it claims
    # carries its own org_id, which the per-delivery update and the signing RPC both
    # filter on; only this initial cross-org claim scan is unscoped, deliberately.
    ("gateway/webhook_delivery.py", "webhook_deliveries", "select"): (
        "gateway sweeper claims due deliveries cross-org under service role, by design"
    ),
}


def _py_files() -> list[Path]:
    return sorted(p for p in BRAIN_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    out: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _chain(table_call: ast.Call, parents: dict[ast.AST, ast.AST]) -> list[ast.Call]:
    """Every Call in the fluent chain hanging off a `.table(...)` call.

    `sb.table("x").select("y").eq("org_id", o).execute()` nests as
    Call(Attribute(Call(Attribute(...)))), so we climb parent links while the
    shape stays `<current>.<attr>(...)`.
    """
    calls = [table_call]
    cur: ast.AST = table_call
    while True:
        attr = parents.get(cur)
        if not (isinstance(attr, ast.Attribute) and attr.value is cur):
            return calls
        call = parents.get(attr)
        if not (isinstance(call, ast.Call) and call.func is attr):
            return calls
        calls.append(call)
        cur = call


def _table_name(call: ast.Call) -> str:
    if call.args and isinstance(call.args[0], ast.Constant):
        return str(call.args[0].value)
    return "<dynamic>"


def _filters_on_org(calls: list[ast.Call]) -> bool:
    for call in calls:
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr not in _FILTER_OPS:
            continue
        if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == "org_id":
            return True
    return False


def _literal_payload_stamps_org(call: ast.Call) -> bool:
    """True when an insert/upsert payload is a literal that sets org_id on EVERY row."""
    if not call.args:
        return False
    arg = call.args[0]
    dicts: list[ast.Dict] = []
    if isinstance(arg, ast.Dict):
        dicts = [arg]
    elif isinstance(arg, ast.List | ast.Tuple):
        dicts = [e for e in arg.elts if isinstance(e, ast.Dict)]
        if len(dicts) != len(arg.elts):
            return False
    elif isinstance(arg, ast.ListComp) and isinstance(arg.elt, ast.Dict):
        dicts = [arg.elt]
    if not dicts:
        return False
    return all(
        any(isinstance(k, ast.Constant) and k.value == "org_id" for k in d.keys) for d in dicts
    )


def _on_conflict_names_org(call: ast.Call) -> bool:
    """True when an upsert's conflict target includes org_id.

    Postgres requires the conflict columns to be present in the inserted row, so a
    target of "org_id,id" both proves the payload carries an org_id and confines the
    match to that org — the upsert cannot silently overwrite another org's row.
    """
    for kw in call.keywords:
        if kw.arg == "on_conflict" and isinstance(kw.value, ast.Constant):
            cols = {c.strip() for c in str(kw.value.value).split(",")}
            return "org_id" in cols
    return False


def _violations() -> list[tuple[str, str, str, int]]:
    """(relpath, table, op, lineno) for every query chain with no provable org scoping."""
    found: list[tuple[str, str, str, int]] = []
    for path in _py_files():
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover - a broken module fails elsewhere
            continue
        parents = _parents(tree)
        rel = path.relative_to(BRAIN_DIR).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "table"):
                continue
            calls = _chain(node, parents)
            ops = {c.func.attr for c in calls if isinstance(c.func, ast.Attribute)}
            query_ops = ops & _QUERY_OPS
            if not query_ops:
                continue  # not a database call (e.g. present.table(...))
            if _filters_on_org(calls):
                continue
            writes = [
                c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr in _WRITE_OPS
            ]
            if writes and all(
                _literal_payload_stamps_org(c) or _on_conflict_names_org(c) for c in writes
            ):
                continue
            op = sorted(query_ops)[0]
            found.append((rel, _table_name(node), op, node.lineno))
    return found


def test_every_supabase_query_is_org_scoped():
    """No Supabase query may reach a table without an org filter.

    RLS is not a safety net here — production runs on the service-role key.
    """
    unexpected = [v for v in _violations() if (v[0], v[1], v[2]) not in ALLOWLIST]
    assert not unexpected, "unscoped Supabase queries (cross-tenant leak risk):\n" + "\n".join(
        f"  brain/{rel}:{line} — {table}.{op}() has no .eq('org_id', ...)"
        for rel, table, op, line in unexpected
    )


def test_allowlist_has_no_stale_entries():
    """Every exception must still correspond to a real unscoped call site.

    A stale entry is a standing permission for a leak that nobody is watching:
    if the site later gains an org filter, or moves, the entry must go — otherwise
    it silently pre-approves the next unscoped query on that table.
    """
    live = {(rel, table, op) for rel, table, op, _ in _violations()}
    stale = sorted(set(ALLOWLIST) - live)
    assert not stale, "allowlisted exceptions that no longer exist — remove them:\n" + "\n".join(
        f"  brain/{rel}: {table}.{op}() — {ALLOWLIST[(rel, table, op)]}" for rel, table, op in stale
    )


def test_guard_catches_an_unscoped_read():
    """The guard must fail on the exact shape of the bug it exists to prevent.

    This is the original GET /v1/mcp/tokens/{end_user_id} query. If this stops
    being detected, the guard has become decorative.
    """
    src = (
        "def f():\n"
        "    return (\n"
        "        _sb_client()\n"
        '        .table("end_user_mcp_tokens")\n'
        '        .select("server_name, server_url, expires_at")\n'
        '        .eq("end_user_id", end_user_id)\n'
        "        .execute()\n"
        "    )\n"
    )
    tree = ast.parse(src)
    parents = _parents(tree)
    table_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "table"
    ]
    assert len(table_calls) == 1
    chain = _chain(table_calls[0], parents)
    assert {c.func.attr for c in chain if isinstance(c.func, ast.Attribute)} & _QUERY_OPS
    assert not _filters_on_org(chain), "guard no longer detects a missing org filter"


def test_guard_accepts_the_fixed_read():
    """The same query, scoped, must pass — the guard must not just fail everything."""
    src = (
        "def f():\n"
        "    return (\n"
        "        _sb_client()\n"
        '        .table("end_user_mcp_tokens")\n'
        '        .select("server_name, server_url, expires_at")\n'
        '        .eq("org_id", _sb_org())\n'
        '        .eq("end_user_id", end_user_id)\n'
        "        .execute()\n"
        "    )\n"
    )
    tree = ast.parse(src)
    parents = _parents(tree)
    table_call = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "table"
    )
    assert _filters_on_org(_chain(table_call, parents))


def test_guard_ignores_non_database_table_calls():
    """`present.table(...)` renders a UI table. It must not be mistaken for a query."""
    src = 'async def f():\n    await present.table(turn_id, "Quote", cols, rows)\n'
    tree = ast.parse(src)
    parents = _parents(tree)
    table_call = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "table"
    )
    chain = _chain(table_call, parents)
    ops = {c.func.attr for c in chain if isinstance(c.func, ast.Attribute)}
    assert not (ops & _QUERY_OPS)


def test_on_conflict_rule_distinguishes_org_targets():
    """`on_conflict` counts as scoping only when it actually names org_id.

    "org_id,id" confines the upsert to this org. "job_id" does not — that one is
    safe only because job_id is a globally-unique PK, which is a schema fact the
    call site cannot show, so it must go through the allowlist instead.
    """

    def _upsert(src: str) -> ast.Call:
        return next(
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "upsert"
        )

    assert _on_conflict_names_org(_upsert('t.upsert(row, on_conflict="org_id,id")'))
    assert _on_conflict_names_org(_upsert('t.upsert(row, on_conflict="org_id, persona, source")'))
    assert not _on_conflict_names_org(_upsert('t.upsert(row, on_conflict="job_id")'))
    assert not _on_conflict_names_org(_upsert('t.upsert(row, on_conflict="id")'))
    assert not _on_conflict_names_org(_upsert("t.upsert(row)"))
    # Must not be fooled by a column that merely contains the substring.
    assert not _on_conflict_names_org(_upsert('t.upsert(row, on_conflict="not_org_id")'))


def test_the_mcp_tokens_endpoint_is_scoped():
    """Belt and braces on the specific regression: the shipped source must scope it."""
    src = (BRAIN_DIR / "api" / "server.py").read_text()
    assert '.eq("org_id", supabase_client.get_org_id())' in src
    idx = src.index('.table("end_user_mcp_tokens")')
    window = src[idx : idx + 700]
    assert '.eq("org_id"' in window, "end_user_mcp_tokens read lost its org filter"
