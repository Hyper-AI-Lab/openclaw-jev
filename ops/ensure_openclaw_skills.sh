#!/usr/bin/env bash
# Ensure workspace OpenClaw skills are visible from npm global skills path.
set -euo pipefail

OPENCLAW_NPM="/usr/lib/node_modules/openclaw"
WORKSPACE_SKILLS="/root/.openclaw/skills"
TARGET_DIR="${OPENCLAW_NPM}/skills"

if [[ ! -d "$OPENCLAW_NPM" ]]; then
  echo "WARN: OpenClaw npm install not found at ${OPENCLAW_NPM}"
  exit 0
fi

mkdir -p "$TARGET_DIR"

if [[ ! -d "$WORKSPACE_SKILLS" ]]; then
  echo "WARN: workspace skills dir missing: ${WORKSPACE_SKILLS}"
  exit 0
fi

linked=0
for skill_dir in "$WORKSPACE_SKILLS"/*; do
  [[ -d "$skill_dir" ]] || continue
  name="$(basename "$skill_dir")"
  link_path="${TARGET_DIR}/${name}"
  if [[ -L "$link_path" ]] && [[ "$(readlink -f "$link_path")" == "$(readlink -f "$skill_dir")" ]]; then
    continue
  fi
  ln -sfn "$skill_dir" "$link_path"
  linked=$((linked + 1))
  echo "Linked skill: ${name}"
done

if [[ -f "${TARGET_DIR}/moltmarket/SKILL.md" ]]; then
  echo "OK: moltmarket SKILL.md resolvable at ${TARGET_DIR}/moltmarket/SKILL.md"
else
  echo "FAIL: moltmarket SKILL.md not found under ${TARGET_DIR}"
  exit 1
fi

echo "ensure_openclaw_skills complete (linked=${linked})"
