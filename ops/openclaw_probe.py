"""Probe an OpenClaw gateway the way RMP uses it: staging before an upgrade, production after.

    venv/bin/python ops/openclaw_probe.py --target staging [--boot-seconds S] [--out FILE]
    venv/bin/python ops/openclaw_probe.py --target production [--skip abort,settle]

Each probe goes through RMP's own code paths:
- health: ``/healthz``, ``/startupz`` and ``/readyz`` (agent databases admitted after migration);
- plugins: every configured plugin loads, RMP's own included;
- hook_run: a ``/hooks/agent`` run with RMP's payload and dispatch marker, its reply read back
  through RMP's transcript reader and reply poller, and whether the marker turn was compressed;
- bootstrap: an RMP session gets no persona files (RMP_MINIMAL_BOOTSTRAP);
- session_entry: RMP's ``patch_session_entry`` leaves the row pending (entry_valid 0), and runs
  on that session and a new one still work (RMP_SESSION_PENDING_OK);
- settle: ``settle_openclaw_sessions.py`` on the migrated schema (staging only);
- abort: ``sessions.abort`` through RMP's SDK helper stops a long run;
- history: sessions from before the migration read back identically through RMP's reader;
- patched: the running dist passes the patch audit.

Probe sessions are ``agent:main:rmp_task_probe-<hex>``: RMP-owned for OpenClaw's hooks and RMP's
patches, never a task id RMP knows. Nothing is delivered (``deliver: false``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import openclaw_sessions as osess  # noqa: E402
from app.activities.openclaw_activities import _poll_jsonl_for_response  # noqa: E402
from app.openclaw_transcripts import decode_event, event_columns, stores_compressed_events  # noqa: E402
from ops import openclaw_patch_audit  # noqa: E402

RMP_ROOT = Path(__file__).resolve().parent.parent
HELPER = RMP_ROOT / "app" / "openclaw_gateway.mjs"
PERSONA_FILES = {"AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"}
# Long enough that OpenClaw 2026.9.7 stores the marker turn compressed (1 KiB and more).
PADDING = "Context for this check, to be ignored: " + "lorem ipsum dolor sit amet " * 60


@dataclass
class Target:
    name: str
    url: str
    hooks_token: str
    state_dir: Path
    package_dir: Path
    node: Path
    unit: str
    helper_env: Dict[str, str] = field(default_factory=dict)
    cli: Callable[[Sequence[str]], subprocess.CompletedProcess] = None  # type: ignore[assignment]

    @property
    def agent_db(self) -> Path:
        return self.state_dir / "agents" / "main" / "agent" / "openclaw-agent.sqlite"


def staging_target() -> Target:
    from ops import openclaw_staging as staging

    config = json.loads((staging.OC / "openclaw.json").read_text())
    return Target(
        name="staging", url=f"http://127.0.0.1:{config['gateway']['port']}", hooks_token=config["hooks"]["token"],
        state_dir=staging.OC, package_dir=staging.PACKAGE, node=staging.node_bin() / "node", unit=staging.UNIT,
        helper_env={**os.environ, **staging.env()},
        cli=lambda args: staging.staged(["openclaw", *args], timeout=600, check=False),
    )


def production_target() -> Target:
    home = Path("/root/.openclaw")
    config = json.loads((home / "openclaw.json").read_text())
    cli_path = Path(os.path.realpath(shutil.which("openclaw") or "/usr/bin/openclaw"))
    return Target(
        name="production", url=f"http://127.0.0.1:{config['gateway']['port']}", hooks_token=config["hooks"]["token"],
        state_dir=home, package_dir=cli_path.parent, node=Path(shutil.which("node") or "/usr/bin/node"),
        unit="openclaw-gateway", helper_env=dict(os.environ),
        cli=lambda args: subprocess.run(["openclaw", *args], capture_output=True, text=True, timeout=600),
    )


def use_store(target: Target) -> None:
    """Point RMP's session and transcript readers at the target's agent database."""
    osess.AGENT_DB_PATH = target.agent_db
    osess.SESSIONS_JSON_PATH = target.state_dir / "no-sessions.json"
    osess.SESSIONS_DIR = target.state_dir / "agents" / "main" / "sessions"


def probe_key() -> str:
    return f"agent:main:rmp_task_probe-{uuid.uuid4().hex[:12]}"


def dispatch(target: Target, key: str, text: str, thinking: str = "low") -> Dict[str, Any]:
    marker = f"[RMP_DISPATCH {uuid.uuid4().hex[:12]}]"
    payload = {"sessionKey": key, "message": f"{marker}\n{text}", "deliver": False,
               "allowUnsafeExternalContent": True, "sessionMode": "persistent", "thinking": thinking}
    start_ms = time.time() * 1000 - 5000
    resp = httpx.post(f"{target.url}/hooks/agent", json=payload, timeout=60,
                      headers={"Authorization": f"Bearer {target.hooks_token}"})
    resp.raise_for_status()
    return {"marker": marker, "start_ms": start_ms, "sent": time.time(), "response": resp.json()}


def wait_reply(key: str, sent: Dict[str, Any], expect: str, timeout: float = 300) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sid = osess.get_session_entry(key).get("sessionId")
        if sid:
            lines = osess.read_transcript_lines(sid)
            text, reason, _ = _poll_jsonl_for_response("", sent["start_ms"], lines, sent["marker"])
            if text and expect in text:
                return {"ok": True, "text": text[:200], "stop_reason": reason, "session_id": sid,
                        "seconds": round(time.time() - sent["sent"], 1)}
        time.sleep(2)
    return {"ok": False, "detail": f"no reply containing {expect!r} within {timeout:.0f} s"}


def marker_row_compressed(target: Target, session_id: str, marker: str) -> Optional[bool]:
    con = sqlite3.connect(f"file:{target.agent_db}?mode=ro", uri=True)
    try:
        rows = con.execute(f"SELECT {event_columns(con)} FROM transcript_events WHERE session_id = ?",
                           (session_id,)).fetchall()
    finally:
        con.close()
    for event_json, event_zstd, utf8_bytes in rows:
        text = decode_event(event_json, event_zstd, utf8_bytes)
        if text and marker in text:
            return event_zstd is not None
    return None


def prompt_report_snapshot(target: Target, key: str) -> Dict[str, Any]:
    """2026.9.7 keeps large entry fields such as systemPromptReport in session_entry_snapshots."""
    con = sqlite3.connect(f"file:{target.agent_db}?mode=ro", uri=True)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'session_entry_snapshots'").fetchone():
            return {}
        row = con.execute("SELECT value_json FROM session_entry_snapshots WHERE session_key = ? AND field = ?",
                          (key, "systemPromptReport")).fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    finally:
        con.close()


def entry_valid(target: Target, key: str) -> Optional[int]:
    con = sqlite3.connect(f"file:{target.agent_db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT entry_valid FROM session_nodes WHERE session_key = ?", (key,)).fetchone()
        return None if row is None else row[0]
    finally:
        con.close()


def turn(target: Target, key: str, *, pad: bool = True) -> Dict[str, Any]:
    nonce = uuid.uuid4().hex[:8]
    text = (PADDING + "\n\n" if pad else "") + f"Reply with exactly this and nothing else: PROBE_OK {nonce}"
    sent = dispatch(target, key, text)
    result = wait_reply(key, sent, f"PROBE_OK {nonce}")
    if result.get("ok"):
        result["marker_compressed"] = marker_row_compressed(target, result["session_id"], sent["marker"])
    return result


# ---- probes -------------------------------------------------------------------------------

def probe_health(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for path in ("/healthz", "/startupz", "/readyz"):
        resp = httpx.get(f"{target.url}{path}", timeout=10)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        out[path] = {"http": resp.status_code, **{k: body[k] for k in ("status", "version", "failing") if k in body}}
    ok = all(v["http"] == 200 for v in out.values())
    if ctx.get("boot_seconds") is not None:
        out["boot_seconds"] = ctx["boot_seconds"]
    return {"ok": ok, **out}


def gateway_log(target: Target) -> List[str]:
    """The gateway's journal since its current start."""
    since = subprocess.run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", target.unit],
                           capture_output=True, text=True).stdout.strip()
    run = subprocess.run(["journalctl", "-u", target.unit, "--since", since or "-1h", "--no-pager", "-o", "cat"],
                         capture_output=True, text=True, timeout=60)
    return run.stdout.splitlines()


