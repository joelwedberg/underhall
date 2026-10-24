#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[[ "$EUID" -eq 0 ]] || { echo "Kör sudo bash scripts/deploy.sh"; exit 1; }
owner="$(stat -c %U "$ROOT")"
g() { runuser -u "$owner" -- git -C "$ROOT" "$@"; }
[[ "$owner" != root && "$(g branch --show-current)" == dev/jdps-deploy && -z "$(g status --porcelain)" ]] || { echo "Fel ägare, branch eller lokala ändringar"; exit 1; }
[[ -s .env ]] || { echo "Saknar lokal .env"; exit 1; }
exec 9>/run/lock/underhall-jdps.lock
flock -n 9 || exit 1
dc() { docker compose -p underhall -f docker-compose.yml "$@"; }
dc config -q
mkdir -p .jdps/backups
chmod 700 .jdps .jdps/backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 ".jdps/backups/$stamp"
cp -a .env ".jdps/backups/$stamp/.env"
g rev-parse HEAD > ".jdps/backups/$stamp/commit.txt"
# Existerande nya Underhall-databas säkerhetskopieras konsistent.
if dc ps --status running --services | grep -qx db; then
  dc exec -T db sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ".jdps/backups/$stamp/underhall.dump"
fi
dc up -d --build --wait --wait-timeout 180
[[ "$(docker inspect -f '{{.State.Health.Status}}' underhall-app-1)" == healthy ]] || { echo "App ej healthy"; exit 1; }
echo "DEPLOY OK: underhall healthy, commit $(g rev-parse --short HEAD)"
