#!/usr/bin/env bash
# Restore RMP from a backup directory made by ops/backup.sh: the RMP and Temporal databases, Qdrant, artifacts,
# OpenClaw's stores and settings. Runbook: docs/runbooks/backup-restore.md. Exits 1, naming what was not restored, if
# any part fails.
#
# Nothing may write to a store while it is replaced. The restore holds the code-reload lock, which a deploy and a code
# reload take too, refuses while a deploy unit is active, and stops every writer first: the RMP timers and their runs,
# rmp-code-watch, the gateway, rmp-api, rmp-worker and Temporal. rmp-qdrant keeps running for the Qdrant restore.
# It starts what it stopped when every part was restored, or when it ends before its first database step. When a part
# failed, or a signal ended it after that step and before its last, it starts nothing: rmp-api runs create_all on rmp_db
# at start, so the operator starts the units once the stores are whole.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /root/.openclaw/rmp/data/backups/YYYYMMDDTHHMMSSZ"
  exit 1
fi

SRC="$1"
# The names and defaults of ops/backup.sh and ops/reload_runtime_on_code_change.sh. Production sets none of them; the
# tests point them at a tmp tree.
RMP_ROOT="${RMP_ROOT:-/root/.openclaw/rmp}"
RMP_DATA="${RMP_DATA_DIR:-${RMP_ROOT}/data}"
PY="${RMP_PYTHON:-${RMP_ROOT}/venv/bin/python}"
SETTINGS="${RMP_SETTINGS_PATH:-${RMP_ROOT}/settings.json}"
LOCK_FILE="${RMP_CODE_RELOAD_LOCK:-/run/rmp-code-reload.lock}"
FAILED=()
TIMERS=()               # the active rmp-*.timer units, recorded before anything stops
declare -A TRIGGERS=()  # the service each recorded timer triggers
declare -A STOPPED=()   # every unit this restore stopped
RUNNING=()              # of rmp-code-watch and openclaw-gateway, those that were running
CHANGED=""              # set before the first database step: from then on a failed part leaves every unit stopped
DONE=""                 # set after the last step: an exit before it, by an error or any signal, is a failure

if [[ ! -d "$SRC" ]]; then
  echo "Backup directory not found: $SRC"
  exit 1
fi
if [[ -f "${SRC}/manifest.json" ]]; then
  failures="$("${PY}" -c 'import json, sys; print(" ".join(json.load(open(sys.argv[1])).get("failures") or []))' "${SRC}/manifest.json")"
  [[ -z "${failures}" ]] || echo "WARNING: this backup recorded failures when it was made: ${failures}"
fi

echo "=== RMP Restore from $SRC ==="
read -r -p "This will overwrite live data. Continue? [y/N] " confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || exit 0

# Held until the restore exits: a deploy waits for it, a code reload skips.
exec 8>"${LOCK_FILE}"
if ! flock -n 8; then
  echo "refused: a deploy or code reload holds ${LOCK_FILE}; run the restore again when it ends" >&2
  exit 1
fi

# The first active or activating deploy unit. Once it has the lock, a deploy restarts rmp-api and rmp-worker, so none
# may outlive the restore. Only rmp-worker's coding activity and rmp-api's reconciler launch one.
deploy_unit() {
  local listed
  listed="$(systemctl list-units --plain --no-legend --state=active,activating 'aura-deploy-*')" || return 1
  awk 'NF {print $1; exit}' <<<"${listed}"
}
unit="$(deploy_unit)" || { echo "refused: systemctl could not list the deploy units" >&2; exit 1; }
if [[ -n "${unit}" ]]; then
  echo "refused: ${unit} is active; run the restore again when it ends" >&2
  exit 1
fi

# Active, or activating: starting, or waiting to restart.
running() {
  case "$(systemctl is-active "$1" || true)" in
    active|activating) return 0 ;;
    *) return 1 ;;
  esac
}

listed="$(systemctl list-units --type=timer --state=active --plain --no-legend 'rmp-*.timer')" \
  || { echo "refused: systemctl could not list the RMP timers" >&2; exit 1; }
