#!/bin/sh
set -eu

read_secret() {
    file="$1"
    [ -r "$file" ] || {
        echo "missing secret: $file" >&2
        exit 1
    }
    value=$(tr -d '\r\n' < "$file")
    [ -n "$value" ] || {
        echo "empty secret: $file" >&2
        exit 1
    }
    case "$value" in
        *[!A-Za-z0-9._@+-]*)
            echo "secret contains unsupported XML characters: $file" >&2
            exit 1
            ;;
    esac
    printf '%s' "$value"
}

SOURCE_PASSWORD=$(read_secret /run/secrets/icecast_source_password)
ADMIN_PASSWORD=$(read_secret /run/secrets/icecast_admin_password)

: "${ICECAST_HOSTNAME:?ICECAST_HOSTNAME is required}"
: "${ICECAST_STREAM_URL:?ICECAST_STREAM_URL is required}"
: "${ICECAST_ADMIN_EMAIL:?ICECAST_ADMIN_EMAIL is required}"
: "${ICECAST_MAX_CLIENTS:?ICECAST_MAX_CLIENTS is required}"
: "${ICECAST_MAX_SOURCES:?ICECAST_MAX_SOURCES is required}"

case "$ICECAST_HOSTNAME" in
    *[!A-Za-z0-9.-]*) echo "invalid ICECAST_HOSTNAME" >&2; exit 1 ;;
esac
case "$ICECAST_STREAM_URL" in
    *[!A-Za-z0-9:/._?=\&+-]*) echo "invalid ICECAST_STREAM_URL" >&2; exit 1 ;;
esac
case "$ICECAST_ADMIN_EMAIL" in
    *[!A-Za-z0-9._@+-]*) echo "invalid ICECAST_ADMIN_EMAIL" >&2; exit 1 ;;
esac
case "$ICECAST_MAX_CLIENTS:$ICECAST_MAX_SOURCES" in
    *[!0-9:]*) echo "client/source limits must be numeric" >&2; exit 1 ;;
esac

runtime=/tmp/icecast.xml
sed \
    -e "s|__ICECAST_SOURCE_PASSWORD__|$SOURCE_PASSWORD|g" \
    -e "s|__ICECAST_ADMIN_PASSWORD__|$ADMIN_PASSWORD|g" \
    -e "s|__ICECAST_HOSTNAME__|$ICECAST_HOSTNAME|g" \
    -e "s|__ICECAST_STREAM_URL__|$ICECAST_STREAM_URL|g" \
    -e "s|__ICECAST_ADMIN_EMAIL__|$ICECAST_ADMIN_EMAIL|g" \
    -e "s|__ICECAST_MAX_CLIENTS__|$ICECAST_MAX_CLIENTS|g" \
    -e "s|__ICECAST_MAX_SOURCES__|$ICECAST_MAX_SOURCES|g" \
    /etc/icecast/icecast.xml.template > "$runtime"

chmod 0600 "$runtime"
chown icecast:radio "$runtime"
chown -R icecast:radio /var/log/icecast
exec su-exec icecast /usr/local/bin/icecast -c "$runtime"
