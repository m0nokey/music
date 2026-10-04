# syntax=docker/dockerfile:1

ARG LIQUIDSOAP_IMAGE=ghcr.io/savonet/liquidsoap-alpine:v2.4.5
FROM ${LIQUIDSOAP_IMAGE}

LABEL org.opencontainers.image.title="music-liquidsoap"

USER root
RUN apk add --no-cache su-exec \
    && install -d -m 0755 /etc/liquidsoap /run/liquidsoap

COPY liquidsoap/entrypoint.sh /usr/local/bin/radio-liquidsoap-entrypoint
RUN chmod 0755 /usr/local/bin/radio-liquidsoap-entrypoint

ENTRYPOINT ["/usr/local/bin/radio-liquidsoap-entrypoint"]
