/**
 * templates — the starter manifests behind every "Create <Kind>…" button, and
 * the fallback for the kinds nobody wrote one for.
 *
 * A create dialog opened on a listing knows the kind it is creating, and an
 * empty editor is the least useful thing it could do with that. What it seeds
 * instead is a **starter**: a named, minimal, *valid* manifest for that kind,
 * which the operator edits into the object they wanted. Several kinds get more
 * than one, because the shapes an operator picks between are genuinely
 * different objects — a headless Service and a LoadBalancer share a kind and
 * almost nothing else — and a single starter would make one of them the default
 * and the other a rewrite.
 *
 * ## Three rules every starter here obeys
 *
 * **It applies to a cluster running the restricted Pod Security Standard.**
 * Anything carrying a pod template says `runAsNonRoot`, `seccompProfile` and
 * the container-level pair, because the profile most clusters enforce by
 * default would otherwise reject the first thing anybody clicked — and an
 * admission rejection on a starter reads as a broken console rather than as a
 * cluster policy.
 *
 * **It never sets `metadata.namespace`.** `ImportYamlDialog` falls back to the
 * masthead's selection and says which of the two namespaces won. A starter that
 * hardcoded one would go stale the moment the operator switched scope after
 * opening the dialog and before pressing Create.
 *
 * **It carries no comments.** A form edit re-serialises the parsed document and
 * nothing carries a comment across a parse, so a commented starter would warn
 * the operator it was about to lose lines this file wrote — on the first click,
 * before they had typed anything. What a comment would have said belongs in the
 * starter's own `description`, which is on screen next to it, and in the form
 * fields' help text.
 *
 * ## A starter belongs to one apiVersion and to no other
 *
 * The registry keys on the exact `apiVersion` string, and the version in that
 * key is the one discovery reported for the resource the create button was
 * built from. The Gateway tabs resolve their version from the live catalog, so
 * on a cluster serving `gateway.networking.k8s.io/v1beta1` the `v1` starters
 * registered below do not match and the skeleton opens instead.
 *
 * That is the behaviour to keep, not a gap to close with a group-and-kind
 * fallback that re-stamps a body under whichever version the cluster serves. A
 * starter is written against one version's schema, and the Gateway API in
 * particular has moved required fields between channels — a policy's
 * `spec.targetRef` became `spec.targetRefs`, its `spec.tls` became
 * `spec.validation`. Re-headering a document across that produces a manifest
 * naming a version whose required fields it does not carry: a confident starter
 * for an API nobody here checked, which is the failure this file exists to
 * avoid. The skeleton claims less and is true.
 *
 * ## The kinds with no starter
 *
 * There is no starter for a kind this console has never heard of, and inventing
 * one would mean guessing at somebody's CRD schema. `templatesFor()` answers
 * with a **skeleton** instead: `apiVersion`, `kind` and a name, described as
 * exactly that. It is not a working object and it does not claim to be — the
 * dry run is what says whether the API server accepts it, which is the same
 * thing that is true of every other starter here.
 *
 * ## Why this lives in `components/`
 *
 * `ImportYamlDialog` needs it, and the dialog is a component: a
 * components -> pages import runs the wrong way and would make the masthead's
 * "+" depend on a page module. Pages import from here in the ordinary
 * direction, as they already do for `components/ui`.
 */

/**
 * What `ImportYamlDialog` seeds its editor with when opened from a "Create"
 * button instead of the masthead's blank "+". Each one is a minimal manifest
 * that parses, matches its own `apiVersion`/`kind`, and applies cleanly against
 * a cluster running Kubernetes' restricted Pod Security Standard — the profile
 * most clusters enforce by default — so the dry-run diff an operator sees on
 * first click is "this creates a container", not an admission rejection about
 * `runAsNonRoot`.
 *
 * The container is NGINX rather than `pause`, because a starter that actually
 * serves something is the one an operator can point a Service, an Ingress or a
 * NetworkPolicy at and see answer. `pause` comes up Running and replies to
 * nothing, which makes every one of those checks unfalsifiable.
 *
 * It is the **unprivileged** NGINX build, and that is not interchangeable with
 * `nginx` or `httpd`: neither official image sets a `USER`, so both run as
 * root. Either one is still admitted under the restricted profile — the spec
 * says `runAsNonRoot: true`, and the spec is all admission reads — and it is
 * the kubelet that then refuses to start the container,
 * `CreateContainerConfigError`. That failure lands *after* the write, so the
 * console truthfully reports the object created, the diff was right, and the
 * pod never runs anyway. Nothing here is in a position to warn about it, which
 * is why the starter does not hand anyone that image. The unprivileged
 * build runs as UID 101 and listens on 8080 rather than 80, which is why the
 * port is spelled out here: 8080 is the surprise in this image, and a template
 * that hid it would send someone to write a Service targeting port 80.
 *
 * The tag is the 1.30 stable line rather than `latest`, so what an operator
 * reads in the editor is what the API server is asked to create.
 *
 * None of these set `metadata.namespace`: `ImportYamlDialog` already falls
 * back to the masthead's selected namespace, and hardcoding one into the
 * template would go stale the moment the operator switched namespaces after
 * opening the dialog but before clicking Create.
 */
export const POD_TEMPLATE = `apiVersion: v1
kind: Pod
metadata:
  name: example
  labels:
    app: example
spec:
  securityContext:
    runAsNonRoot: true
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: example
      image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
      ports:
        - containerPort: 8080
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop:
            - ALL
`;

