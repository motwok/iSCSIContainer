FROM debian:trixie-slim AS base
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        python3 python3-rtslib-fb python3-yaml targetcli-fb kmod iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY src/iscsicontainer /usr/lib/python3/dist-packages/iscsicontainer
RUN printf '#!/usr/bin/python3\nfrom iscsicontainer.cli import entry\nentry()\n' \
        > /usr/local/bin/iscsictl \
    && chmod +x /usr/local/bin/iscsictl

FROM base AS test
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3-pytest python3-pytest-cov open-iscsi xz-utils \
    && rm -rf /var/lib/apt/lists/*
COPY src/iscsimodule /usr/lib/python3/dist-packages/iscsimodule
COPY patches /usr/share/iscsicontainer/patches
# Measures processes the tests start (iscsictl run) when COVERAGE_PROCESS_START is set.
RUN echo 'import coverage; coverage.process_startup()' \
        > /usr/lib/python3/dist-packages/zz-coverage.pth
WORKDIR /work
COPY pyproject.toml ./
COPY tests tests

FROM base
ARG VERSION=0.0.0
RUN echo "__version__ = \"${VERSION}\"" > /usr/lib/python3/dist-packages/iscsicontainer/__init__.py
LABEL org.opencontainers.image.title="iSCSIContainer" \
      org.opencontainers.image.description="iSCSI target (Linux LIO) configured by YAML" \
      org.opencontainers.image.source="https://github.com/motwok/iSCSIContainer" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.authors="Emmo Emminghaus"
VOLUME /config
EXPOSE 3260 9482
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=20m --retries=2 \
    CMD ["iscsictl", "health"]
ENTRYPOINT ["iscsictl"]
CMD ["run"]
