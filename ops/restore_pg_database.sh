#!/usr/bin/env bash
# Restore the one database a `pg_dump -Fc` archive of ops/backup.sh holds, as an exact copy, through the postgres
# superuser's peer login. pg_restore drops the database the archive names, recreates it from the archive's own
# definition (owner, encoding, locale, settings) and restores into it, so nothing the dump lacks survives. It runs as
# postgres because the rmp role has neither CREATEDB nor superuser, and rmp_db's `vector` extension needs a superuser.
#
#   bash ops/restore_pg_database.sh <backup-dir>/rmp_db.dump
#
# --exit-on-error stops at the first error: a DROP DATABASE refused because a session is still connected changes
# nothing. The archive reaches pg_restore on stdin, opened here by root, because postgres cannot read the root-only
# backups. RMP_PG_SOCKET_DIR and RMP_PG_PORT point it at a scratch server for the restore drill; the restore never sets
# them.
set -euo pipefail

dump="${1:?usage: restore_pg_database.sh <dump>}"
[[ -s "${dump}" ]] || { echo "restore_pg_database: ${dump} is missing or empty" >&2; exit 1; }
sudo -n -u postgres pg_restore -h "${RMP_PG_SOCKET_DIR:-/var/run/postgresql}" -p "${RMP_PG_PORT:-5432}" \
  --clean --if-exists --create --exit-on-error -d postgres < "${dump}"
