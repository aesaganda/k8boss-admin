"""
Which layer may import which, and which routes may change something.

`docs/architecture.md` states the direction and CLAUDE.md's subsystem map
repeats it: `admin/` is **every write** and may use `resources/` and
`services/`; the read layers may not reach back. This is the test, because the
rule is otherwise enforced by nobody — an import that went the wrong way would
compile, pass, and be discovered years later by somebody trying to understand
why reading one pod pulls the write funnel into the graph.

That is not hypothetical. `app/services/pods.py` imported `read_object` from
`app/admin/apply.py` for exactly as long as this test did not exist. The
function was misfiled rather than misused — a read helper living in the write
module — and the fix was to move it, not to route around it.

The last two tests here read the same tree for a different property: not where
a write lives, but whether every route that changes something is gated at all.
Same reason for reading the source rather than the behaviour — the endpoint
that was missing its gate returned the right shape, the right status, and
nothing that could fail.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"

#: Layers that must not depend on the write funnel, and what each one is for.
READ_LAYERS = {
    "resources": "discovery, the generic reader, the envelope — used *by* the writes",
    "services": "typed read models. Nothing here writes",
    "identity": "who is signed in to the console. Nothing to do with a cluster write",
    "audit": "the trail. `admin` writes to it, never the other way",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("layer", sorted(READ_LAYERS))
def test_no_read_layer_imports_the_write_layer(layer):
    """`admin` may use these; none of them may use `admin`.

    A docstring mentioning `app.admin` is fine and several do — the modules
    explain where writes go. An *import* is the thing that makes the dependency
    real, so that is what this reads.
    """
    offenders = {
        path.relative_to(BACKEND).as_posix(): sorted(
            module for module in _imports(path) if module.startswith("app.admin")
        )
        for path in sorted((APP / layer).glob("*.py"))
    }
    offenders = {path: modules for path, modules in offenders.items() if modules}

    assert offenders == {}, (
        f"app/{layer}/ is {READ_LAYERS[layer]}, so it must not import the write "
        f"layer: {offenders}"
    )


def test_only_one_module_declares_the_dry_run_default():
    """§0.3's default lives in one place, and this is what keeps it there.

    `dryRun` defaults to true and its alias accepts `dry_run` as well, and both
    halves are safety: a client that forgets the field gets a projection, and a
    client that sends the snake_case spelling is understood rather than
    silently falling back — which for this field means an operator asking for a
    real drain, receiving a dry run, and being told `applied: false` about a
    node they believe is now empty.

    Eight modules declared it: three private `_MutationBody` classes, one of
    whose docstrings read "See §6's copy for why", and five inline. All eight
    were correct. That was the problem — eight chances to stop being correct,
    with nothing that would fail if the ninth got it wrong, and a flipped
    default looking identical from outside until a cluster changed.
    """
    declarers = sorted(
        path.name for path in (APP / "api").glob("*.py")
        if 'alias="dryRun"' in path.read_text()
    )

    assert declarers == ["bodies.py"], (
        "subclass app.api.bodies.MutationBody instead of redeclaring dry_run; "
        "an override takes the default with it"
    )


def test_every_write_body_defaults_to_a_dry_run():
    """The property itself, not just where it is written.

    Inheriting from the right base is the mechanism; this is the promise. A
    subclass that overrode `dry_run` would pass the test above and fail here.
    """
    import os

    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from app.api import cli, debug, nodes, portal, projects, routes, workloads
    from app.api.bodies import MutationBody

    checked = 0
    for module in (cli, debug, nodes, portal, projects, routes, workloads):
        for name in dir(module):
            candidate = getattr(module, name)
            if not isinstance(candidate, type) or not issubclass(type(candidate), type):
                continue
            fields = getattr(candidate, "model_fields", None)
            if not fields or "dry_run" not in fields:
                continue
            field = fields["dry_run"]
            assert field.default is True, f"{module.__name__}.{name} defaults to a write"
            assert field.alias == "dryRun", f"{module.__name__}.{name} lost the alias"
            assert issubclass(candidate, MutationBody), (
                f"{module.__name__}.{name} has a dry_run that did not come from "
                "MutationBody"
            )
            checked += 1

    assert checked >= 10, f"only {checked} write bodies found; the sweep missed some"


def test_the_transport_is_not_owned_by_the_write_layer():
    """`request_json` is one authenticated round trip that keeps its headers.

    Both directions use it — the funnel's POST and PATCH, and `read_object`'s
    GET — so it lives in `resources/`. While it lived in `admin/apply.py`, a
    read model that wanted one object had to import from the write module to
    get there.
    """
    from app.resources import transport

    assert transport.request_json is not None
    assert "app.admin" not in {
        module for module in _imports(APP / "resources" / "transport.py")
    }


# --------------------------------------------------------------------------- #
# Who may change something
# --------------------------------------------------------------------------- #

#: The dependencies from `app.identity.dependencies` that decide a *role*.
#:
#: Matched by name rather than by substring on purpose: `app.admin.mutate`
#: exports `require_open`, which gates on `ADMIN_ALLOW_MUTATIONS` and says
#: nothing about who is calling. A substring rule would read that as a role
#: check and wave through the exact class of endpoint this test exists to catch.
ROLE_DEPENDENCIES = {"require_admin", "require_console_admin"}

#: State-changing routes that are neither funnelled nor role-gated, and why
#: each one is allowed to be. `(module, handler)` → the reason.
#:
#: **Every entry is a decision, not an observation.** If a route you just wrote
#: fails the test below, the answer is a gate or a funnel call — adding a line
#: here because the test is red turns it into a list of everything that exists,
#: which is a test that can never fail again.
UNGATED_BY_DESIGN = {
    ("auth.py", "login"): (
        "establishes the identity every other gate reads. A role dependency here "
        "is a deadlock: nobody could ever acquire the role. Throttled by "
        "app.identity.throttle and recorded in the trail like a cluster write"
    ),
    ("auth.py", "logout"): (
        "ends the caller's own session and can touch nobody else's — the session "
        "cookie is the whole input"
    ),
    ("auth.py", "saml_acs"): (
        "the identity provider POSTs the assertion here, unauthenticated by "
        "definition. What it trusts is the signature on the assertion, checked in "
        "app.identity.saml — not a console role the browser does not have yet"
    ),
    ("quota.py", "preview_quota"): (
        "arithmetic, not admission (§29). POST carries a pod spec too large for a "
        "query string; `advise()` reads and returns a verdict, and writes nothing "
        "to a cluster or to this console"
    ),
    ("access.py", "post_preflight"): (
        "§9 is a question about permissions, not a use of them. "
        "SelfSubjectAccessReview asks about the caller's own access"
    ),
    ("access.py", "post_subject_review"): (
        "§23 asks the API server what another subject may do and takes no action "
        "as them. Privileged enough to audit, which it does — one row per request "
        "naming who asked about whom — but still a read"
    ),
    ("clusters.py", "test_cluster"): (
        "stamps the outcome of a connection the registered row already describes: "
        "`status`, `server_version`, `last_connected`. Every field written is "
        "derived from that round trip rather than from the caller, so unlike the "
        "three CRUD endpoints beside it there is no input that could re-point a "
        "cluster or retarget its stored token"
    ),
}


def _admin_modules_reaching_the_funnel() -> set[str]:
    """Which `app/admin/*.py` stems end at `mutate()`, directly or via a sibling.

    Importing *something* from `app.admin` is not the property worth asserting:
    `preflight` and `access_review` live there and are explicitly not writes, so
    a handler that called one of them would otherwise look funnelled. The
    reachable set is computed instead, so `routes`, `portal`, `olm` and
    `namespace_delete` — which reach `mutate` through `app.admin.apply` — count,
    and the two authorization modules do not.
    """
    calls: dict[str, bool] = {}
    siblings: dict[str, set[str]] = {}
    for path in sorted((APP / "admin").glob("*.py")):
        stem = path.stem
        tree = ast.parse(path.read_text())
        calls[stem] = any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "mutate")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "mutate")
            )
            for node in ast.walk(tree)
        )
        siblings[stem] = {
            alias.name if node.module == "app.admin" else node.module.split(".")[2]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "app.admin" or node.module.startswith("app.admin."))
            for alias in node.names
        }

    # `mutate` is seeded rather than detected: `mutate.py` defines the funnel and
    # has no reason to call it, so the call scan alone would leave the one module
    # that *is* the answer out of the set of modules that reach it.
    reaching = {"mutate"} | {stem for stem, hit in calls.items() if hit}
    while True:
        grown = reaching | {
            stem for stem, imported in siblings.items() if imported & reaching
        }
        if grown == reaching:
            return reaching
        reaching = grown


def _admin_bindings(tree: ast.Module) -> dict[str, str]:
    """Local name → the `app/admin/` module stem it came from.

    Covers both spellings in use: `from app.admin import routes as routes_admin`
    binds the module, `from app.admin.scale import scale_workload` binds a
    function out of it.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module == "app.admin":
            for alias in node.names:
                bound[alias.asname or alias.name] = alias.name
        elif node.module.startswith("app.admin."):
            for alias in node.names:
                bound[alias.asname or alias.name] = node.module.split(".")[2]
    return bound


