#!/bin/sh
set -eu
mkdir -p /etc/nginx/certs
if [ ! -f /etc/nginx/certs/cert.pem ] || [ ! -f /etc/nginx/certs/key.pem ]; then
    apk add --no-cache openssl >/dev/null
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
        -keyout /etc/nginx/certs/key.pem \
        -out /etc/nginx/certs/cert.pem \
        -subj "/CN=fnol-workbench"
fi
exec /docker-entrypoint.sh nginx -g "daemon off;"