mapfile -t TIMERS < <(awk 'NF {print $1}' <<<"${listed}")
for unit in rmp-code-watch openclaw-gateway; do
  if running "${unit}"; then
    RUNNING+=("${unit}")
  fi
done

# WP02-19's timer check: "next: <timer>" for an armed RMP timer, "NO NEXT: <timer>" for one without a next elapse.
timer_check() {
  systemctl list-timers 'rmp-*' --all --no-pager --no-legend \
    | awk '{print ($1 == "-" ? "NO NEXT: " : "next: ") $(NF-1)}'
}

# Every RMP timer must be armed again. A timer started after its OnUnitActiveSec= passed, or after a missed
# OnCalendar= elapse (Persistent=true), runs its service at once and shows no next elapse until that run ends: wait for
# the run (every 10 s, 15 min at most), then check once more.
check_timers() {
  local lines timer service waits line status=0
  local -A overran=()
  lines="$(timer_check)" || { echo "the timer check failed: systemctl list-timers" >&2; return 1; }
  if grep -q '^NO NEXT: ' <<<"${lines}"; then
    for timer in $(sed -n 's/^NO NEXT: //p' <<<"${lines}"); do
      service="${TRIGGERS[${timer}]:-}"
      [[ -n "${service}" ]] || continue
      waits=0
      while running "${service}"; do
        if (( waits == 90 )); then
          overran[${timer}]=1
          break
        fi
        sleep 10
        waits=$((waits + 1))
      done
    done
    lines="$(timer_check)" || { echo "the timer check failed: systemctl list-timers" >&2; return 1; }
  fi
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    if [[ "${line}" == "NO NEXT: "* ]]; then
      status=1
      timer="${line#NO NEXT: }"
      [[ -z "${overran[${timer}]:-}" ]] || line+=" (${TRIGGERS[${timer}]} still running after 15 min)"
    fi
    echo "${line}"
  done <<<"${lines}"
  if (( status )); then
    echo "A timer has no next elapse: wait for its run to end, or re-arm it (docs/runbooks/backup-restore.md)" >&2
  fi
  return "${status}"
}

