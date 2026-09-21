# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project has not
reached 1.0 — see [SECURITY.md](SECURITY.md) for what "supported" means
before then; expect breaking changes between minor versions until it does.

## [Unreleased]

### Added

- A cluster adopted or imported from a context whose name identifies nothing is
  named after its distribution instead. Recognising k3s made `default` a cluster
  name for the first time, and "default" beside a second registration reads as
  *the default one* rather than as a name — in the switcher, in the audit
  trail's `cluster_name`, and in the startup log. An explicit `name` on import
  always wins, and an unclassified context keeps its own name.

- **`create pods` joins the registration baseline**, making it nineteen checks.
  It is one grant behind three features — §5.5's node debug pod, §15's CLI pod
  and §4's create-from-YAML — because RBAC cannot tell an nginx pod from one
  mounting the node's root filesystem. All three are reached from pages that
  look like they work until the confirm, and the switches that enable them say
  nothing about whether the ServiceAccount may create a pod, so a missing grant
  read as a bug in the console. `docs/rbac.md` had published it in the baseline
  list for a long time without the endpoint checking it; the list and the code
  now agree, and a test holds them there.
- **Zero-setup onboarding for a local cluster (§34).** With nothing registered,
  the console reads the kubeconfig on its own machine and registers the one
  local cluster in it — `kind`, `k3d`, minikube, Docker Desktop, Rancher
  Desktop, Colima, OrbStack, MicroK8s — so a first start shows a cluster
  instead of an empty form. `ADMIN_AUTO_DISCOVER_LOCAL=false` turns it off.
- `GET /api/clusters/discovery` and `POST /api/clusters/import`, with a
  **Discovered on this machine** panel on the Clusters page. Every context is
  listed, including the ones the console refuses to copy a credential from —
  an `exec` credential plugin is a fact about a context, never something this
  console runs — each with the sentence saying what to do instead.
- X.509 client-certificate registration (`authentication_type:
  "client_certificate"`). The local cluster tools mint no ServiceAccount token,
  so these were the clusters the console could not represent at all. The
  certificate is stored in the clear, the key encrypted like a token.
- `ClusterPublic` gains `has_client_certificate` and `origin`
  (`manual` | `kubeconfig` | `autodiscovered`).
- [`docs/adr-0012-kubeconfig-onboarding.md`](docs/adr-0012-kubeconfig-onboarding.md):
  why reading a kubeconfig does not make it a credential source, and what an
  adopted `cluster-admin` certificate costs next to a scoped token.

### Fixed

- **`make install`, and the prerequisites it needs.** No document in the
  repository said to install a dependency before running anything, so a new
  contributor following README's Development section met `No module named
  pytest` on their first command — a missing step that reads as a broken
  repository. README and CONTRIBUTING now state Python 3.12 and Node 20 (the
  versions CI runs, and the only ones exercised), `.nvmrc` pins the second, and
  the new target installs both halves.
- `docker-compose.yml` pointed at `make dev`, which has not existed since the
  target was split into `dev-backend` and `dev-frontend`. It sat in the comment
  explaining the `kind`/`127.0.0.1` adoption caveat — read precisely when that
  caveat has just bitten someone.
- README's documentation table listed ADR-0007 as **Proposed** when it is
  accepted and shipped (`app/k8s/impersonation.py`, the
  `impersonation_unavailable` error code), and omitted ADR-0004, 0005, 0008,
  0009, 0010 and 0011 — including both bundle-boundary ADRs the project asks
  people to read before adding a third bundle.
- The registration form's **Bearer token** option (`bearer_token`) was not in
  the backend's supported set, so choosing it answered `422 Unsupported
  authentication type`. The form was self-consistent, the backend was right
  about its own list, and the two had never been compared.
- **Cluster API calls carried no timeout at all.** The deadline
  `K8S_CONNECT_TIMEOUT_SECONDS` / `K8S_READ_TIMEOUT_SECONDS` configure was
  applied with `if "_request_timeout" not in kwargs`, and the kubernetes
  client's `GET`/`POST`/`PUT`/`PATCH`/`DELETE` all pass that keyword
  explicitly as `None` — so the key was always present and the default never
  fired. A connection test against an unroutable address took **540 seconds**
  instead of the configured 5, holding a threadpool worker for all of it while
  the liveness probe, which touches no cluster, kept answering healthy. That is
  the exact failure the code was written to prevent. Now 20s, and asserted on
  the value that reaches the transport in
  `backend/tests/test_transport_deadlines.py`.
- A kubeconfig the backend process cannot read — the mode-600 file against a
  container running as uid 10001, which the README has warned about for as long
  as the Compose mount has existed — is now reported, naming the uid, instead
  of producing a console that starts fine and reports no clusters.

## [0.1.0] - 2026-09-15

Initial public release.

### Added

- The console itself: catalog-driven browsing of every resource a cluster
  serves, and the single write funnel — gate, preflight, dry-run, diff,
  audit — that every mutation goes through. See
  [`docs/safety-model.md`](docs/safety-model.md).
- An append-only, hash-chained audit trail covering every write, including
  the ones that failed, were denied, or conflicted.
- Optional built-in authentication behind one identity registry: local
  accounts, LDAP, OpenID Connect, OAuth 2.0, the OpenShift OAuth server, and
  SAML 2.0 — plus a legacy authenticating-proxy mode for when it's disabled.
- Two off-by-default installable bundles, both applied through the ordinary
  write funnel: a pinned HAProxy ingress controller (§14,
  `ADMIN_ROUTER_MANAGE_ENABLED`) and a vendored, SHA-256-pinned Operator
  Lifecycle Manager release (§33, `ADMIN_OLM_INSTALL_ENABLED`). See
  [`docs/adr-0004-shipped-router.md`](docs/adr-0004-shipped-router.md) and
  [`docs/adr-0008-shipped-olm.md`](docs/adr-0008-shipped-olm.md) for why
  there are exactly two.
- Per-cluster impersonation ([ADR-0007](docs/adr-0007-impersonation.md)),
  node drain planning, debug containers, route compilation, project
  (namespace) creation, Pod Security level changes with impact preview, PVC
  growth, autoscaler bounds, VolumeSnapshots, node taint/label management,
  CSR approval, namespace deletion with a dependency listing, and RBAC grant
  management — each behind its own preflight and audit record.
- `SECURITY.md`, `CONTRIBUTING.md`, and `CODE_OF_CONDUCT.md` for the public
  release.

[0.1.0]: https://github.com/aesaganda/k8boss-admin/releases/tag/v0.1.0
