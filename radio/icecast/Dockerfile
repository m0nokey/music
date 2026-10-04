# syntax=docker/dockerfile:1

FROM alpine:3.24.1 AS builder

ARG ICECAST_KH_VERSION=icecast-2.4.0-kh22

RUN apk add --no-cache \
    autoconf \
    automake \
    build-base \
    ca-certificates \
    curl \
    curl-dev \
    libogg-dev \
    libtheora-dev \
    libtool \
    libvorbis-dev \
    libxml2-dev \
    libxslt-dev \
    openssl-dev \
    pkgconf \
    speex-dev

WORKDIR /build

RUN curl -fL --proto '=https' --tlsv1.2 \
    "https://github.com/karlheyes/icecast-kh/archive/refs/tags/${ICECAST_KH_VERSION}.tar.gz" \
    -o icecast-kh.tar.gz \
    && tar -xzf icecast-kh.tar.gz --strip-components=1 \
    && rm -f icecast-kh.tar.gz

RUN sed -i 's/xmlErrorPtr error/const xmlError *error/g' src/cfgfile.c src/cfgfile.h
RUN ./configure \
    --prefix=/usr/local \
    --sysconfdir=/etc/icecast \
    --localstatedir=/var \
    --with-curl-config=/usr/bin/curl-config \
    --with-openssl \
    && make -j"$(nproc)" \
    && make install DESTDIR=/out

FROM alpine:3.24.1

LABEL org.opencontainers.image.title="music-icecast-kh"

RUN apk add --no-cache \
    ca-certificates \
    curl \
    libcurl \
    libogg \
    libssl3 \
    libtheora \
    libvorbis \
    libxml2 \
    libxslt \
    openssl \
    speex \
    wget \
    && addgroup -g 10000 -S radio \
    && adduser -u 10001 -S -D -H -G radio icecast \
    && mkdir -p /etc/icecast /run/icecast /var/log/icecast /usr/local/share/icecast \
    && chown -R icecast:radio /etc/icecast /run/icecast /var/log/icecast

COPY --from=builder /out/ /
COPY icecast/entrypoint.sh /usr/local/bin/radio-icecast-entrypoint

RUN chmod 0755 /usr/local/bin/radio-icecast-entrypoint

USER icecast
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/radio-icecast-entrypoint"]
