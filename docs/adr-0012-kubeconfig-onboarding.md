# ADR 0012 — the console reads the kubeconfig on its own machine, once, to onboard

**Status:** accepted
**Date:** 2026-09-19
**Qualifies:** `app/k8s/client.py`'s opening sentence — *clients are built from a
registered cluster's stored API server endpoint and encrypted bearer token —
never from a kubeconfig on the server.* That sentence is still true of every
client this console builds. This ADR is about the one place a kubeconfig is now
read, which is not a client.

## The decision

The console reads the kubeconfig on the machine it runs on, at two moments and no
others:

1. when an administrator asks it to (`GET /api/clusters/discovery`), to list what
   is there;
2. when an administrator confirms one context (`POST /api/clusters/import`), or
   when startup adoption confirms one on their behalf under §34.5's five
   conditions, to **copy** that context's credential into §3's encrypted
   registry.

After (2) the kubeconfig has no further part in that cluster's life. Nothing
re-reads it, no request falls back to it, and rotating, moving or deleting it
changes nothing about a cluster registered from it. Re-importing is how a new
credential is picked up.

To make that copy possible for the clusters it is actually for, the console also
gained a second credential kind: X.509 client certificates
(`authentication_type: client_certificate`), because `kind`, `k3d`, minikube and
Docker Desktop mint no ServiceAccount token at all.

## Why

**The first screen was asking for four commands' worth of work to show anything.**
Registration is an API server address and a bearer token. For a remote cluster
that is exactly right — it is a credential somebody deliberately provisioned for
this console, scoped by `deploy/rbac.yaml`, rotatable independently of any human.
For a `kind` cluster on the same laptop it is: create a ServiceAccount, apply a
ClusterRoleBinding, mint a token, dig the API address and the CA out of the
kubeconfig, paste. Five steps, against a cluster whose credentials are three
lines from the process being started, to reach a console that then shows a
cluster the operator had already made.

`scripts/onboard-cluster.sh` automates those five steps and is the right answer
for a real cluster. It is a poor answer for a local one: it applies RBAC to a
throwaway cluster to mint a token for a credential path that cluster already has.

**The existing local path was invisible, and invisible is the failure mode this
project cares most about.** There has always been a kubeconfig fallback in
`client.py`, used only when nothing is registered. It works, and it produces a
console whose `GET /api/clusters` returns `[]`, whose switcher is empty, whose
health reports `registered: 0`, and which serves pages anyway. An operator cannot
test it, cannot see its permissions, cannot name it and cannot tell it apart from
a console that is pointed at nothing. An adopted cluster is a row: it has a name,
a status, a connection test, a permission matrix and a line in the switcher.

**And it failed silently in the deployment most people start with.** Under
Compose the kubeconfig is mounted read-only into a container running as uid
10001, and a kubeconfig is mode 600 owned by the operator. The README has
documented that for as long as the mount has existed; nothing *detected* it. The
console started, reported no clusters, and looked exactly like a machine with no
kubeconfig on it — "empty is never blind", violated by a path that predates the
rule.

## What was rejected

**Loading the kubeconfig with `kubernetes.config.load_kube_config`.** Fewer lines,
and it *executes* the `exec` credential plugin of whichever context it loads.
Running an arbitrary binary named by a file on disk as a side effect of
**listing** what is available is not something a console does. The file is parsed
with `app.yaml_dialect` instead — the backend's one YAML (ADR-0009) — and an
`exec` stanza is a fact about a context, never an instruction.

**Reading the kubeconfig at request time, as a credential source.** This is the
one that would actually have been less code: no import, no new columns, no
`origin`. It was rejected because it makes a cluster's credential something this
console cannot reason about. A registration would mean different things on
different days; "which credential is this console using" would have no answer
that survives somebody running `kubectl config use-context`; and the audit trail
would name a cluster whose identity had changed under it. The copy is the whole
point: after an import there is exactly one place the credential lives, and it is
the same place every other cluster's lives.