// Written out per kind rather than assembled from a shared fragment: the
// nesting depth of `spec.template` differs (a bare Deployment vs. a CronJob's
// `spec.jobTemplate.spec.template`), and YAML's meaning is its indentation —
// string-splicing four spaces into the right place for six kinds is exactly
// the kind of code nobody can eyeball-verify. Six literals are more lines and
// zero risk of a template that silently reindents itself wrong.
//
// The Job and CronJob starters carry the same server, and a server does not
// exit: run one unedited and the Job stays incomplete until it is deleted.
// That was equally true of `pause` and is not what a Job is for — the image
// and a `command` are the two lines to replace, which is the whole reason the
// dialog seeds an editor rather than a form.
export const WORKLOAD_TEMPLATES = {
  deployments: `apiVersion: apps/v1
kind: Deployment
metadata:
  name: example
  labels:
    app: example
spec:
  replicas: 1
  selector:
    matchLabels:
      app: example
  template:
    metadata:
      labels:
        app: example
    spec:
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: example
          image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
          ports:
            - containerPort: 8080
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
`,
  statefulsets: `apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: example
  labels:
    app: example
spec:
  serviceName: example
  replicas: 1
  selector:
    matchLabels:
      app: example
  template:
    metadata:
      labels:
        app: example
    spec:
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: example
          image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
          ports:
            - containerPort: 8080
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
`,
  daemonsets: `apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: example
  labels:
    app: example
spec:
  selector:
    matchLabels:
      app: example
  template:
    metadata:
      labels:
        app: example
    spec:
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: example
          image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
          ports:
            - containerPort: 8080
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
`,
  replicasets: `apiVersion: apps/v1
kind: ReplicaSet
metadata:
  name: example
  labels:
    app: example
spec:
  replicas: 1
  selector:
    matchLabels:
      app: example
  template:
    metadata:
      labels:
        app: example
    spec:
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: example
          image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
          ports:
            - containerPort: 8080
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
`,
  jobs: `apiVersion: batch/v1
kind: Job
metadata:
  name: example
  labels:
    app: example
spec:
  template:
    metadata:
      labels:
        app: example
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: example
          image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
          ports:
            - containerPort: 8080
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
`,
  cronjobs: `apiVersion: batch/v1
kind: CronJob
metadata:
  name: example
  labels:
    app: example
spec:
  schedule: '*/5 * * * *'
  jobTemplate:
    spec:
      template:
        metadata:
          labels:
            app: example
        spec:
          restartPolicy: Never
          securityContext:
            runAsNonRoot: true
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: example
              image: docker.io/nginxinc/nginx-unprivileged:1.30-alpine
              ports:
                - containerPort: 8080
              securityContext:
                allowPrivilegeEscalation: false
                capabilities:
                  drop:
                    - ALL
`,
};

/* ── The registry ───────────────────────────────────────────────────────── */

/**
 * One entry per kind, keyed by the `apiVersion` and `kind` a document has to
 * carry to be that kind. The catalog item the create button was built from
 * supplies both, so the lookup is the same equality check
 * `ImportYamlDialog` already makes to decide where to POST.
 */
const STARTERS = new Map();

/** Register the starters for one kind. */
function starters(apiVersion, kind, list) {
  STARTERS.set(`${apiVersion}\u0000${kind}`, list.map((entry) => ({ ...entry, apiVersion, kind })));
}

/* ── The starters ───────────────────────────────────────────────────────── */

starters('v1', 'Pod', [
  {
    id: 'server',
    label: 'A server',
    description:
      'One unprivileged NGINX container listening on 8080, with the security context the restricted Pod ' +
      'Security Standard asks for. Almost every field on a Pod is immutable once it exists.',
    text: POD_TEMPLATE,
  },
]);

starters('apps/v1', 'Deployment', [
  {
    id: 'server',
    label: 'A server',
    description:
      'One replica of an unprivileged NGINX container, with a selector that matches its own pod template. ' +
      'The selector is immutable after creation, so it is the field to get right here.',
    text: WORKLOAD_TEMPLATES.deployments,
  },
]);

starters('apps/v1', 'StatefulSet', [
  {
    id: 'server',
    label: 'A server',
    description:
      'One replica with a governing headless Service name. The Service itself is a separate object this ' +
      'does not create — a StatefulSet whose serviceName points at nothing still starts, with pods whose ' +
      'DNS names never resolve.',
    text: WORKLOAD_TEMPLATES.statefulsets,
  },
]);

starters('apps/v1', 'DaemonSet', [
  {
    id: 'server',
    label: 'A server on every node',
    description:
      'No replica count: a DaemonSet runs one pod per eligible node, and which nodes are eligible is the ' +
      'node selector and the nodes’ own taints.',
    text: WORKLOAD_TEMPLATES.daemonsets,
  },
]);

starters('apps/v1', 'ReplicaSet', [
  {
    id: 'server',
    label: 'A server',
    description:
      'A bare ReplicaSet, which is what a Deployment creates for you. Creating one directly means nothing ' +
      'will roll it out: there is no revision history and no rollback.',
    text: WORKLOAD_TEMPLATES.replicasets,
  },
]);

starters('batch/v1', 'Job', [
  {
    id: 'server',
    label: 'One run',
    description:
      'A single run to completion. The image below is a server and a server does not exit, so this Job ' +
      'stays incomplete until it is deleted — the image and a command are the two lines to replace.',
    text: WORKLOAD_TEMPLATES.jobs,
  },
]);

starters('batch/v1', 'CronJob', [
  {
    id: 'server',
    label: 'On a schedule',
    description:
      'A Job created on a schedule, in the cluster’s own timezone unless spec.timeZone says otherwise. ' +
      'The same caveat as a Job: the starter image is a server and will not exit on its own.',
    text: WORKLOAD_TEMPLATES.cronjobs,
  },
]);

