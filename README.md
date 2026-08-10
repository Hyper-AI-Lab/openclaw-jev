# Aura — Reliability & Memory Plane (RMP)

Public snapshot of the **Aura** control plane that runs on top of [OpenClaw](https://github.com/openclaw/openclaw): Temporal workflows, FastAPI, Postgres, Qdrant memory, and the Slack intake path.

Org: [Hyper-AI-Lab](https://github.com/Hyper-AI-Lab) · Repo: [aura](https://github.com/Hyper-AI-Lab/aura)

## What’s in this repo

| Path | Role |
|------|------|
| `app/` | FastAPI API, Temporal workflows/activities, intake, memory, LLM quota broker |
| `ops/` | Canaries, backup, janitor, patch verify, healthchecks |
| `tests/` | Pytest suite |
| `plugins/rmp_adapter/` | OpenClaw plugin (Slack → RMP routing) |
| `patch_openclaw.sh` | Re-apply RMP dist patches after OpenClaw upgrades |
| `ARCHITECTURE.md` | System architecture |
| `settings.example.json` | Example config (**no secrets**) |

## Not included (on purpose)

- Live `settings.json`, API keys, NVIDIA/Slack tokens  
- `data/` (Postgres dumps, Qdrant, Temporal DB, usage ledgers)  
- `venv/`, session transcripts, backups  

## Quick start (high level)

1. Install OpenClaw + Node ≥ 22.23, Postgres, Temporal, Qdrant.  
2. Copy `settings.example.json` → `settings.json` and set `api_key` + production fields.  
3. Place NVIDIA keys in your OpenClaw env; run `ops/sync_nvidia_keys.py`.  
4. Install/link `plugins/rmp_adapter` into OpenClaw plugins; run `bash patch_openclaw.sh`.  
5. Start `rmp-api`, `rmp-worker`, `openclaw-gateway`, Temporal.  
6. `make production-check`

## License

All rights reserved unless otherwise noted by Hyper-AI-Lab.
