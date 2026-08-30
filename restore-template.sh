#!/bin/bash
set -euo pipefail

SNAPSHOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APPDATA_DST="/DATA/AppData"
COMPOSE_DST="/var/lib/casaos/apps"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "ERREUR : lance ce script en root (sudo)."
  exit 1
fi

command -v docker >/dev/null || { echo "ERREUR : docker introuvable"; exit 1; }
command -v rsync >/dev/null || { echo "ERREUR : rsync introuvable"; exit 1; }

if [ ! -d "$SNAPSHOT_DIR/containers" ]; then
  echo "ERREUR : dossier containers introuvable."
  exit 1
fi

read -r -p "Restaurer cette sauvegarde sur CE ZimaOS ? Tape RESTAURER : " ANSWER
[ "$ANSWER" = "RESTAURER" ] || { echo "Annulé."; exit 0; }

RUNNING_FILE="/tmp/zdbm-running-containers.txt"
docker ps -q > "$RUNNING_FILE" || true

cleanup() {
  if [ -s "$RUNNING_FILE" ]; then
    echo "Redémarrage des conteneurs qui étaient actifs..."
    xargs -r docker start < "$RUNNING_FILE" >/dev/null 2>&1 || true
  fi
  rm -f "$RUNNING_FILE"
}
trap cleanup EXIT

if [ -s "$RUNNING_FILE" ]; then
  echo "Arrêt propre des conteneurs..."
  xargs -r docker stop -t 30 < "$RUNNING_FILE" >/dev/null
fi

mkdir -p "$APPDATA_DST" "$COMPOSE_DST"

for CDIR in "$SNAPSHOT_DIR"/containers/*; do
  [ -d "$CDIR" ] || continue
  CNAME="$(basename "$CDIR")"
  echo
  echo "===== $CNAME ====="

  if [ -d "$CDIR/appdata" ]; then
    echo "Restauration AppData..."
    rsync -aHAX --numeric-ids "$CDIR/appdata/" "$APPDATA_DST/"
  fi

  if [ -d "$CDIR/compose/zimaos" ]; then
    echo "Restauration Compose ZimaOS..."
    rsync -aHAX --numeric-ids "$CDIR/compose/zimaos/" "$COMPOSE_DST/"
  fi
done

if [ -d "$SNAPSHOT_DIR/shared-volumes" ]; then
  echo
  echo "===== VOLUMES DOCKER ====="
  for VOLDIR in "$SNAPSHOT_DIR"/shared-volumes/*; do
    [ -d "$VOLDIR/_data" ] || continue
    VOLNAME="$(basename "$VOLDIR")"
    echo "Volume : $VOLNAME"
    docker volume inspect "$VOLNAME" >/dev/null 2>&1 || docker volume create "$VOLNAME" >/dev/null
    MOUNTPOINT="$(docker volume inspect -f '{{ .Mountpoint }}' "$VOLNAME")"
    mkdir -p "$MOUNTPOINT"
    rsync -aHAX --numeric-ids "$VOLDIR/_data/" "$MOUNTPOINT/"
  done
fi

echo
echo "Données restaurées."
echo "Les images Docker seront retéléchargées lors de la reconstruction."
echo
echo "Étape suivante :"
echo "  sudo $SNAPSHOT_DIR/rebuild-stacks.sh"
echo
echo "Vérifie avant que les montages /media, /mnt et NAS existent aux mêmes chemins."