starters('networking.k8s.io/v1', 'NetworkPolicy', [
  {
    id: 'default-deny-ingress',
    label: 'Deny all inbound',
    description:
      'The policy every segmentation story starts with. The empty podSelector selects EVERY pod in the ' +
      'namespace, and the absence of any ingress rule is what denies inbound traffic to all of them. ' +
      'policyTypes is spelled out rather than left to the default because the default is asymmetric: ' +
      'every policy is assumed to affect ingress whether it says so or not, and only a policy that names ' +
      'Egress affects egress — so this line changes nothing here and is the line to add when the same ' +
      'policy should also cut off outbound traffic. Whether any of it is enforced belongs to the ' +
      'cluster’s CNI plugin, which no API this console reads reports on.',
    text: `apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
spec:
  podSelector: {}
  policyTypes:
    - Ingress
`,
  },
  {
    id: 'allow-from-namespace',
    label: 'Allow from one label',
    description:
      'Inbound traffic to the pods this selects, permitted only from pods in the same namespace carrying ' +
      'the peer label. Every other source is denied, because a pod selected by any Ingress policy is ' +
      'isolated for inbound traffic and this is the only rule permitting any.',
    text: `apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-app
spec:
  podSelector:
    matchLabels:
      app: example
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: caller
      ports:
        - protocol: TCP
          port: 8080
`,
  },
]);

starters('v1', 'Service', [
  {
    id: 'clusterip',
    label: 'In-cluster address',
    description:
      'A virtual IP and a DNS name in front of whatever the selector matches, reachable only from inside ' +
      'the cluster. targetPort is 8080 because that is where the unprivileged NGINX in these starters ' +
      'listens — a Service pointing at 80 is created without complaint and answers nothing.',
    text: `apiVersion: v1
kind: Service
metadata:
  name: example
  labels:
    app: example
spec:
  type: ClusterIP
  selector:
    app: example
  ports:
    - name: http
      port: 80
      targetPort: 8080
      protocol: TCP
`,
  },
  {
    id: 'headless',
    label: 'No address, DNS only',
    description:
      'clusterIP: None returns one address per ready pod instead of a virtual IP, and is what a ' +
      'StatefulSet’s serviceName has to point at. A StatefulSet governed by an ordinary ClusterIP Service ' +
      'starts anyway, with per-pod DNS names that never resolve.',
    text: `apiVersion: v1
kind: Service
metadata:
  name: example
  labels:
    app: example
spec:
  type: ClusterIP
  clusterIP: None
  selector:
    app: example
  ports:
    - name: http
      port: 8080
      targetPort: 8080
      protocol: TCP
`,
  },
  {
    id: 'nodeport',
    label: 'A port on every node',
    description:
      'Opens one port in the 30000-32767 range on every node in the cluster. No nodePort is written below ' +
      'on purpose: left unset the API server allocates a free one, and pinning a number risks a collision ' +
      'that arrives as a rejected create rather than as a working Service.',
    text: `apiVersion: v1
kind: Service
metadata:
  name: example
  labels:
    app: example
spec:
  type: NodePort
  selector:
    app: example
  ports:
    - name: http
      port: 80
      targetPort: 8080
      protocol: TCP
`,
  },
  {
    id: 'loadbalancer',
    label: 'An external load balancer',
    description:
      'Asks the cloud provider, or an in-cluster controller, for an address. On a cluster with neither ' +
      'this object is created, reports no error, and its external address stays pending forever — which ' +
      'looks identical to one that is still being provisioned.',
    text: `apiVersion: v1
kind: Service
metadata:
  name: example
  labels:
    app: example
spec:
  type: LoadBalancer
  selector:
    app: example
  ports:
    - name: http
      port: 80
      targetPort: 8080
      protocol: TCP
`,
  },
]);

starters('v1', 'ConfigMap', [
  {
    id: 'keys',
    label: 'Key/value settings',
    description:
      'Every value here is a string. An unquoted count: 3 is a YAML integer and the API server refuses ' +
      'the whole object with an unmarshalling error that names no key — which reads as a broken document ' +
      'rather than as a missing pair of quotes.',
    text: `apiVersion: v1
kind: ConfigMap
metadata:
  name: example
data:
  LOG_LEVEL: info
  TIMEOUT_SECONDS: "30"
`,
  },
  {
    id: 'file',
    label: 'A file to mount',
    description:
      'The key becomes the filename when this is mounted as a volume. Mounted that way it updates in ' +
      'place on the kubelet’s sync period without restarting anything — unless it was mounted with ' +
      'subPath, which never updates and gives no sign that it does not.',
    text: `apiVersion: v1
kind: ConfigMap
metadata:
  name: example
data:
  app.conf: |
    listen 8080
    log_level info
`,
  },
]);

starters('v1', 'Secret', [
  {
    id: 'opaque',
    label: 'Arbitrary values',
    description:
      'stringData is write-only: the API server base64-encodes it into data and returns an object with no ' +
      'stringData at all, so the dry-run diff shows the field move. That is the projection being honest, ' +
      'not a mistake — and it is still preferable to encoding by hand, which nothing here would check.',
    text: `apiVersion: v1
kind: Secret
metadata:
  name: example
type: Opaque
stringData:
  PASSWORD: change-me
`,
  },
  {
    id: 'dockerconfigjson',
    label: 'Registry credentials',
    description:
      'The .dockerconfigjson key is required for this type, and its absence — or a value that is not ' +
      'well-formed JSON — is refused at admission. What is inside it is checked no further: a valid ' +
      'document naming the wrong registry, user or password is accepted here and surfaces much later as ' +
      'ImagePullBackOff on every pod that uses it.',
    text: `apiVersion: v1
kind: Secret
metadata:
  name: example
type: kubernetes.io/dockerconfigjson
stringData:
  .dockerconfigjson: '{"auths":{"registry.example.com":{"username":"user","password":"change-me","auth":"dXNlcjpjaGFuZ2UtbWU="}}}'
`,
  },
  {
    id: 'tls',
    label: 'A certificate and key',
    description:
      'Both keys are required for this type, and both are empty below because this console has no ' +
      'certificate to put in them — the document is deliberately incomplete. The API server checks only ' +
      'that the keys exist; it does not check that they parse or that they match each other, so a Secret ' +
      'created successfully here can still be why an ingress serves nothing.',
    text: `apiVersion: v1
kind: Secret
metadata:
  name: example
type: kubernetes.io/tls
stringData:
  tls.crt: ""
  tls.key: ""
`,
  },
]);

