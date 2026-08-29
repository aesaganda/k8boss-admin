#!/usr/bin/env python3
"""
Regenerate ``deploy/router.yaml`` from ``app.admin.router_bundle`` (§14).

The checked-in manifest exists so that ``kubectl apply -f deploy/router.yaml``
and pressing Install in the console are demonstrably the same install, and so
that bumping ``ROUTER_VERSION`` shows up as a reviewable diff over real
manifests rather than as one changed string in a Python file.

Generated rather than hand-maintained, and enforced rather than trusted:
``tests/test_router.py::test_the_shipped_manifest_matches_the_bundle`` fails the
build when the two disagree. A checked-in copy that could drift would document
one router while the console installed another, which is the same defect class
as an advisory lint — a check that exists, passes, and means nothing.

    make router-manifest
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# app.config builds its Settings at import time and app.database builds the
# engine from them, so these have to exist before any `app.*` import. Nothing
# here touches either; they are only needed to get through module import.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ENCRYPTION_KEY", "render-router-manifest")

from app.admin import router_bundle  # noqa: E402
from app.resources import reader  # noqa: E402

HEADER = """\
# The router k8boss-admin ships (§14) — the same objects the console installs.
#
# GENERATED. Do not edit by hand: `backend/app/admin/router_bundle.py` is the
# source, and `backend/tests/test_router.py::test_the_shipped_manifest_matches_the_bundle`
# fails the build if this file and that module disagree. Regenerate with:
#
#     make router-manifest
#
# It exists so that `kubectl apply -f deploy/router.yaml` and pressing Install in
# the console are demonstrably the same install, and so that every version bump
# shows up as a reviewable diff rather than as one changed string in a Python
# file. It carries the DEFAULT options — namespace k8boss-router, a
# LoadBalancer Service, two replicas, the `haproxy` IngressClass, not the
# cluster default, no Gateway API. The console lets an operator change all five;
# this file is what they get if they change none.
#
# READ THE ClusterRole BEFORE APPLYING. It grants get/list/watch on Secrets
# cluster-wide, which is what any ingress controller needs to terminate TLS and
# which RBAC cannot narrow to "only the referenced ones". Applying this gives
# the router's ServiceAccount the ability to read every Secret in the cluster.
"""


def render() -> str:
    """The manifest text, from the bundle's default options."""
    documents = [
        reader.to_yaml(item.body)
        for item in router_bundle.build(router_bundle.RouterOptions())
    ]
    return HEADER + "---\n" + "\n---\n".join(documents)


def main() -> int:
    target = ROOT / "deploy" / "router.yaml"
    rendered = render()
    if target.exists() and target.read_text() == rendered:
        print(f"{target} is already up to date.")
        return 0
    target.write_text(rendered)
    print(f"Wrote {target} ({router_bundle.ROUTER_VERSION}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
