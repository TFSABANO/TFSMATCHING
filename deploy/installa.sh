#!/usr/bin/env bash
# Installazione su un server Ubuntu/Debian nuovo.
# Uso:  sudo bash deploy/installa.sh riconcilia.tfsabano.com
set -euo pipefail

DOMINIO="${1:?Indicare il sottodominio, es: sudo bash deploy/installa.sh riconcilia.tfsabano.com}"
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  echo ">> Installo Docker"
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
  echo ">> Creo .env con chiavi casuali"
  rnd() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
  FERNET=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')
  cat > .env <<ENV
DOMINIO=${DOMINIO}
DJANGO_SECRET_KEY=$(rnd 60)
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=${DOMINIO}
DJANGO_CSRF_TRUSTED_ORIGINS=https://${DOMINIO}
FIELD_ENCRYPTION_KEY=${FERNET}
POSTGRES_DB=riconcilia
POSTGRES_USER=riconcilia
POSTGRES_PASSWORD=$(rnd 32)
POSTGRES_HOST=db
ENV
  chmod 600 .env
  echo "   IMPORTANTE: conserva una copia di .env in un posto sicuro (contiene le chiavi di cifratura)."
fi

mkdir -p backup
echo ">> Avvio i servizi"
docker compose up -d --build

echo ">> Attendo l'avvio dell'applicazione"
for i in $(seq 1 60); do
  docker compose exec -T web python manage.py showmigrations core >/dev/null 2>&1 && break
  sleep 3
done

echo
echo ">> Crea ora l'utente amministratore:"
docker compose exec web python manage.py createsuperuser

echo
echo "Fatto. Apri https://${DOMINIO}/admin/ per creare l'azienda e gli utenti, poi https://${DOMINIO}/"