starters('v1', 'ServiceAccount', [
  {
    id: 'plain',
    label: 'An identity for pods',
    description:
      'This grants nothing until a RoleBinding names it, and no token Secret comes with it: pods get a ' +
      'short-lived projected token instead. An account that exists and is bound to nothing is the ' +
      'expected state, not a half-finished one.',
    text: `apiVersion: v1
kind: ServiceAccount
metadata:
  name: example
`,
  },
  {
    id: 'no-token',
    label: 'No API token mounted',
    description:
      'Pods running as this account get no API token at all, which is the right default for a workload ' +
      'that does not talk to Kubernetes. It applies to everything in the pod, including a sidecar that ' +
      'expected one and will fail in a way that never names this field.',
    text: `apiVersion: v1
kind: ServiceAccount
metadata:
  name: example
automountServiceAccountToken: false
`,
  },
  {
    id: 'pull',
    label: 'With registry credentials',
    description:
      'imagePullSecrets applies to every pod that runs as this account, which configures a private ' +
      'registry once rather than per workload. The Secret has to already exist in the same namespace — a ' +
      'name matching nothing is accepted here and appears later as ImagePullBackOff.',
    text: `apiVersion: v1
kind: ServiceAccount
metadata:
  name: example
imagePullSecrets:
  - name: registry-credentials
`,
  },
]);

starters('v1', 'PersistentVolumeClaim', [
  {
    id: 'default-class',
    label: 'A block of storage',
    description:
      'The cluster’s default StorageClass provisions it; naming no class is not the same as naming an ' +
      'empty one. With WaitForFirstConsumer binding this claim sits Pending until a pod that uses it is ' +
      'scheduled, and that Pending is the design rather than a fault to chase.',
    text: `apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: example
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
`,
  },
  {
    id: 'from-snapshot',
    label: 'Restored from a snapshot',
    description:
      'The request has to be at least the snapshot’s restore size, and a smaller one fails at ' +
      'provisioning rather than here. What comes back is a copy: writing to it does not touch the ' +
      'snapshot, and deleting the snapshot afterwards does not touch it.',
    text: `apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: example
spec:
  dataSource:
    apiGroup: snapshot.storage.k8s.io
    kind: VolumeSnapshot
    name: example
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
`,
  },
  {
    id: 'block',
    label: 'A raw block device',
    description:
      'No filesystem. The container is handed the device at volumeDevices[].devicePath and nothing mounts ' +
      'it, so a workload expecting a directory finds an empty one. volumeMode is immutable, which makes ' +
      'this a decision taken here or not at all.',
    text: `apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: example
spec:
  volumeMode: Block
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
`,
  },
]);

starters('v1', 'ResourceQuota', [
  {
    id: 'compute',
    label: 'CPU and memory ceilings',
    description:
      'The moment a quota names requests.cpu, every pod created in this namespace afterwards must state ' +
      'that request or admission refuses it — including pods from controllers that were working a minute ' +
      'ago. The 403 does not name this rule, which is why the Quota advisor reads it out.',
    text: `apiVersion: v1
kind: ResourceQuota
metadata:
  name: example
spec:
  hard:
    requests.cpu: "4"
    requests.memory: 8Gi
    limits.cpu: "8"
    limits.memory: 16Gi
`,
  },
  {
    id: 'counts',
    label: 'How many objects',
    description:
      'Counts rather than capacity, and count/<resource>.<group> works for anything the cluster serves, ' +
      'CRDs included. A count already exceeded is not rolled back — what exists stays, and the next ' +
      'create is what fails.',
    text: `apiVersion: v1
kind: ResourceQuota
metadata:
  name: example
spec:
  hard:
    pods: "20"
    services: "10"
    persistentvolumeclaims: "10"
    count/deployments.apps: "10"
`,
  },
]);

starters('v1', 'LimitRange', [
  {
    id: 'defaults',
    label: 'Defaults for containers',
    description:
      'Applied at admission to containers that state no requests or limits of their own, which is how a ' +
      'namespace under a compute quota stops refusing everything. It changes nothing about pods that ' +
      'already exist, so a namespace this covers can still be full of workloads that predate it.',
    text: `apiVersion: v1
kind: LimitRange
metadata:
  name: example
spec:
  limits:
    - type: Container
      default:
        cpu: 500m
        memory: 512Mi
      defaultRequest:
        cpu: 100m
        memory: 128Mi
`,
  },
  {
    id: 'bounds',
    label: 'Minimum and maximum',
    description:
      'A container asking for more than the maximum, or less than the minimum, is refused at admission ' +
      'with a message naming this object rather than the workload. Bounds and defaults are different ' +
      'acts: this one rejects, it does not fill anything in.',
    text: `apiVersion: v1
kind: LimitRange
metadata:
  name: example
spec:
  limits:
    - type: Container
      min:
        cpu: 50m
        memory: 64Mi
      max:
        cpu: "2"
        memory: 2Gi
`,
  },
]);

starters('v1', 'Namespace', [
  {
    id: 'plain',
    label: 'An empty namespace',
    description:
      'Nothing comes with it: no quota, no limit range, no default-deny policy, no bound roles. The ' +
      'Projects action creates those alongside a namespace in one audited write; this creates the ' +
      'namespace alone.',
    text: `apiVersion: v1
kind: Namespace
metadata:
  name: example
`,
  },
  {
    id: 'restricted',
    label: 'With Pod Security enforced',
    description:
      'enforce refuses non-conforming pods outright; warn only tells whoever created them, which means a ' +
      'namespace carrying warn alone rejects nothing. Pinning to latest lets an API server upgrade ' +
      'tighten the rules under a workload that was admitted yesterday.',
    text: `apiVersion: v1
kind: Namespace
metadata:
  name: example
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/warn-version: latest
`,
  },
]);

