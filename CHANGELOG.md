# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project has not
reached 1.0 — see [SECURITY.md](SECURITY.md) for what "supported" means
before then; expect breaking changes between minor versions until it does.

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
