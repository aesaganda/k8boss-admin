"""
Which layer may import which.

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
