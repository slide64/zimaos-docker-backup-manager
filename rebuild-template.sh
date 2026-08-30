#!/bin/bash
set -u

SNAPSHOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLAN="$SNAPSHOT_DIR/rebuild-plan.tsv"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "ERREUR : lance ce script en root (sudo)."
  exit 1
fi

command -v docker >/dev/null || { echo "ERREUR : docker introuvable"; exit 1; }

if [ ! -f "$PLAN" ]; then
  echo "ERREUR : plan de reconstruction introuvable : $PLAN"
  exit 1
fi

echo "Ce script relance uniquement les conteneurs sélectionnés lors de la sauvegarde."
echo "Les chemins /media, /mnt et autres bind mounts doivent déjà exister."
read -r -p "Lancer la reconstruction ? Tape RELANCER : " ANSWER
[ "$ANSWER" = "RELANCER" ] || { echo "Annulé."; exit 0; }

FAIL=0

while IFS=$'\t' read -r COMPOSE SERVICE CONTAINER; do
  [ -n "$CONTAINER" ] || continue
  [ "$CONTAINER" = "container" ] && continue

  echo
  echo "===== $CONTAINER ====="

  if [ -z "$COMPOSE" ]; then
    echo "IGNORE : aucun Compose trouvé. Voir containers/$CONTAINER/container-inspect.json."
    continue
  fi

  echo "Compose : $COMPOSE"
  [ -n "$SERVICE" ] && echo "Service : $SERVICE"

  if [ ! -f "$COMPOSE" ]; then
    echo "ECHEC : fichier Compose introuvable"
    FAIL=1
    continue
  fi

  if ! docker compose -f "$COMPOSE" config >/dev/null 2>&1; then
    echo "ECHEC : Compose invalide ou dépendances manquantes"
    FAIL=1
    continue
  fi

  if [ -n "$SERVICE" ]; then
    docker compose -f "$COMPOSE" up -d "$SERVICE" || { echo "ECHEC : $CONTAINER"; FAIL=1; }
  else
    docker compose -f "$COMPOSE" up -d || { echo "ECHEC : $CONTAINER"; FAIL=1; }
  fi
done < "$PLAN"

echo
echo "Reconstruction terminée. Vérifie avec : docker ps -a"
exit "$FAIL"
