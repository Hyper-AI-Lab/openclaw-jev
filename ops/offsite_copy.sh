#!/usr/bin/env bash
# The off-host copy of one backup of ops/backup.sh: restic copies the directory into its repository, encrypted on this
# host, then forgets all but the newest copies this host made, complete or not (RMP_OFFSITE_KEEP, 14), and prunes what
# only those held. backup.sh copies a failed run too, so its copy counts here, while backup.sh's local KEEP=14 counts
# complete backups only. It groups by host: restic's default grouping by path would make each night's directory a
# group of its own, and nothing would ever be forgotten.
#
#   bash ops/offsite_copy.sh <backup dir>
#
# The repository and its password come only from the environment: RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE and the
# backend's variables, which ops/backup.sh reads from the root-only /etc/rmp/restic.env in a subshell. Nothing here
# prints them or puts them on a command line.
#
# Switched off on this host, an accepted exception (K-7): backups stay on this host by Kirill's decision, and
# ops/backup.sh runs this only when settings' backup.offsite.enabled is true (docs/runbooks/backup-restore.md,
# "Off-host copy (accepted exception K-7)"). The restore drill (ops/restore_drill.py) runs it against a scratch
# repository on each drilled backup.
set -euo pipefail

dir="${1:?usage: offsite_copy.sh <backup dir>}"
host="$(hostname)"
# restic asks for a password on a terminal, or reads one from stdin, when the environment gives none: here it reads
# nothing, so it fails instead.
exec </dev/null
restic backup --quiet --tag rmp-backup --host "${host}" "${dir}"
restic forget --quiet --tag rmp-backup --host "${host}" --group-by host --keep-last "${RMP_OFFSITE_KEEP:-14}" --prune