**Rewriting `https://127.0.0.1:6443` to `host.docker.internal` when running in a
container.** It is the obvious fix and it does not work: `kind` presents a
certificate for `127.0.0.1` and `localhost`, not for that name, so the rewritten
URL fails verification instead of connecting — and the only way to make it
connect is to set `skip_tls_verify` on the operator's behalf, which is a security
decision taken quietly to make a convenience appear to work. A partial-verification
mode (chain yes, hostname no) was considered and is a larger decision than this
ADR; it is not ruled out, it is simply not made here. What ships instead is a
`concern` on the candidate saying what is wrong and what to do about it, and a
startup adoption that declines rather than registering something that cannot
connect.

**Adopting more than one cluster, or a remote one, at startup.** A console that
boots and quietly registers the production context a developer happens to have in
their kubeconfig is a far worse failure than one that asks. `is_local` is
therefore two tests joined with `and` — a recognised local distribution *and* a
loopback or private address — so a context called `kind-prod` pointing at a
public endpoint is remote, whatever it is named. And two qualifying clusters
adopts neither: picking between them is a choice, and boot is where nobody sees a
choice being made.

**Recognising a distribution by a generic name.** k3s — one of the commonest ways
to get a local cluster, and the one this feature shipped without — names *every*
entry in `/etc/rancher/k3s/k3s.yaml` `default`, so the name table recognised none
of it and a `127.0.0.1` cluster on the console's own machine was classified
remote. Adding `default` to that table was rejected: it is a name a remote
cluster's context routinely carries, so it would auto-adopt precisely what the
paragraph above refuses, and it would do it under the heading of a fix.

What was accepted instead is the **path**. `/etc/rancher/k3s/k3s.yaml` and
`/etc/rancher/rke2/rke2.yaml` are fixed, and a kubeconfig read from one of them
was written by that tool by construction — no cluster elsewhere can arrange to be
read from there. So `classify()` takes the path it was read from, symlinks
resolved, and recognises the file rather than any name inside it. `is_local` is
unchanged: the address test still has to pass, so a `default` context pointing at
a public endpoint is remote from that path as from any other.

The cost is stated rather than patched over: **the evidence does not survive the
file being moved.** A k3s kubeconfig copied to `~/.kube/config`, or merged into
one with `kubectl config view --flatten`, has no path left to read and a context
called `default`, and it is classified remote and imported by hand. There is
nothing safe left to go on at that point, and the weaker heuristic that would
cover it is the one rejected two paragraphs up. The path is read the other way
round, though — it identifies the file, so a k3s context somebody renamed is
still recognised.

**Making the discovery endpoint public, or unauthenticated.** It reports the path
of a file on the console's machine, the contexts in it and the addresses they
point at. No credential material, and still somebody's infrastructure. It carries
`require_console_admin` with §3's three writes.

## What this costs

**A second credential kind in the schema, one of which is a private key.**
`client_key_encrypted` is encrypted at rest and listed in
`_CLUSTER_SECRET_COLUMNS`, and `Cluster.to_public_dict` raises rather than
serialise it. But the kubernetes client reads a certificate and key from *paths*,
so building that transport spills a decrypted key to a temp file — mode 0600,
tracked on the bundle, deleted when it closes. That is a file that did not exist
before. The cleanup is tested; the exposure is real and is the price of the
credential kind.

**A row in the registry that nobody typed.** Startup adoption writes a `Cluster`
without a request. Five conditions narrow it and `origin` records it, and neither
of those makes it not true. `ADMIN_AUTO_DISCOVER_LOCAL=false` is the answer for a
deployment where every registration must be an explicit act, and it is one
variable rather than a posture.

