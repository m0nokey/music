# syntax=docker/dockerfile:1

FROM alpine:3.24.1 AS healthcheck

FROM ghcr.io/static-web-server/static-web-server:2.44.0@sha256:2c1a7c3e0feaea5859307403b74e1c575f3ec1499094fc077344173d11abaae2 AS runtime

LABEL org.opencontainers.image.title="music-web"

ENV SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8080 \
    SERVER_ROOT=/public \
    SERVER_FALLBACK_PAGE=/public/index.html \
    SERVER_HEALTH=true \
    SERVER_LOG_LEVEL=info \
    SERVER_CACHE_CONTROL_HEADERS=false

COPY --from=healthcheck /bin/busybox /bin/busybox
COPY --from=healthcheck /lib/ /lib/
COPY --chown=65532:65532 public/ /public/

USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/bin/busybox", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/health"]
