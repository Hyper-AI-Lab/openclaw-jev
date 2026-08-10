# Phase 7 — Communication & Reliability Hardening

**Goal:** Fix timezone/greeting errors, reconciler false-positive stuck repairs, and ops alert noise. Production-grade, no placeholders.

| Step | Deliverable | Verify |
|------|-------------|--------|
| H1 | `openclaw.json` → `agents.defaults.userTimezone: Asia/Tokyo` | Gateway restart; JSONL shows JST |
| H2 | User local time block in `prompt_policy.build_generic_execute_prompt` | Unit test |
| H3 | `touch_task_liveness` during OpenClaw JSONL poll | Task updated_at refreshes |
| H4 | `STUCK_REPAIR_MINUTES` 45; skip internal/smoke tasks (existing) | Reconciler tests |
| H5 | `catalog_task.py` use `payload.rework_max_attempts` not `get_rework_max_attempts()` | No sandbox crash |
| H6 | Conversational evidence: skip strict length for greeting intents | Unit test |
| H7 | Run memory canary; clear stale sentinel alert | last_memory_canary completed |
| H8 | Full pytest + production-check + restart | All pass |
| H9 | Append progress log | VISION_COMPLETION_PROGRESS.md |

**Execution rule:** One step, one solution, append-only log. No plan edits mid-flight.
