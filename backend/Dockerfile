# k8boss-admin backend.
#
# Two stages for one reason: the wheels for cryptography and psycopg2-binary
# occasionally need a compiler, and a compiler in the *runtime* image is a
# permanent liability in a container that holds decryption keys for every
# registered cluster's credentials. So the build toolchain lives in a stage that
# is thrown away, and the final layer contains an interpreter, the installed
# packages, and the application. Nothing else.

# **The build context is the repository root, not `backend/`.** Docker cannot
# COPY from outside its context, and this image needs one thing that is not
# under `backend/`: the vendored Operator Lifecycle Manager release in
# `deploy/olm`, which §33 reads at run time and pins by SHA-256. Built from
# `backend/` it was silently absent, and the symptom was a 500 from
# `GET /api/portal/olm` for a path that is nobody's layout (`/deploy/olm`) while
# every other endpoint worked. Build it as:
#
#     docker build -f backend/Dockerfile -t k8boss-admin-backend .
#
# ── Stage 1: build the dependency tree ───────────────────────────────────────
FROM python:3.12-slim AS builder

# gcc and the libpq headers are here and only here. Most builds will not use
# them — the pinned versions all publish manylinux wheels — but a wheel that is
# missing for the platform being built (arm64 is the usual one) otherwise turns
# into a build failure whose message is a compiler that does not exist.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY backend/requirements.txt .

# Install into a self-contained prefix that the runtime stage copies wholesale.
# A virtualenv rather than --user: the path is explicit, so the runtime stage's
# PATH is one line and does not depend on which user ends up running the process.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim
LABEL org.opencontainers.image.title="k8boss-admin backend" \
      org.opencontainers.image.description="Kubernetes administration console API" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/aesaganda/k8boss-admin"

# Non-root, with a fixed uid/gid. Fixed, because the SQLite database and the
# encryption key file live on a mounted volume: a uid assigned by the distro at
# image build time can change when the base image does, and the symptom of that
# is a console that starts, fails to open its own database, and reports every
# registered cluster as gone. A pinned uid makes the volume's ownership a
# property an operator can set once.
RUN groupadd --gid 10001 k8boss \
    && useradd --uid 10001 --gid 10001 --home-dir /app --shell /sbin/nologin k8boss

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=k8boss:k8boss backend/ .

# Upstream's OLM manifests, byte for byte, beside the application rather than at
# the repository path — `app.admin.olm_bundle` looks in both. Their digests are
# pinned in `olm_bundle.FILE_DIGESTS` and checked on every read, so a file that
# arrived here altered refuses to load rather than installing something nobody
# reviewed.
COPY --chown=k8boss:k8boss deploy/olm/ ./deploy/olm/

# The data directory holds the SQLite database and, next to it, the generated
# Fernet key that encrypts stored cluster tokens (app/crypto.key_file_path puts
# them together on purpose, so a volume snapshot captures both or neither).
# Created and owned here so the container works with an empty named volume and
# with no volume at all.
RUN mkdir -p /app/data && chown -R k8boss:k8boss /app/data

# Point the default database at that directory, because the sentence above was
# not true without it. `app.config`'s default is `sqlite:///./k8boss_admin.db` —
# relative to the working directory, which is `/app`, which is owned by root
# while this process runs as 10001. A plain `docker run` of this image therefore
# died at startup with "unable to open database file", and the only reason
# nobody saw it is that docker-compose.yml overrides DATABASE_URL to this exact
# value. An image that cannot start without the compose file around it is not a
# shipped artifact, it is half of one. `app.crypto` persists its generated key
# beside the database, so this also keeps the key and the rows it protects in
# the one directory a volume is mounted on — capture both or neither.
ENV DATABASE_URL=sqlite:////app/data/k8boss_admin.db

USER 10001:10001

EXPOSE 8020

# The health check must not depend on a cluster. GET /api/health reports on the
# console, never on the clusters it administers — an unreachable customer
# cluster appears in `degraded[]` and leaves `status` at 200. Probing anything
# that touches a cluster here would restart a healthy pod because someone else's
# API server went down.
# Timings sized for a loaded host, not an idle one. The probe spawns a cold
# interpreter and imports httpx before it makes a request, which is tens of
# milliseconds on an idle machine and seconds on a busy one — and a deployment
# running the `autoheal` sidecar (see docker-compose.yml) restarts whatever
# Docker marks unhealthy. At timeout=5s that turned a load spike into a restart
# of a console that was answering every request, and every restart made the next
# probe more likely to time out. The generous start_period covers the same thing
# during boot, where failing probes do not count against the retries at all.
HEALTHCHECK --interval=30s --timeout=15s --start-period=45s --retries=3 \
  CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8020/api/health', timeout=10).status_code==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020", "--log-level", "info"]
