#!/usr/bin/env bash
# GitHub for Claude in Aura's direct sessions, installed as /usr/local/bin/aura-github. The token is read
# only inside the command that needs it: never printed, never in a remote URL, git config or env file.
#
#   aura-github push [branch]   push a branch of this clone of Aura's repository (never main)
#   aura-github gh <args...>    gh on Aura's repository, e.g. pr create --fill, pr view, pr checks (never pr merge)
#
# Aura merges and deploys a pull request with her deploy_pr tool. Other repositories need Kirill's go-ahead.
set -euo pipefail
REPO="Hyper-AI-Lab/openclaw-jev"
TOKEN_FILE="${AURA_GITHUB_TOKEN_FILE:-/root/.config/github_pat}"

die() { echo "aura-github: $*" >&2; exit 2; }

case "${1:-}" in
  push)
    origin="$(git config --get remote.origin.url 2>/dev/null || true)"
    [[ "${origin%.git}" == "https://github.com/${REPO}" ]] \
      || die "this clone's origin is not ${REPO}; other repositories need Kirill's go-ahead first"
    branch="${2:-$(git rev-parse --abbrev-ref HEAD)}"
    [[ "${branch}" != "main" && "${branch}" != "HEAD" ]] \
      || die "never push main: push a branch, open a pull request, and Aura deploys it with deploy_pr"
    askpass="$(mktemp)"
    trap 'rm -f "${askpass}"' EXIT
    printf '#!/bin/sh\ncase "$1" in *Username*) echo x-access-token ;; *) tr -d "\\n\\r" < "%s" ;; esac\n' \
      "${TOKEN_FILE}" > "${askpass}"
    chmod 700 "${askpass}"
    GIT_ASKPASS="${askpass}" GIT_TERMINAL_PROMPT=0 git push --set-upstream origin "${branch}:refs/heads/${branch}"
    ;;
  gh)
    shift
    [[ "${1:-}" != "pr" || "${2:-}" != "merge" ]] \
      || die "never merge here: Aura merges and deploys a pull request with deploy_pr, once CI's test check passed"
    GH_TOKEN="$(tr -d '\n\r' < "${TOKEN_FILE}")" GH_REPO="${REPO}" exec gh "$@"
    ;;
  *)
    die "usage: aura-github push [branch] | aura-github gh <args...>"
    ;;
esac