def probe_plugins(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Loaded in the running gateway: no load failure, and every runtime plugin in its listening line.

    The CLI's own list loads plugins in its own process and missed 2026.9.7 refusing langsearch.
    Provider plugins (brave, openai, ...) never appear in the listening line.
    """
    config = json.loads((target.state_dir / "openclaw.json").read_text())
    entries = (config.get("plugins") or {}).get("entries", {})
    runtime = {Path(p).name for p in (config.get("plugins") or {}).get("load", {}).get("paths", [])}
    runtime |= {name for name, channel in (config.get("channels") or {}).items()
                if isinstance(channel, dict) and channel.get("enabled") and entries.get(name, {}).get("enabled", True)}
    lines = gateway_log(target)
    failed = sorted({m.group(1) for line in lines for m in [re.search(r"\[plugins\] (\S+) failed during load from", line)] if m})
    listening = [m for line in lines for m in [re.search(r"http server listening \(\d+ plugins: ([^;)]+)", line)] if m]
    loaded = {name.strip() for name in listening[-1].group(1).split(",")} if listening else set()
    missing = sorted(runtime - loaded)
    result = {"ok": bool(listening) and not failed and not missing, "gateway_loaded": sorted(loaded),
              "failed_during_load": failed, "runtime_missing": missing}
    result["cli"] = cli_plugin_status(target, entries)
    return result


def cli_plugin_status(target: Target, entries: Dict[str, Any]) -> Dict[str, Any]:
    wanted = sorted(k for k, v in entries.items() if v.get("enabled", True))
    run = target.cli(["plugins", "list", "--json"])
    try:
        listed = json.loads(run.stdout[run.stdout.index("{") if "{" in run.stdout else 0:] or "{}")
    except ValueError:
        return {"ok": False, "detail": f"plugins list did not return JSON (exit {run.returncode}): {run.stdout[-300:]}"}
    items = listed.get("plugins") if isinstance(listed, dict) else listed
    status = {}
    for item in items or []:
        pid = item.get("id") or item.get("name")
        status[pid] = item.get("status") or ("loaded" if item.get("loaded") else "enabled" if item.get("enabled") else "?")
    missing = [p for p in wanted if status.get(p) not in ("loaded", "active", "enabled", "ok")]
    return {"ok": not missing, "wanted": wanted, "status": {p: status.get(p) for p in wanted}, "not_loaded": missing}


def probe_hook_run(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    key = probe_key()
    result = turn(target, key)
    ctx["run_key"] = key
    expect_compressed = stores_compressed(target)
    if result.get("ok") and expect_compressed and result.get("marker_compressed") is not True:
        return {**result, "ok": False, "detail": "the marker turn was not stored compressed"}
    return {"key": key, **result}


def probe_bootstrap(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    key = ctx.get("run_key")
    if not key:
        return {"ok": False, "detail": "needs hook_run"}
    report = osess.get_session_entry(key).get("systemPromptReport") or prompt_report_snapshot(target, key)
    files = [f.get("name") if isinstance(f, dict) else f for f in report.get("injectedWorkspaceFiles") or []]
    persona = sorted(PERSONA_FILES & set(files))
    return {"ok": "injectedWorkspaceFiles" in report and not persona, "injected": files, "persona": persona}


def probe_session_entry(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    key = ctx.get("run_key")
    if not key:
        return {"ok": False, "detail": "needs hook_run"}
    patched = osess.patch_session_entry(key, {"rmpProbe": uuid.uuid4().hex[:8]})
    pending = entry_valid(target, key)
    again = turn(target, key, pad=False)
    fresh = turn(target, probe_key(), pad=False)
    return {"ok": bool(patched) and again.get("ok") and fresh.get("ok"), "patched": patched,
            "entry_valid_after_patch": pending, "same_session": again, "new_session": fresh,
            "entry_valid_now": entry_valid(target, key)}


def probe_settle(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    run = subprocess.run([str(RMP_ROOT / "venv" / "bin" / "python"), str(RMP_ROOT / "ops" / "settle_openclaw_sessions.py"),
                          str(target.agent_db)], capture_output=True, text=True, timeout=300)
    after = turn(target, probe_key(), pad=False)
    return {"ok": run.returncode == 0 and after.get("ok"), "exit": run.returncode,
            "output": run.stdout.strip()[-400:], "run_after": after}


def helper_call(target: Target, method: str, params: Dict[str, Any], timeout: float = 60) -> Dict[str, Any]:
    proc = subprocess.Popen([str(target.node), str(HELPER), str(target.package_dir)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=target.helper_env)
    try:
        ready = proc.stdout.readline()
        if '"ready"' not in ready:
            return {"ok": False, "error": f"helper not ready: {ready.strip() or proc.stderr.read()[-300:]}"}
        proc.stdin.write(json.dumps({"id": "1", "method": method, "params": params, "timeoutMs": 15000}) + "\n")
        proc.stdin.flush()
        started = time.time()
        line = proc.stdout.readline()
        reply = json.loads(line) if line.strip() else {"ok": False, "error": "no reply"}
        reply["seconds"] = round(time.time() - started, 2)
        return reply
    finally:
        proc.kill()


def probe_abort(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    key = probe_key()
    sent = dispatch(target, key, "Write the whole numbers from one to five thousand in English words, one per "
                                 "line, with no commentary. Do not stop early and do not summarize.")
    running_after = None
    for _ in range(30):
        time.sleep(1)
        if (osess.get_session_entry(key).get("status") or "") == "running":
            running_after = round(time.time() - sent["sent"], 1)
            break
    # A run is abortable a moment after its session shows running; retry inside that window.
    attempts = []
    for _ in range(20):
        reply = helper_call(target, "sessions.abort", {"key": key, "clearQueued": True})
        status = (reply.get("result") or {}).get("status")
        attempts.append({"at": round(time.time() - sent["sent"], 1), "status": status or reply.get("error")})
        if status != "no-active-run" or (osess.get_session_entry(key).get("status") or "") != "running":
            break
        time.sleep(0.5)
    ended = None
    for _ in range(60):
        entry = osess.get_session_entry(key)
        if (entry.get("status") or "") != "running":
            ended = {"status": entry.get("status"), "abortedLastRun": entry.get("abortedLastRun"),
                     "seconds": round(time.time() - sent["sent"], 1)}
            break
        time.sleep(1)
    return {"ok": bool(reply.get("ok")) and status == "aborted" and ended is not None, "running_after": running_after,
            "attempts": attempts, "abort": {k: reply.get(k) for k in ("ok", "result", "error", "seconds")},
            "ended": ended}


def probe_history(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    before: Optional[Path] = ctx.get("before_db")
    if not before or not before.exists():
        return {"ok": False, "detail": "needs the pre-migration agent database (--before-db)"}
    con = sqlite3.connect(f"file:{before}?mode=ro", uri=True)
    try:
        sids = [r[0] for r in con.execute("SELECT session_id FROM transcript_events GROUP BY session_id "
                                          "ORDER BY MAX(created_at) DESC LIMIT 40")]
    finally:
        con.close()
    reads: Dict[str, Dict[str, List[str]]] = {}
    for label, db in (("before", before), ("after", target.agent_db)):
        osess.AGENT_DB_PATH = db
        reads[label] = {sid: osess.read_transcript_lines(sid) for sid in sids}
    use_store(target)
    # The gateway may append to a copied session after the migration (2026.9.7 posts a session
    # event to agent:main:main on start); what was there before must read back unchanged.
    changed = [sid for sid in sids if reads["after"][sid][:len(reads["before"][sid])] != reads["before"][sid]]
    appended = {sid[:8]: len(reads["after"][sid]) - len(reads["before"][sid])
                for sid in sids if len(reads["after"][sid]) > len(reads["before"][sid])}
    digest = hashlib.sha256("\n".join(line for sid in sids for line in reads["before"][sid]).encode()).hexdigest()[:16]
    con = sqlite3.connect(f"file:{target.agent_db}?mode=ro", uri=True)
    try:
        compressed = con.execute("SELECT COUNT(*) FROM transcript_events WHERE event_zstd IS NOT NULL").fetchone()[0] \
            if stores_compressed_events(con) else 0
        total = con.execute("SELECT COUNT(*) FROM transcript_events").fetchone()[0]
    finally:
        con.close()
    return {"ok": not changed, "sessions": len(sids), "lines_before": sum(len(v) for v in reads["before"].values()),
            "sha_before": digest, "changed": changed, "appended_after": appended,
            "compressed_events": compressed, "events": total}


def probe_patched(target: Target, ctx: Dict[str, Any]) -> Dict[str, Any]:
    failures = openclaw_patch_audit.audit(target.package_dir / "dist")
    return {"ok": not failures, "failures": failures[:10]}


def stores_compressed(target: Target) -> bool:
    con = sqlite3.connect(f"file:{target.agent_db}?mode=ro", uri=True)
    try:
        return stores_compressed_events(con)
    finally:
        con.close()


PROBES = [("health", probe_health), ("plugins", probe_plugins), ("hook_run", probe_hook_run),
          ("bootstrap", probe_bootstrap), ("session_entry", probe_session_entry), ("settle", probe_settle),
          ("abort", probe_abort), ("history", probe_history), ("patched", probe_patched)]


def run(target: Target, *, skip: Sequence[str] = (), ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ctx = dict(ctx or {})
    use_store(target)
    results: Dict[str, Any] = {}
    for name, probe in PROBES:
        if name in skip:
            continue
        started = time.time()
        try:
            result = probe(target, ctx)
        except Exception as exc:  # noqa: BLE001 - a probe that raises has failed
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
        result["probe_seconds"] = round(time.time() - started, 1)
        results[name] = result
        print(f"{'PASS' if result.get('ok') else 'FAIL'} {name} ({result['probe_seconds']} s)", flush=True)
    return {"target": target.name, "ok": all(r.get("ok") for r in results.values()), "results": results}


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=("staging", "production"), required=True)
    parser.add_argument("--skip", default="", help="comma-separated probe names")
    parser.add_argument("--boot-seconds", type=float)
    parser.add_argument("--before-db", type=Path, help="agent database from before the migration")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    target = staging_target() if args.target == "staging" else production_target()
    skip = [s for s in args.skip.split(",") if s]
    if args.target == "production" and "settle" not in skip:
        skip.append("settle")
    before = args.before_db
    if before is None and args.target == "staging":
        from ops import openclaw_staging as staging
        before = staging.PRE_BACKUP / "openclaw-agent.sqlite"
    report = run(target, skip=skip, ctx={"boot_seconds": args.boot_seconds, "before_db": before})
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
