FROM docker:29-cli AS docker

FROM python:3.13-slim

# git for pulling; docker CLI + buildx (from the official image) to build/push via the host daemon.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates openssh-client \
 && rm -rf /var/lib/apt/lists/*
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx

COPY autobuild /app/autobuild

# Relative paths in the config (e.g. ./data/repos) resolve inside the /data volume.
WORKDIR /data
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    AUTOBUILD_CONFIG=/config/autobuild.toml
VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["python", "-m", "autobuild"]
CMD ["run"]