starters('networking.k8s.io/v1', 'Ingress', [
  {
    id: 'host',
    label: 'One host, one backend',
    description:
      'pathType is required in v1 and has no default. ingressClassName is left as a placeholder on ' +
      'purpose: guessing a controller’s class name produces an Ingress that is accepted, reports no ' +
      'error and routes nothing, which is the exact failure this console exists to refuse — the Ingress ' +
      'Classes tab lists what this cluster actually serves.',
    text: `apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: example
spec:
  ingressClassName: example
  rules:
    - host: example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: example
                port:
                  number: 80
`,
  },
  {
    id: 'tls',
    label: 'With a certificate',
    description:
      'The Secret must be of type kubernetes.io/tls and must already exist in this namespace; a missing ' +
      'one is not an error on this object. Nothing at write time checks that the certificate covers the ' +
      'host below — the Routes page reads it afterwards and says when it expires and whether its subject ' +
      'alternative names match.',
    text: `apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: example
spec:
  ingressClassName: example
  tls:
    - hosts:
        - example.com
      secretName: example-tls
  rules:
    - host: example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: example
                port:
                  number: 80
`,
  },
]);

starters('networking.k8s.io/v1', 'IngressClass', [
  {
    id: 'class',
    label: 'A controller by name',
    description:
      'spec.controller is immutable and has to be the exact string the controller watches for. A typo ' +
      'produces a class nothing claims, and every Ingress naming it stays unrouted with no error on ' +
      'either object.',
    text: `apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: example
spec:
  controller: example.com/ingress-controller
`,
  },
  {
    id: 'default',
    label: 'The default class',
    description:
      'Only one IngressClass in a cluster may carry this annotation — with two, an Ingress naming no ' +
      'class is refused rather than defaulted. The value has to be the string "true"; an unquoted YAML ' +
      'boolean is not a legal annotation value and the create fails on the type, not on the meaning.',
    text: `apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: example
  annotations:
    ingressclass.kubernetes.io/is-default-class: "true"
spec:
  controller: example.com/ingress-controller
`,
  },
]);

starters('rbac.authorization.k8s.io/v1', 'Role', [
  {
    id: 'read',
    label: 'Read-only in one namespace',
    description:
      'get alone does not permit list, and list returns whole objects rather than names — a list on ' +
      'secrets is a read of every credential in the namespace, which is why this one does not include ' +
      'them.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: example-read
rules:
  - apiGroups:
      - ""
    resources:
      - pods
      - services
      - configmaps
    verbs:
      - get
      - list
      - watch
  - apiGroups:
      - apps
    resources:
      - deployments
      - statefulsets
      - daemonsets
      - replicasets
    verbs:
      - get
      - list
      - watch
`,
  },
  {
    id: 'edit',
    label: 'Full control of workloads',
    description:
      'This includes secrets, which puts it close to namespace admin: anyone holding it can read every ' +
      'credential here and create a pod that mounts them. Removing secrets from the list below does not ' +
      'close that — a pod spec can still mount what its ServiceAccount may read.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: example-edit
rules:
  - apiGroups:
      - ""
    resources:
      - pods
      - services
      - configmaps
      - secrets
      - persistentvolumeclaims
      - serviceaccounts
    verbs:
      - get
      - list
      - watch
      - create
      - update
      - patch
      - delete
  - apiGroups:
      - apps
    resources:
      - deployments
      - statefulsets
      - daemonsets
      - replicasets
    verbs:
      - get
      - list
      - watch
      - create
      - update
      - patch
      - delete
`,
  },
  {
    id: 'exec',
    label: 'A shell into pods',
    description:
      'Subresources are separate resources with their own verbs: pods/exec takes create, not get, ' +
      'because opening a shell is a POST — a role granting get on pods/exec grants nothing and the ' +
      'denial names a permission that looks present. A shell in a pod is that pod’s ServiceAccount token ' +
      'and every volume it mounts.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: example-exec
rules:
  - apiGroups:
      - ""
    resources:
      - pods
    verbs:
      - get
      - list
  - apiGroups:
      - ""
    resources:
      - pods/log
    verbs:
      - get
  - apiGroups:
      - ""
    resources:
      - pods/exec
      - pods/portforward
    verbs:
      - create
`,
  },
]);

starters('rbac.authorization.k8s.io/v1', 'ClusterRole', [
  {
    id: 'read',
    label: 'Read-only across the cluster',
    description:
      'Bound with a ClusterRoleBinding this reads every namespace; bound with a RoleBinding it reads ' +
      'exactly one. The same object, two very different grants, and only the binding says which — the ' +
      'role itself is not where that is decided.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: example-read
rules:
  - apiGroups:
      - ""
    resources:
      - pods
      - services
      - configmaps
      - namespaces
      - nodes
    verbs:
      - get
      - list
      - watch
  - apiGroups:
      - apps
    resources:
      - deployments
      - statefulsets
      - daemonsets
      - replicasets
    verbs:
      - get
      - list
      - watch
`,
  },
  {
    id: 'aggregate-to-view',
    label: 'Extends the view role',
    description:
      'The label makes the aggregation controller merge these rules into the built-in view role, so ' +
      'everyone already bound to view gains them without any binding changing and without anything ' +
      'reporting that it happened. The audit row for this write is the only record that the grant moved.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: example-view-widgets
  labels:
    rbac.authorization.k8s.io/aggregate-to-view: "true"
rules:
  - apiGroups:
      - example.com
    resources:
      - widgets
    verbs:
      - get
      - list
      - watch
`,
  },
  {
    id: 'nonresource',
    label: 'Non-resource URLs',
    description:
      '/metrics and /healthz are endpoints rather than objects, and only a ClusterRole can grant them. ' +
      'nonResourceURLs and resources cannot appear in the same rule — a rule carrying both is refused, ' +
      'and a namespaced Role carrying nonResourceURLs is refused outright: namespaced rules cannot apply to non-resource URLs.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: example-metrics
rules:
  - nonResourceURLs:
      - /metrics
      - /healthz
    verbs:
      - get
`,
  },
]);

