#!/bin/sh
set -eu

password_file=/run/secrets/icecast_source_password
template=/etc/liquidsoap/radio.liq.template
runtime=/tmp/radio.liq

[ -r "$password_file" ] || { echo "source password secret is missing" >&2; exit 1; }
password=$(tr -d '\r\n' < "$password_file")
[ -n "$password" ] || { echo "source password secret is empty" >&2; exit 1; }
case "$password" in
    *[!A-Za-z0-9._@+-]*) echo "source password contains unsupported characters" >&2; exit 1 ;;
esac

sed "s|__ICECAST_SOURCE_PASSWORD__|$password|g" "$template" > "$runtime"
chmod 0600 "$runtime"
chown liquidsoap "$runtime"
exec su-exec liquidsoap /usr/bin/liquidsoap "$runtime"