def _state_changing_routes():
    """Every POST/PUT/PATCH/DELETE handler in `app/api/`, with how it is gated.

    Yields `(module, handler, method, route_source, admin_stems, role_deps)`.
    The route is kept as written rather than evaluated — several are built from
    a module-level prefix constant — because it is only ever shown to a human
    reading a failure.
    """
    for path in sorted((APP / "api").glob("*.py")):
        tree = ast.parse(path.read_text())
        bound = _admin_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in {"post", "put", "patch", "delete"}:
                    continue
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                # Both places a dependency can be declared: in the decorator's
                # `dependencies=[...]`, and as a `Depends()` default on a
                # parameter. The funnel-backed endpoints use neither, so missing
                # one would not show up as a pass — it would show up here.
                declared = set(names)
                for kw in dec.keywords:
                    if kw.arg == "dependencies":
                        declared |= {
                            n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)
                        }
                yield (
                    path.name,
                    node.name,
                    dec.func.attr.upper(),
                    ast.unparse(dec.args[0]) if dec.args else "?",
                    {bound[name] for name in names if name in bound},
                    declared & ROLE_DEPENDENCIES,
                )


def test_every_state_changing_route_is_funnelled_or_role_gated():
    """A write reaches `mutate()` or it names the role allowed to make it.

    Cluster create/update/delete carried neither. Any signed-in user could PUT a
    new `api_server` onto a registered cluster while omitting `token` — which
    keeps the stored credential, exactly as `ClusterUpdate` promises — and the
    next request handed that cluster's real bearer token to a host the caller
    picked. It is fixed, and nothing in this suite would have noticed a fourth
    endpoint arriving beside those three without the gate: same envelope, same
    status, no failing test, and the damage is silent.

    Two ways to be safe, because there are two kinds of write here. A cluster
    write goes through `app/admin/`, where §0.2's preflight decides against the
    operator's own RBAC. A write to *this console's* database — registrations,
    users — has no cluster RBAC to consult, so it has to say out loud which role
    may perform it.
    """
    funnel = _admin_modules_reaching_the_funnel()

    ungated = {}
    for module, handler, method, route, stems, roles in _state_changing_routes():
        if stems & funnel or roles:
            continue
        if (module, handler) in UNGATED_BY_DESIGN:
            continue
        ungated[f"{module}::{handler}"] = f"{method} {route}"

    assert ungated == {}, (
        "these routes change state and are neither routed through app/admin/ nor "
        f"gated on a role: {ungated}. Add the gate, or — if it genuinely needs "
        "neither — add it to UNGATED_BY_DESIGN with the reason"
    )


def test_the_exemptions_still_name_routes_that_exist():
    """An allowlist nobody prunes is how one grows into a list of everything.

    A stale entry is worse than a missing one: it silently pre-approves whatever
    handler later takes that name back.
    """
    live = {(module, handler) for module, handler, *_ in _state_changing_routes()}
    stale = sorted(entry for entry in UNGATED_BY_DESIGN if entry not in live)

    assert stale == [], f"UNGATED_BY_DESIGN names routes that are gone: {stale}"