starters('rbac.authorization.k8s.io/v1', 'RoleBinding', [
  {
    id: 'serviceaccount',
    label: 'Role to a ServiceAccount',
    description:
      'Both names below are placeholders, and subjects[].namespace is the one to read twice: the ' +
      'dialog’s namespace fallback fills the object’s namespace and never the subject’s, the two are ' +
      'allowed to differ, and RBAC never checks that the account named here exists — a binding to an ' +
      'identity nobody has is created without complaint and grants nothing. roleRef is immutable, so a ' +
      'binding pointing at the wrong role has to be deleted and recreated.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: example
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: example-read
subjects:
  - kind: ServiceAccount
    name: example
    namespace: example
`,
  },
  {
    id: 'clusterrole-in-namespace',
    label: 'A ClusterRole, one namespace',
    description:
      'roleRef.kind: ClusterRole inside a RoleBinding grants that role’s rules in this namespace only, ' +
      'which is how view and edit are handed out. The identical roleRef in a ClusterRoleBinding grants ' +
      'them in every namespace, and the two objects read almost the same.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: example-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: Group
    apiGroup: rbac.authorization.k8s.io
    name: example-team
`,
  },
]);

// One starter, not two. A ClusterRoleBinding differs from a RoleBinding in
// scope rather than in shape, and the ServiceAccount-subject trap — a subject
// namespace nothing fills in — is already carried by the RoleBinding starter
// above. A second body here would restate it while adding a cluster-wide grant
// to the list of things a first click produces.
starters('rbac.authorization.k8s.io/v1', 'ClusterRoleBinding', [
  {
    id: 'group',
    label: 'Grant a ClusterRole cluster-wide',
    description:
      'This grants the role in every namespace that exists and every namespace created afterwards, and ' +
      'roleRef.kind can only be ClusterRole — a ClusterRoleBinding cannot reference a namespaced Role. ' +
      'Naming cluster-admin below hands over every verb on every resource, including RBAC itself and ' +
      'therefore the ability to remove whatever restricted the holder before.',
    text: `apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: example-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: Group
    apiGroup: rbac.authorization.k8s.io
    name: example-team
`,
  },
]);

starters('policy/v1', 'PodDisruptionBudget', [
  {
    id: 'min-available',
    label: 'Keep this many running',
    description:
      'A voluntary eviction is refused while it would drop the count below this. It stops nothing else — ' +
      'not a node failing, not a crash, not a delete — only evictions, which is what a drain uses and ' +
      'what makes a budget look ignored to anyone watching a deleted pod.',
    text: `apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: example
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: example
`,
  },
  {
    id: 'max-unavailable',
    label: 'Allow this many down',
    description:
      'The same rule counted against the desired replicas rather than absolutely, which is what a ' +
      'workload that scales wants. maxUnavailable: 0 permits no eviction at all and a drain of any node ' +
      'running one of these pods never finishes; the selector matters as much — in policy/v1 an empty ' +
      'one selects every pod in the namespace.',
    text: `apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: example
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: example
`,
  },
]);

starters('autoscaling/v2', 'HorizontalPodAutoscaler', [
  {
    id: 'cpu',
    label: 'Scale on CPU utilization',
    description:
      'averageUtilization is a percentage of the pod’s CPU request, so a workload whose containers state ' +
      'no request cannot be scaled on it at all — the autoscaler reports an unknown metric and holds the ' +
      'replica count where it is. While this exists, a manual scale of its target is reverted at the ' +
      'next sync.',
    text: `apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: example
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: example
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 80
`,
  },
  {
    id: 'behavior',
    label: 'With a scale-down brake',
    description:
      'Scaling up is immediate by default and scaling down already waits five minutes; this stretches ' +
      'the descent further and caps how much of the fleet one step may remove. When an autoscaler ' +
      'flaps, the stabilization window is the field to reach for — lowering the metric target instead ' +
      'changes where it settles, not how violently it gets there.',
    text: `apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: example
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: example
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 600
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
`,
  },
]);

starters('scheduling.k8s.io/v1', 'PriorityClass', [
  {
    id: 'preempting',
    label: 'Higher scheduling priority',
    description:
      'A PriorityClass has no spec — value, description and preemptionPolicy are top-level fields — and ' +
      'that is worth reading twice, because a document that nests them under spec does not fail: the ' +
      'API server drops the block it does not recognise, warns, and creates a PriorityClass with value ' +
      '0. This one preempts: a pod using it evicts running lower-priority pods when the cluster is full, ' +
      'and their own disruption budgets are only consulted on a best-effort basis.',
    text: `apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: example-high
value: 1000000
description: Scheduled before ordinary workloads, and preempts them when the cluster is full.
preemptionPolicy: PreemptLowerPriority
`,
  },
  {
    id: 'non-preempting',
    label: 'Priority without preemption',
    description:
      'Goes to the front of the scheduling queue and never evicts anything already running, which is ' +
      'what "important, but not at the cost of something else" actually looks like. It is not the ' +
      'default — omit preemptionPolicy and this class preempts.',
    text: `apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: example-high
value: 1000000
description: Scheduled ahead of ordinary workloads, but never evicts a running pod.
preemptionPolicy: Never
`,
  },
]);

