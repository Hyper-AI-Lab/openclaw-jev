# Slack Socket Mode — this host only

Readiness check `slack_sockets` (`app/production/readiness.py`) counts **`openclaw-gateway` PIDs on this VPS** (systemd MainPID plus `/proc/*/comm`). Pass if exactly one. Warn if zero or more than one.

This check cannot see Slack sockets on **other machines**. Another host with the same Slack app tokens can still hold a Socket Mode connection. That is undetectable from this VPS.

## Never call `apps.connections.open`

Do **not** use Slack `apps.connections.open` (or any API that opens another socket) to “inspect” or “fix” sockets. That would **open another connection**, which is the dual-socket failure mode.

## If this host has zero or two+ gateways

1. `systemctl status openclaw-gateway`
2. `pgrep -a openclaw-gateway`
3. If two local processes: stop extras; keep one `openclaw-gateway` unit. Restart only when `count_active_user_tasks_sync()==0` (`ops/controlled_capability_restart.sh --gateway`).
4. If Slack still looks dual after a single local PID: look at **other hosts / leftover laptops**, not this check.

## Related

- Binding: Slack DMs are RMP-owned (plugin claim → `POST /tasks`). No native OpenClaw Slack.
- Dual-write plugin copies: `rmp/plugins/rmp_adapter` and `/root/.openclaw/plugins/rmp_adapter`.
