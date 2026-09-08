# Operator Lifecycle Manager, vendored (§33)

These two files are upstream's release artifacts, **byte for byte**, for OLM
**v0.35.0**:

| File | Upstream | SHA-256 |
|---|---|---|
| `crds.yaml` | [`v0.35.0/crds.yaml`](https://github.com/operator-framework/operator-lifecycle-manager/releases/download/v0.35.0/crds.yaml) | `0b66ca9d94298f04ec0704887adf663003447f50ca906187aa0ed14d701a9bd7` |
| `olm.yaml` | [`v0.35.0/olm.yaml`](https://github.com/operator-framework/operator-lifecycle-manager/releases/download/v0.35.0/olm.yaml) | `5756646581f5a13fab43a20e7c548492c2494ed5899d8ad2d18c0c3032d2a590` |

Verify them yourself — the point of the digests is that you do not have to take
this repository's word for it:

```bash
sha256sum deploy/olm/*.yaml
curl -sSL https://github.com/operator-framework/operator-lifecycle-manager/releases/download/v0.35.0/crds.yaml | sha256sum
```

**Do not edit these files.** The same digests are pinned in
`backend/app/admin/olm_bundle.py`, `_read()` refuses to load a file that does not
match — at run time, not only under test — and
`tests/test_olm.py::test_the_vendored_manifests_match_their_pinned_digests` fails
the build. A vendored manifest that has been edited is a manifest nobody reviewed
against upstream, and what is in these files is a cluster's entire operator
control plane plus a ClusterRole granting every verb on every resource.

To move to a newer OLM: replace **both** files with that release's artifacts and
update `FILE_DIGESTS` and `OLM_VERSION` in the same commit. The digests are what
make it impossible to do half of that.

## What the console installs, and what it leaves out

`app.admin.olm_bundle.build()` applies three deliberate differences to these
bytes. Everything else is upstream's:

1. **Every object gets `app.kubernetes.io/managed-by: k8boss-admin`**, added
   alongside upstream's labels and never replacing them. It is how an install
   tells "this cluster has no OLM" from "this cluster already runs one" and
   refuses the second case — which matters, because installing over a running OLM
   restarts every operator on the cluster.
2. **`operatorhubio-catalog` is not installed unless asked for.** Upstream's
   `olm.yaml` ends with a `CatalogSource` pulling
   `quay.io/operatorhubio/catalog:latest`, re-polled hourly. Installing it by
   default would make the console the thing that decided your cluster trusts
   operatorhub.io, at an unpinned tag. It is opt-in behind an acknowledged
   consequence. An OLM with no CatalogSource is a working OLM with an empty
   catalog.
3. **The apply is two ordered phases with a wait between them** — `crds.yaml`,
   then `Established`, then `olm.yaml` — because eleven objects in the second
   file are instances of the CRDs in the first.

## Installing by hand

`kubectl apply -f` these files and you get the same thing the console installs,
minus the labels and plus the community catalog:

```bash
kubectl apply -f deploy/olm/crds.yaml --server-side
kubectl wait --for=condition=Established --timeout=120s \
  -f deploy/olm/crds.yaml
kubectl apply -f deploy/olm/olm.yaml
kubectl rollout status -n olm deploy/olm-operator deploy/catalog-operator
kubectl get csv -n olm packageserver -o jsonpath='{.status.phase}'
```

`--server-side` on the CRDs is not optional: the `ClusterServiceVersion` CRD is
about 1 MiB, and a client-side apply cannot fit it in the
`last-applied-configuration` annotation.

That last line is the one that matters. Until the `packageserver` CSV reaches
`Succeeded`, `packages.operators.coreos.com` does not exist and the console's
operator portal reads exactly the empty state it read before you started — which
is correct, not a failure.

## Read before applying

`olm.yaml` contains `system:controller:operator-lifecycle-manager`:

```yaml
rules:
  - apiGroups: ['*']
    resources: ['*']
    verbs: [watch, list, get, create, update, patch, delete, deletecollection, escalate, bind]
  - nonResourceURLs: ['*']
    verbs: ['*']
```

That is cluster-admin plus the ability to grant cluster-admin, bound to
`olm-operator-serviceaccount` in the `olm` namespace. It is inherent to OLM — it
installs operators that ask for arbitrary permissions, so it must hold them — and
it is the single most consequential thing in this directory.

`docs/adr-0008-shipped-olm.md` records why this console ships these files at all,
and what it cost.
