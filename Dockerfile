# Quorum, in a container.
#
# Small on purpose. The GPU work does not happen here: the SAM 3.1 model runs
# in its own image, either as the nuclio function this talks to over the
# network or as a container this starts through the docker socket. What this
# image needs is python, ffmpeg and about 300 MB — which is why it can be
# rebuilt in seconds and why `./run.sh` on the host stays the fast path for
# development.
#
# Three things it deliberately does not do:
#
#   · it does not copy `data/`. State is a bind mount; an image with a 387 MB
#     sqlite database baked into it is not an image, it is a backup.
#   · it does not copy `plugins/` into a layer that a rebuild is needed to
#     change. Both `plugins/` and `web/` are bind-mounted in compose, because
#     this app has no build step and editing a plugin should mean reloading a
#     tab, not rebuilding an image.
#   · it does not install the docker CLI unless you ask (`--build-arg
#     WITH_DOCKER=1`). Only the auto-annotator needs it, and a container that
#     can drive the host's daemon is a container that can do anything on the
#     host — that should be a decision, not a default. It is `docker-cli`, not
#     `docker.io`: this needs the *client*, and on Debian 13 the engine package
#     no longer ships one (installing it gives you containerd and no `docker`,
#     which is a confusing way to find that out).
FROM python:3.12-slim

ARG WITH_DOCKER=0

# ffmpeg is not optional here: the frame map, the clock offsets, the still
# frames and the SAM interactor all read packet timestamps out of a video.
# `quorum.probe` raises `NoFFmpeg` with a readable message rather than
# guessing, so leaving it out fails honestly — but it fails.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && if [ "$WITH_DOCKER" = "1" ]; then apt-get install -y --no-install-recommends docker-cli; fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/quorum

# Dependencies first, so editing the source does not re-resolve them.
COPY pyproject.toml ./
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.27" "numpy>=1.24"

COPY quorum/ ./quorum/
COPY web/ ./web/
COPY plugins/ ./plugins/
COPY docker/ ./docker/
RUN chmod +x docker/entrypoint.sh

# QUORUM_CONFIG is set in the *image* rather than exported by the entrypoint,
# because `docker exec` inherits the image's environment and not the shell the
# entrypoint runs in. Without it, `docker exec quorum python -m quorum job …`
# silently falls back to the default config and talks to a different, empty
# database — which looks exactly like "my capture disappeared".
ENV PYTHONUNBUFFERED=1 QUORUM_CONFIG=/tmp/quorum.toml
EXPOSE 8600

# A container that is up but cannot answer is worse than one that is down,
# because nothing restarts it.
HEALTHCHECK --interval=20s --timeout=3s --start-period=15s \
  CMD curl -fsS http://127.0.0.1:8600/health || exit 1

ENTRYPOINT ["/srv/quorum/docker/entrypoint.sh"]
CMD ["python", "-m", "quorum", "serve"]