starters('node.k8s.io/v1', 'RuntimeClass', [
  {
    id: 'handler',
    label: 'A runtime by name',
    description:
      'handler is top-level and required — a RuntimeClass has no spec — and it has to match a handler ' +
      'the CRI runtime is configured with on the nodes. Nothing validates that here, and a pod naming a ' +
      'class no node can honour stays Pending with the scheduler’s verdict as the only clue.',
    text: `apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: example
handler: example
`,
  },
  {
    id: 'scheduled',
    label: 'With scheduling and overhead',
    description:
      'scheduling keeps pods using this class on the nodes that can actually run it, instead of ' +
      'discovering that at start-up. overhead.podFixed is added to every such pod’s effective requests, ' +
      'so it is charged against the namespace’s ResourceQuota and a workload that fit before may stop ' +
      'fitting the moment it adopts this class.',
    text: `apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: example
handler: example
overhead:
  podFixed:
    cpu: 250m
    memory: 120Mi
scheduling:
  nodeSelector:
    example.com/runtime: example
  tolerations:
    - key: example.com/runtime
      operator: Equal
      value: example
      effect: NoSchedule
`,
  },
]);

starters('storage.k8s.io/v1', 'StorageClass', [
  {
    id: 'csi',
    label: 'A CSI driver',
    description:
      'provisioner is top-level and required; a StorageClass has no spec, and its parameters are defined ' +
      'by the driver rather than by Kubernetes, so nothing here can tell you a key is wrong. ' +
      'reclaimPolicy: Delete means deleting a claim destroys the data in the storage system, and only ' +
      'one class in a cluster should carry the default annotation.',
    text: `apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: example
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: csi.example.com
parameters:
  type: example
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
`,
  },
  {
    id: 'static',
    label: 'For hand-made volumes',
    description:
      'kubernetes.io/no-provisioner provisions nothing — the class exists only so claims and hand-made ' +
      'PersistentVolumes can name each other. WaitForFirstConsumer defers binding until a pod is ' +
      'scheduled, which is what stops a node-local volume binding to a node the pod cannot run on.',
    text: `apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-storage
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
`,
  },
]);

starters('snapshot.storage.k8s.io/v1', 'VolumeSnapshot', [
  {
    id: 'from-claim',
    label: 'Snapshot a claim',
    description:
      'A snapshot is not a backup: it usually lives in the same storage system as the volume it copies, ' +
      'so whatever loses that system loses both. Whether it is usable is status.readyToUse, which is ' +
      'null while the storage system is still working — not false, and rendering the two alike is how a ' +
      'snapshot that failed hours ago passes for one in progress.',
    text: `apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: example
spec:
  volumeSnapshotClassName: example
  source:
    persistentVolumeClaimName: example
`,
  },
  {
    id: 'from-content',
    label: 'Adopt an existing snapshot',
    description:
      'Binds to a VolumeSnapshotContent that already exists instead of taking a new snapshot — exactly ' +
      'one of the two source fields may be set. volumeSnapshotClassName is ignored for this shape, so ' +
      'leaving it in writes a line that reads as configuration and governs nothing.',
    text: `apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: example
spec:
  source:
    volumeSnapshotContentName: example-content
`,
  },
]);

starters('snapshot.storage.k8s.io/v1', 'VolumeSnapshotClass', [
  {
    id: 'delete',
    label: 'Deleted with their object',
    description:
      'driver and deletionPolicy are both top-level and both required. Delete means removing a ' +
      'VolumeSnapshot destroys the snapshot data in the storage system — intended, immediate, and not ' +
      'undoable from anywhere in this console.',
    text: `apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: example
driver: csi.example.com
deletionPolicy: Delete
`,
  },
  {
    id: 'retain-default',
    label: 'Kept, and the default',
    description:
      'Retain leaves the snapshot in the storage system when its Kubernetes object goes away, which also ' +
      'means nothing in Kubernetes will ever clean it up and no listing here will show what is still ' +
      'being paid for. One default class per cluster, and the annotation value has to be a string.',
    text: `apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: example
  annotations:
    snapshot.storage.kubernetes.io/is-default-class: "true"
driver: csi.example.com
deletionPolicy: Retain
`,
  },
]);

// Registered at v1 and nowhere else, on purpose — see the module docstring. The
// Gateway tabs resolve the served version at runtime, so these open on a
// cluster serving v1 and fall back to the skeleton on one serving v1beta1,
// which is the honest outcome rather than a gap.
starters('gateway.networking.k8s.io/v1', 'GatewayClass', [
  {
    id: 'class',
    label: 'A Gateway implementation',
    description:
      'spec.controllerName is immutable and has to be the exact string the controller watches for. ' +
      'Nothing validates it: a Gateway naming a class no controller accepted stays unprogrammed with no ' +
      'address, and neither object carries an error saying why.',
    text: `apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: example
spec:
  controllerName: example.com/gateway-controller
`,
  },
]);

starters('gateway.networking.k8s.io/v1', 'Gateway', [
  {
    id: 'http',
    label: 'One HTTP listener',
    description:
      'allowedRoutes.namespaces.from decides who may attach routes here: Same keeps it to this ' +
      'namespace, All lets any namespace in the cluster put traffic through this Gateway. Listener names ' +
      'have to be unique — a route’s sectionName is what points at one of them.',
    text: `apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: example
spec:
  gatewayClassName: example
  listeners:
    - name: http
      protocol: HTTP
      port: 80
      allowedRoutes:
        namespaces:
          from: Same
`,
  },
  {
    id: 'https',
    label: 'An HTTPS listener',
    description:
      'mode: Terminate requires certificateRefs and Passthrough forbids them. A Secret in another ' +
      'namespace additionally needs a ReferenceGrant in that namespace — without one the listener ' +
      'reports RefNotPermitted and serves nothing, while the Gateway itself still looks created.',
    text: `apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: example
spec:
  gatewayClassName: example
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      hostname: example.com
      tls:
        mode: Terminate
        certificateRefs:
          - group: ""
            kind: Secret
            name: example-tls
      allowedRoutes:
        namespaces:
          from: Same
`,
  },
]);

