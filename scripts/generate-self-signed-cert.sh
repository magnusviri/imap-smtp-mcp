#!/usr/bin/env sh
set -eu

output_dir=${1:-./tls}
mkdir -p "$output_dir"
umask 077
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
  -keyout "$output_dir/key.pem" \
  -out "$output_dir/cert.pem" \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 "$output_dir/key.pem"
chmod 644 "$output_dir/cert.pem"
