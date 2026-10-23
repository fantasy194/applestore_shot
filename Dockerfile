# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:24-bookworm-slim
ARG RUST_IMAGE=rust:1.88-bookworm
FROM ${NODE_IMAGE} AS frontend
WORKDIR /build
RUN npm install --global pnpm@11.9.0
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store pnpm install --frozen-lockfile --store-dir /pnpm/store \
    --network-concurrency=4 --fetch-timeout=600000
COPY tsconfig.json index.html ./
COPY src ./src
COPY public ./public
COPY assets ./assets
RUN pnpm exec tsc --noEmit && pnpm exec vite build --mode local-web --outDir /www --emptyOutDir

FROM ${RUST_IMAGE} AS backend
ARG TARGETARCH
WORKDIR /build
ENV CARGO_BUILD_JOBS=4 CARGO_HTTP_TIMEOUT=600 CARGO_HTTP_LOW_SPEED_LIMIT=1
COPY local-web/Cargo.toml local-web/Cargo.lock ./local-web/
COPY local-web/src ./local-web/src
COPY src-tauri/src ./src-tauri/src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,id=duo-cargo-target-${TARGETARCH},target=/build/local-web/target \
    cargo build --release --locked --manifest-path local-web/Cargo.toml \
    && cp local-web/target/release/duo-killer-web /duo-killer-web

FROM python:3.12-slim-bookworm AS runtime
ARG TARGETARCH
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PATH=/opt/venv/bin:$PATH \
    DISPLAY=:99 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    DUO_WEB_PORT=17654
# Both architectures use an installed GUI browser connected through local CDP.
# Google Chrome is available for amd64; Debian Chromium provides native arm64.
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    case "$TARGETARCH" in amd64|arm64) ;; *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; esac \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install --no-install-recommends -y ca-certificates curl xvfb x11-utils openbox fonts-noto-cjk fonts-liberation tzdata \
    && useradd --create-home --uid 1000 --shell /bin/sh duo \
    && install -d -o duo -g duo -m 0700 /data
ARG CHROME_NO_PROXY
COPY docker/download.py /usr/local/lib/duo-download.py
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/cache/duo-chrome \
    if [ "$TARGETARCH" = arm64 ]; then \
      apt-get -o Acquire::Retries=5 install --no-install-recommends -y chromium chromium-sandbox; \
    else \
      no_proxy="${CHROME_NO_PROXY:-${NO_PROXY:-${no_proxy:-}}}" python /usr/local/lib/duo-download.py \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb /var/cache/duo-chrome/chrome.deb \
    && cp /var/cache/duo-chrome/chrome.deb /tmp/chrome.deb \
    && apt-get -o Acquire::Retries=5 install --no-install-recommends -y /tmp/chrome.deb \
      && rm -f /tmp/chrome.deb; \
    fi
WORKDIR /app
COPY worker/requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/var/cache/duo-wheels \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --timeout=60 --retries=8 \
        --find-links=/var/cache/duo-wheels --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY worker ./worker
COPY docker/worker-launcher.sh ./worker/duo-killer-worker
COPY docker/entrypoint.py docker/healthcheck.py ./docker/
COPY --from=backend /duo-killer-web ./duo-killer-web
COPY --from=frontend /www ./www
RUN chmod 0755 /app/duo-killer-web /app/worker/duo-killer-worker \
    && install -d -m 1777 /tmp/.X11-unix
USER duo
VOLUME ["/data"]
EXPOSE 17654
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
