#!/bin/sh
set -eu

: "${TUNNEL_HOST:?Set TUNNEL_HOST to the SSH bastion hostname}"
: "${TUNNEL_USER:?Set TUNNEL_USER to the SSH username}"
: "${DOCUMENTDB_HOST:?Set DOCUMENTDB_HOST to the private database hostname}"

TUNNEL_LOCAL_PORT="${TUNNEL_LOCAL_PORT:-27017}"
DOCUMENTDB_PORT="${DOCUMENTDB_PORT:-27017}"

set -- ssh -N \
    -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:${TUNNEL_LOCAL_PORT}:${DOCUMENTDB_HOST}:${DOCUMENTDB_PORT}"

if [ -n "${TUNNEL_IDENTITY_FILE:-}" ]; then
    set -- "$@" -i "$TUNNEL_IDENTITY_FILE"
fi

if [ -n "${TUNNEL_SSH_PORT:-}" ]; then
    set -- "$@" -p "$TUNNEL_SSH_PORT"
fi

exec "$@" "${TUNNEL_USER}@${TUNNEL_HOST}"
