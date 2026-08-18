# k8boss-admin frontend: Vite build, nginx runtime.
#
# ── Stage 1: build ───────────────────────────────────────────────────────────
# Pinned to $BUILDPLATFORM — the builder's native architecture — on purpose. Vite
# emits architecture-independent static assets, so this stage runs once and its
# output is shared by every target platform. Without the pin, a multi-arch
# `docker buildx --platform=linux/amd64,linux/arm64` runs the whole JS build a
# second time under QEMU emulation, where the native bundler binaries crash with
# an illegal instruction. Building native is also several minutes faster.
FROM --platform=$BUILDPLATFORM node:20-alpine AS builder

WORKDIR /app

# package.json + lockfile first, so a source-only change reuses the install
# layer. `npm ci` and not `npm install`: ci installs exactly the lockfile, which
# is what makes the image reproducible and what CI runs.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ── Stage 2: serve ───────────────────────────────────────────────────────────
FROM nginx:1.27-alpine
LABEL org.opencontainers.image.title="k8boss-admin frontend" \
      org.opencontainers.image.description="Kubernetes administration console UI" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/aesaganda/k8boss-admin"

# Where /api/ is proxied. An env var rather than a baked-in hostname so the same
# image serves compose (service name `backend`) and Kubernetes (the backend
# Service DNS name) — the nginx entrypoint renders the template on start.
ENV BACKEND_URL=http://backend:8020

COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=builder /app/dist /usr/share/nginx/html

# Non-root. The template is rendered into /etc/nginx/conf.d at container start,
# so that directory has to be writable by the running user — this is the step
# that is easy to miss, and its symptom is a container that exits immediately
# with a permission error from a script nobody wrote.
RUN chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx/conf.d \
    && touch /var/run/nginx.pid && chown nginx:nginx /var/run/nginx.pid

USER nginx

# 8021, above 1024, so the unprivileged user can bind it without NET_BIND_SERVICE.
EXPOSE 8021

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1:8021/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
