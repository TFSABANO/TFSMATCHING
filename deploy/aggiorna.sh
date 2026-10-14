#!/usr/bin/env bash
# Aggiorna l'applicazione all'ultima versione su GitHub.  Uso: sudo bash deploy/aggiorna.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull
docker compose up -d --build
docker image prune -f >/dev/null
echo "Aggiornato."
