"""k8boss-admin backend — a Kubernetes administration console.

Reads and writes clusters. Every write goes through a single funnel that
preflights RBAC, supports dry-run, diffs, and audits — see ``app.admin.mutate``.
Every read that could not look says so in an ``unavailable`` envelope rather
than returning an empty list, because an empty list means "the cluster has
none" and nothing else.
"""

__all__ = []
