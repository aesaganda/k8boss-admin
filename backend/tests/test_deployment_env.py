"""docker-compose.yml passes every setting the backend reads.

The failure this prevents, which shipped twice before this file existed.

Compose interpolates ``.env`` into the compose *file*. It does not pass it into
the container. A setting that ``app/config.py`` reads but the backend service's
``environment:`` block does not list is therefore not "inherited from .env" — it
reaches the process as its default, and nothing anywhere says so.

What that looks like from the operator's chair: they read the variable's row in
the README, set it in ``.env``, restart the stack, and the console tells them
the feature is disabled. There is no error, no warning, and no log line — the
value they set was read by compose, used to substitute nothing, and discarded.
``ADMIN_ROUTER_MANAGE_ENABLED`` and ``ADMIN_NODE_DEBUG_ENABLED`` both spent
releases in that state, and the second was only found because someone went
looking after the first.

Deliberately NOT extended to ``deploy/deployment.yaml``. That file lists a
curated subset on purpose — a Kubernetes operator patches env onto a Deployment
rather than expecting all 59 knobs pre-declared — so the same rule there would
need a 33-name exemption list, which is the advisory-lint mistake in test form:
a check that exists, passes, and means nothing.
"""

from __future__ import annotations

import pathlib

import yaml

from app.config import Settings

#: Settings with no row in the compose ``environment:`` block, each because
#: compose already decides the answer or the setting has no place in this
#: deployment shape. Anything not named here must be listed.
#:
#: This is an allowlist, not a suppression list: adding a name is a claim that
#: an operator setting it in .env has no business taking effect, and that claim
#: gets reviewed. If a name lands here to make a test pass, the test has been
#: turned off for that variable.
_NOT_PASSED_THROUGH = {
    # Not an operator knob — it is the reported version of the running build.
    "APP_VERSION",
    # Compose fixes the cookie name by fixing the deployment shape: one backend
    # behind one nginx on one origin. Renaming it only matters when two consoles
    # share a domain, which this file cannot produce.
    "AUTH_COOKIE_NAME",
    # The key file's location is decided by the volume mount, not by env: the
    # named volume is what keeps the key and the database it protects together.
    # Pointing this elsewhere would separate them, which is the failure the
    # volume comment in docker-compose.yml exists to prevent.
    "ENCRYPTION_KEY_FILE",
    # Selects a context inside a kubeconfig, and compose mounts the host's
    # kubeconfig read-only as a local-development fallback only. Clusters are
    # registered through the UI.
    "KUBE_CONTEXT",
}


def _env_var_name(field_name: str, field) -> str:
    """The variable pydantic-settings will actually read for this field.

    Mirrors its own rule rather than assuming every field carries an explicit
    alias: an uppercase entry in ``AliasChoices`` when there is one, and the
    field name uppercased otherwise (``case_sensitive`` is False and
    ``env_prefix`` is empty). Keying this test on ``AliasChoices`` alone would
    have covered 11 of 59 fields and called that a pass.
    """
    choices = getattr(field.validation_alias, "choices", None) or []
    explicit = [c for c in choices if isinstance(c, str) and c.isupper()]
    return explicit[0] if explicit else field_name.upper()


def _compose_backend_env() -> set[str]:
    root = pathlib.Path(__file__).resolve().parent.parent.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    entries = compose["services"]["backend"]["environment"]
    return {entry.split("=", 1)[0] for entry in entries}


def test_every_setting_is_passed_into_the_backend_container():
    """A setting the backend reads and compose does not forward is unreachable."""
    expected = {
        _env_var_name(name, field) for name, field in Settings.model_fields.items()
    }
    missing = expected - _compose_backend_env() - _NOT_PASSED_THROUGH

    assert not missing, (
        "app/config.py reads these, and docker-compose.yml does not pass them "
        f"into the backend container: {sorted(missing)}. Setting one in .env "
        "will appear to work and do nothing — compose substitutes it into the "
        "compose file, not into the process. Add a row to the backend service's "
        "`environment:` block, or add the name to _NOT_PASSED_THROUGH with the "
        "reason it should not reach the process."
    )


def test_compose_passes_nothing_the_backend_does_not_read():
    """The other direction, and it fails just as quietly.

    ``Settings`` is configured ``extra="ignore"``, so a misspelled variable in
    the compose block is accepted by the container and dropped by pydantic. The
    operator sets it, the value arrives, nothing reads it, and the symptom is
    identical to the bug above — a setting that has no effect and no complaint.
    """
    known = {
        _env_var_name(name, field) for name, field in Settings.model_fields.items()
    }
    unread = _compose_backend_env() - known

    assert not unread, (
        "docker-compose.yml passes these into the backend and app/config.py "
        f"reads none of them: {sorted(unread)}. Settings is extra=\"ignore\", so "
        "the container accepts them and pydantic drops them silently. Fix the "
        "spelling or delete the row."
    )
