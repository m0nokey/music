# syntax=docker/dockerfile:1

FROM ghcr.io/static-web-server/static-web-server:2.44.0@sha256:2c1a7c3e0feaea5859307403b74e1c575f3ec1499094fc077344173d11abaae2

LABEL org.opencontainers.image.title="music-web"

ENV SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8080 \
    SERVER_ROOT=/public \
    SERVER_FALLBACK_PAGE=/public/index.html \
    SERVER_HEALTH=true \
    SERVER_LOG_LEVEL=info \
    SERVER_CACHE_CONTROL_HEADERS=false

COPY --chown=65532:65532 public/ /public/

USER 65532:65532
EXPOSE 8080