# Before the first database step, or with every part restored: start what the restore stopped, in dependency order,
# and check the timers. After a failed part: start nothing, and say what is left stopped.
finish() {
  local rc=$? unit
  # Nothing may end this before it starts what the restore stopped: not a write to a terminal that hung up (EIO) or to
  # a `| tee` that died (SIGPIPE), nor the second HUP of a hang-up, which the kernel sends when the login shell exits.
  set +e
  trap '' PIPE HUP
  local start=() not_started=()
  # Bash runs this trap with status 0 after a signal it does not trap: an exit before the last step fails either way.
  [[ -n "${DONE}" ]] || (( rc )) || rc=1
  for unit in temporal rmp-api rmp-worker openclaw-gateway rmp-code-watch "${TIMERS[@]}"; do
    [[ -z "${STOPPED[${unit}]:-}" ]] || start+=("${unit}")
  done
  if [[ -n "${CHANGED}" ]] && (( rc != 0 || ${#FAILED[@]} )); then
    if (( ${#FAILED[@]} )); then
      echo "Restore INCOMPLETE: not restored: ${FAILED[*]}" >&2
    fi
    if (( rc != 0 )); then
      echo "Restore INCOMPLETE: it ended before its last step (exit ${rc})" >&2
    fi
    echo "Left stopped, because a store may be half restored: ${start[*]}" >&2
    echo "When the stores are whole, start them with: systemctl start ${start[*]}" >&2
    echo "Do not start rmp-api, rmp-worker or openclaw-gateway, run a deploy or reboot before the stores are whole: each starts rmp-api, which runs create_all on rmp_db" >&2
    exit 1
  fi
  (( ${#start[@]} )) || exit "${rc}"
  echo "Starting what the restore stopped: ${start[*]}"
  for unit in "${start[@]}"; do
    systemctl start "${unit}" || not_started+=("${unit}")
  done
  if (( ${#not_started[@]} )); then
    echo "could not start: ${not_started[*]}" >&2
    rc=1
  fi
  check_timers || rc=1
  (( rc )) || echo "Restore complete."
  exit "${rc}"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Recorded first: a signal during the stop runs the trap before the next command, and the unit may be stopped by then.
stop() {
  STOPPED[$1]=1
  systemctl stop "$1" || { unset "STOPPED[$1]"; echo "systemctl stop $1 failed: nothing was restored" >&2; exit 1; }
}
# Each timer, then the service it triggers: stopping an inactive oneshot does nothing, and a running canary, janitor,
# watchdog or backup run ends. Then the code watch and the gateway, then RMP and Temporal.
for timer in "${TIMERS[@]}"; do
  service="$(systemctl show -p Triggers --value "${timer}")" && [[ -n "${service}" ]] \
    || { echo "systemctl could not name the service ${timer} triggers: nothing was restored" >&2; exit 1; }
  TRIGGERS[${timer}]="${service}"
  stop "${timer}"
  stop "${service}"
done
for unit in "${RUNNING[@]}" rmp-api rmp-worker temporal; do
  stop "${unit}"
done

# A deploy unit that rmp-api or rmp-worker launched since the first check is now waiting for the lock. With both
# stopped, nothing launches another until an operator starts one of them.
unit="$(deploy_unit)" || { echo "systemctl could not list the deploy units: nothing was restored" >&2; exit 1; }
if [[ -n "${unit}" ]]; then
  echo "refused: ${unit} started while the restore stopped the services; run the restore again when it ends" >&2
  exit 1
fi

CHANGED=1
# RMP's ledger, then Temporal's own databases. Each is dropped and recreated as an exact copy of its archive through
# the postgres superuser: the rmp role cannot create a database. A failure is named as in the backup's manifest.
declare -A PART=([rmp_db]=postgres [temporal]=temporal:temporal [temporal_visibility]=temporal:temporal_visibility)
for db in rmp_db temporal temporal_visibility; do
  if [[ -s "${SRC}/${db}.dump" ]]; then
    echo "Restoring the ${db} database..."
    bash "${RMP_ROOT}/ops/restore_pg_database.sh" "${SRC}/${db}.dump" || FAILED+=("${PART[${db}]}")
  fi
done

# Qdrant: a full snapshot restores collection by collection into the running server.
if [[ -f "${SRC}/qdrant-full.snapshot" ]]; then
  echo "Restoring Qdrant collections..."
  bash "${RMP_ROOT}/ops/restore_qdrant_snapshot.sh" "${SRC}/qdrant-full.snapshot" || FAILED+=("qdrant")
elif [[ -f "${SRC}/qdrant.tar.gz" ]]; then
  # Backups made before 2026-10-08 carry a tar of the legacy embedded directory, which was empty in server mode:
  # it does not restore the Qdrant server (data/qdrant-server). Only embedded-mode backups hold real data in it.
  echo "WARNING: qdrant.tar.gz restores the embedded store only; it does not restore a Qdrant server."
  echo "Restoring embedded Qdrant data..."
  { rm -rf "${RMP_DATA}/qdrant" && tar -xzf "${SRC}/qdrant.tar.gz" -C "${RMP_DATA}"; } || FAILED+=("qdrant")
fi

if [[ -f "${SRC}/artifacts.tar.gz" ]]; then
  echo "Restoring artifacts..."
  { rm -rf "${RMP_DATA}/artifacts" && tar -xzf "${SRC}/artifacts.tar.gz" -C "${RMP_DATA}"; } || FAILED+=("artifacts")
fi

# OpenClaw's stores (sessions, transcripts, auth profiles, cron jobs), with the gateway stopped above. The tool checks
# every copy's checksum and integrity before it replaces anything, and refuses while the gateway runs.
if [[ -f "${SRC}/openclaw-state/manifest.json" ]]; then
  echo "Restoring OpenClaw's stores..."
  "${PY}" "${RMP_ROOT}/ops/backup_openclaw_state.py" restore "${SRC}/openclaw-state" --yes || FAILED+=("openclaw-state")
fi

if [[ -f "${SRC}/settings.json" ]]; then
  cp -a "${SRC}/settings.json" "${SETTINGS}" || FAILED+=("settings")
fi
DONE=1
# finish, the EXIT trap, starts what the restore stopped, or reports what was not restored.
