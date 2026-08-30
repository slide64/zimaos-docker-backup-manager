#!/bin/bash
set -euo pipefail

APP_DIR="/DATA/AppData/zimaos-docker-backup-manager"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "ERREUR : lance avec sudo ou en root."
  exit 1
fi

command -v docker >/dev/null || { echo "ERREUR : Docker n'est pas installé."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERREUR : docker compose n'est pas disponible."; exit 1; }

DOCKER_ROOT_DIR="$(docker info --format '{{.DockerRootDir}}')"

echo "Installation dans $APP_DIR"
mkdir -p "$APP_DIR"

# Copie les sources sans écraser les données persistantes.
rsync -a --delete \
  --exclude '/data/' \
  --exclude '/.env' \
  "$SCRIPT_DIR/" "$APP_DIR/"

mkdir -p "$APP_DIR/data" "$APP_DIR/.docker" "$APP_DIR/.home"

# ZimaOS monte /root en lecture seule. Docker Buildx/Compose essaie sinon
# de créer /root/.docker pendant un build.
export HOME="$APP_DIR/.home"
export DOCKER_CONFIG="$APP_DIR/.docker"

# Conserve le port existant lors d'une mise à jour.
APP_PORT="9877"
if [ -f "$APP_DIR/.env" ]; then
  OLD_PORT="$(awk -F= '$1=="APP_PORT" {print $2; exit}' "$APP_DIR/.env" 2>/dev/null || true)"
  if [ -n "$OLD_PORT" ]; then
    APP_PORT="$OLD_PORT"
  fi
fi

cat > "$APP_DIR/.env" <<ENVEOF
DOCKER_ROOT_DIR=$DOCKER_ROOT_DIR
APP_PORT=$APP_PORT
ENVEOF

cd "$APP_DIR"
docker compose up -d --build

echo
echo "=============================================="
echo " ZimaOS Docker Backup Manager installé"
echo "=============================================="
# Pas de détection automatique de l'IP. Elle n'est pas nécessaire au fonctionnement.
echo "Interface : http://IP_DU_ZIMAOS:$APP_PORT"
echo "Répertoire : $APP_DIR"
echo "Docker root : $DOCKER_ROOT_DIR"
