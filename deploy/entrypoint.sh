#!/bin/sh
# Donne le dossier de données à l'utilisateur de l'application (Docker crée ./data en root),
# puis lance la commande sans privilèges root.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/data}"
    chown -R flotte:flotte "${DATA_DIR:-/data}"
    exec setpriv --reuid=flotte --regid=flotte --init-groups "$@"
fi
exec "$@"
