"""
Read-side services: the shaped, cross-resource views the typed API endpoints
serve (§6 onwards).

The split from ``app.resources`` is deliberate. ``app.resources`` answers "give
me this API object", generically, for anything the cluster serves.
``app.services`` answers a *question an operator has* — "what is the state of
this workload" — which means correlating several API objects (the controller,
its pods, the Services in front of it) and deriving a judgement from them.

Nothing here writes. Every mutation goes through ``app.admin.mutate``, which is
the single write funnel; a service module that wrote to a cluster would bypass
the preflight, the dry-run diff and the audit record in one step.
"""