starters('gateway.networking.k8s.io/v1', 'HTTPRoute', [
  {
    id: 'prefix',
    label: 'Route a path prefix',
    description:
      'The match type is PathPrefix — Prefix is the Ingress spelling and this API rejects it — and ' +
      'backendRefs[].port is required for a Service backend. It is the **Service’s** port, not the ' +
      'container’s: 80 here matches the Service starters, whose targetPort is what reaches 8080 in the ' +
      'pod, and a route sent to 8080 resolves no port and forwards nothing. A route whose parentRefs ' +
      'name no existing Gateway is accepted, attaches to nothing, and says so only in its status.',
    text: `apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example
spec:
  parentRefs:
    - name: example
  hostnames:
    - example.com
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: example
          port: 80
`,
  },
  {
    id: 'canary',
    label: 'Split traffic two ways',
    description:
      'Weights are relative, not percentages: 90 and 10 splits exactly as 9 and 1 does, and removing one ' +
      'backend sends everything to the other rather than dropping its share. A backend at weight 0 stays ' +
      'configured and receives nothing, which is the shape to leave behind after a rollout. Both ports ' +
      'are the Services’ own, as above.',
    text: `apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example
spec:
  parentRefs:
    - name: example
  hostnames:
    - example.com
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: example
          port: 80
          weight: 90
        - name: example-canary
          port: 80
          weight: 10
`,
  },
]);

/* ── The kinds this file deliberately leaves to the skeleton ─────────────── */

// Not an oversight and not a backlog. Each of these is a kind the console lists
// and a kind a worked example would make worse:
//
// * Endpoints and EndpointSlice are written by the endpoints controller for
//   every Service that has a selector, and it overwrites a hand-written one
//   within seconds without reporting that it did. The single honest case — a
//   selectorless Service pointing outside the cluster — is indistinguishable in
//   the document from the case that gets silently reverted.
// * PersistentVolume: a starter here is a pointer at a backing store, and the
//   failure of a wrong one is two clusters mounting the same disk. What makes
//   it wrong is knowledge of the storage system, which nothing here has.
// * Lease is a lock a controller holds. The ones in kube-node-lease are node
//   heartbeats the node controller reads to decide a node is gone and start
//   evicting its pods; the ones in kube-system are leader election. Creating
//   one by hand is how two leaders end up running at once.
// * CertificateSigningRequest: spec.request is base64 of a PKCS#10 request
//   whose private key never appears in the object. This console cannot generate
//   a key pair and must not pretend to — a starter with an empty request is a
//   form for obtaining a certificate nobody holds the key to.
// * ValidatingWebhookConfiguration and MutatingWebhookConfiguration: a starter
//   naming a service that does not exist, with the API's own default
//   failurePolicy of Fail, breaks every create and update it matches in the
//   cluster — including the write that would remove it.
// * The Gateway policy kinds this console resolves at runtime, BackendTLSPolicy
//   first among them: the shape moved between channels (spec.targetRef became
//   spec.targetRefs, spec.tls became spec.validation), so a starter written for
//   one alpha version is a manifest whose required fields the served version
//   does not have.
//
// All of them get `skeletonStarter()`, which names the kind and claims nothing
// else.

/**
 * The smallest document that names a kind, for the kinds this console ships no
 * starter for.
 *
 * Deliberately not a guess at the spec. A CRD's required fields live in its
 * schema, which this console does not read, and a plausible-looking `spec:`
 * block invented here would be the defect standard applied to a text editor —
 * a confident answer about somebody's API, produced by a console that does not
 * know it. What it produces instead is a document that is unambiguously
 * unfinished, and says so in its description.
 */
export function skeletonStarter({ apiVersion, kind }) {
  return {
    id: 'skeleton',
    apiVersion,
    kind,
    label: 'Minimal object',
    description:
      `This console ships no starter for ${kind}. Below is the smallest document that names the kind — ` +
      'what belongs under it is defined by whoever installed this API, and the dry run is what will tell ' +
      'you whether the API server accepts what you write.',
    text: `apiVersion: ${apiVersion}\nkind: ${kind}\nmetadata:\n  name: example\n`,
  };
}

/**
 * The starters offered for one kind — never empty.
 *
 * A kind with none of its own gets the skeleton, so every create button has
 * something to open with and no caller has to branch on whether this file has
 * heard of the resource it is looking at.
 */
export function templatesFor(entry) {
  if (!entry?.apiVersion || !entry?.kind) return [];
  return STARTERS.get(`${entry.apiVersion}\u0000${entry.kind}`) ?? [skeletonStarter(entry)];
}

/**
 * Every registered starter, flattened. Nothing in the app calls this; the
 * Playwright suite does.
 *
 * The properties that matter about a starter are the ones no click reveals. A
 * body that parses but names a different `kind`, one carrying a
 * `metadata.namespace` the dialog was going to supply itself, one with a
 * comment the first form edit would silently drop — each opens in the editor
 * looking exactly like a correct one, and the operator meets the difference as
 * a rejected create or as a rewrite warning they did nothing to earn. A spec
 * can settle all of that by parsing, but only over the starters it can reach:
 * one that drives the pages opens the handful of kinds those pages have buttons
 * for and never sees the rest. Handing it the registry instead means a starter
 * added later is covered by having been registered, which is the only version
 * of this check that is still true of the sixtieth entry.
 */
export function allStarters() {
  return [...STARTERS.values()].flat();
}

/** Does this console ship a starter of its own for this kind? */
export function hasStarters(entry) {
  return Boolean(entry?.apiVersion && entry?.kind && STARTERS.has(`${entry.apiVersion}\u0000${entry.kind}`));
}
