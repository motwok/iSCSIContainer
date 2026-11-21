# Module service: builds the patched iscsi_target_mod for the running Debian kernel.
# Build context is the repository root.
FROM debian:trixie-slim
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        python3 ca-certificates build-essential bc bison flex libelf-dev libssl-dev \
        dpkg-dev patch kmod xz-utils cpio \
    && rm -rf /var/lib/apt/lists/*
COPY src/iscsimodule /usr/lib/python3/dist-packages/iscsimodule
COPY patches /usr/share/iscsicontainer/patches
RUN printf '#!/usr/bin/python3\nfrom iscsimodule.service import entry\nentry()\n' \
        > /usr/local/bin/iscsimodule \
    && chmod +x /usr/local/bin/iscsimodule
ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="iSCSIContainer module service" \
      org.opencontainers.image.description="Builds the patched iscsi_target_mod for iSCSIContainer" \
      org.opencontainers.image.source="https://github.com/motwok/iSCSIContainer" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.authors="Emmo Emminghaus"
VOLUME /modules
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=20m --retries=2 \
    CMD ["iscsimodule", "health"]
ENTRYPOINT ["iscsimodule"]
CMD ["run"]