**A kubeconfig is not a scoped credential.** A ServiceAccount token minted by
`deploy/rbac.yaml` holds exactly the permissions that file grants. A kind
kubeconfig's client certificate is `cluster-admin`, because that is what those
tools write. So an adopted local cluster is registered with far more authority
than a deliberately onboarded one — which is *appropriate* for a throwaway
cluster and would not be for anything else, and is the sharpest reason adoption
is restricted to local clusters. It is stated here rather than implied by the
restriction.

**RKE2 is on the path list, and it is not a laptop.** The paragraph above says
an adopted cluster-admin certificate is *appropriate for a throwaway cluster and
would not be for anything else*. `/etc/rancher/rke2/rke2.yaml` is the one entry
where that sentence is under real strain: RKE2 is Rancher's hardened
enterprise and government distribution, its users are the population least
likely to accept a registration nobody asked for, and adoption takes the same
cluster-admin certificate from it as from a kind cluster. It is included anyway, for three
reasons worth having on the record rather than rediscovering in an argument.

*The distinction it would draw is not real.* k3s is not a development tool that
RKE2 is the production version of — k3s runs in production constantly, at the
edge and on single-node installs, which is most of why it was worth recognising.
Dropping RKE2 while keeping k3s removes a label and leaves the risk class
exactly where it was, which is the kind of change that reads as a control and is
not one.

*The configuration it needs is narrow and deliberate.* Both files are mode
`0600` and owned by root, so the console has to be running **as root on the
server node** to read either. `deploy/deployment.yaml` ships
`ADMIN_AUTO_DISCOVER_LOCAL=false`, so an in-cluster install adopts nothing;
`docker-compose.yml` mounts `~/.kube/config` as uid 10001 and never sees
`/etc/rancher` at all. What is left is somebody running the backend as a bare
root process on the control-plane node with an empty registry — a single-machine
appliance setup, where registering that node's own cluster is plausibly the
point.

*The controls are the five conditions, not the list.* An empty registry, a
loopback or private address, no reachability concern, exactly one candidate, and
`ADMIN_AUTO_DISCOVER_LOCAL` for anyone who wants none of it. Those are what this
decision rests on; the path table only decides what gets *offered* to them.

**If that trade is ever reopened, the coherent change is not deleting one row.**
It is to split adoption from classification: recognise the path-identified
server distributions — k3s and RKE2 together — so they are labelled and offered
under Clusters, and auto-adopt only the name-identified developer tools, which
say what they are. That costs k3s its zero-setup start, which is the thing
recognising it bought. Stated here so the option is on the record with its
price, rather than arrived at by deleting a line and calling it safer.

**One more thing that can be wrong about somebody's laptop.** Distribution
detection is a list of name prefixes and a list of two fixed paths in one file,
container detection is `/.dockerenv` plus a cgroup scan, and both can be wrong. Being wrong about "is this local"
costs an adoption that does not happen and a panel that says `remote`; being
wrong about "am I in a container" costs a concern shown or withheld. Neither
produces a registration that lies, which is the bar.

## What this does not change

* **Where clients come from.** Still a `Cluster` row, still nothing else.
* **§3's registration path.** Unchanged, still the way a real cluster is
  onboarded, still what `scripts/onboard-cluster.sh` drives.
* **The development fallback in `client.py`.** Still there, still only when
  nothing is registered — now mostly losing to adoption, and kept for in-cluster
  mode, where the credential is the pod's own ServiceAccount and there is no
  kubeconfig to import, and for a kubeconfig whose only context is remote.
* **ADR-0007.** Adoption never sets `impersonation_enabled`. A kubeconfig says
  nothing about whether this console and that API server believe the same issuer.
* **The audit trail.** Registration was never an audited cluster write and is not
  one now; it changes this console's own database, and §3 has always logged it.

## The line, stated so a later change has to cross it deliberately

The kubeconfig is an **onboarding source**, read on request and copied. It is not
a credential store, not a fallback, and not something this console watches. A
change that made a request read it — refreshing a credential, following a
rotation, resolving a context at call time — is not an extension of this ADR. It
is the thing this ADR rejected, and it needs its own.
