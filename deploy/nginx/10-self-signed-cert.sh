#!/bin/sh
set -eu

CERT_DIR="${TLS_CERT_DIR:-/etc/nginx/certs}"
CERT_FILE="$CERT_DIR/server.crt"
KEY_FILE="$CERT_DIR/server.key"

if [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ] && [ "${TLS_REGENERATE:-0}" != "1" ]; then
    echo "TLS: reusing existing certificate $CERT_FILE"
    exit 0
fi

TLS_HOSTS="${TLS_HOSTS:-localhost,127.0.0.1}"
TLS_CN="${TLS_CN:-localhost}"
TLS_CERT_DAYS="${TLS_CERT_DAYS:-825}"
TLS_KEY_BITS="${TLS_KEY_BITS:-2048}"

mkdir -p "$CERT_DIR"

san=""
old_ifs="$IFS"
IFS=','
for host in $TLS_HOSTS; do
    host="$(printf '%s' "$host" | tr -d '[:space:]')"
    [ -n "$host" ] || continue
    case "$host" in
        *[!0-9.]*)
            san="${san},DNS:${host}"
            ;;
        *)
            san="${san},IP:${host}"
            ;;
    esac
done
IFS="$old_ifs"
san="${san#,}"
[ -n "$san" ] || san="DNS:localhost"

echo "TLS: generating self-signed certificate for ${san} (${TLS_CERT_DAYS} days)"
openssl req -x509 -nodes -newkey "rsa:${TLS_KEY_BITS}" \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -days "$TLS_CERT_DAYS" \
    -subj "/CN=${TLS_CN}" \
    -addext "subjectAltName=${san}" >/dev/null 2>&1

chmod 600 "$KEY_FILE"
chmod 644 "$CERT_FILE"

echo "TLS: certificate written to $CERT_FILE"
