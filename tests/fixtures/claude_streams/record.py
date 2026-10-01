"""Record real Claude Code stream-json runs as test fixtures (run as root on the host).

Each scenario runs the pinned `claude -p` as aura-coder in a hardened transient unit, the way the
runner does: stdout to a root-owned file, and the exit recorded by a root `ExecStopPost`.

    venv/bin/python tests/fixtures/claude_streams/record.py [SCENARIO ...]
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.coding.units import CLAUDE_BIN, CODER_HOME, CODER_USER, JOBS_DIR, TOKEN_ENV_FILE  # noqa: E402
from app.coding.units import systemd_run_argv, unit_properties  # noqa: E402
from app.config import get_coding_config  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = Path("/srv/aura-code/runs/_fixtures")
REPORT = {"type": "object", "properties": {"summary": {"type": "string"}, "phrase": {"type": "string"}},
          "required": ["summary"]}
BUGGY = '''def add(a, b):
    return a - b
'''
TEST = '''import unittest
from calc import add

class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

if __name__ == "__main__":
    unittest.main()
'''


def job_dir(name: str, files: dict) -> Path:
    job = JOBS_DIR / f"_fixture-{name}"
    shutil.rmtree(job, ignore_errors=True)
    job.mkdir(mode=0o700)
    for rel, text in files.items():
        (job / rel).write_text(text, encoding="utf-8")
    for path in [job, *job.rglob("*")]:
        shutil.chown(path, CODER_USER, CODER_USER)
    return job


def run(name: str, args: list, files: dict, *, token: bool = True, env: dict = None,
        interrupt_after: float = None, signal: str = "SIGINT", timeout: int = 300) -> dict:
    cfg = get_coding_config()
    job = job_dir(name, files)
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream, exit_file = RUNS / f"{name}.jsonl", RUNS / f"{name}.exit"
    for path in (stream, exit_file):
        path.unlink(missing_ok=True)
    unit = f"aura-claude-fixture-{name}"
    props = unit_properties(writable=[job, CODER_HOME], memory_max=cfg["memory_max"], cpu_quota=cfg["cpu_quota"],
                            tasks_max=int(cfg["tasks_max"]), runtime_max_sec=timeout,
                            env_file=TOKEN_ENV_FILE if token else None)
    props += [f"StandardOutput=file:{stream}", f"StandardError=file:{RUNS / (name + '.stderr')}",
              f"ExecStopPost=+/bin/sh -c 'echo \"$SERVICE_RESULT $EXIT_CODE $EXIT_STATUS\" > {exit_file}'"]
    command = [str(CLAUDE_BIN), "-p", *args]
    subprocess.run(systemd_run_argv(unit, command, properties=props, workdir=job, env=env), check=True)
    started = time.monotonic()
    interrupted = False
    while time.monotonic() - started < timeout + 30:
        if interrupt_after is not None and not interrupted and time.monotonic() - started >= interrupt_after:
            subprocess.run(["systemctl", "kill", f"--signal={signal}", unit], check=False)
            interrupted = True
        if exit_file.exists():
            break
        time.sleep(0.5)
    exit_line = exit_file.read_text().strip() if exit_file.exists() else "missing"
    target = HERE / f"{name}.jsonl"
    shutil.copyfile(stream, target)
    (HERE / f"{name}.exit").write_text(exit_line + "\n")
    shutil.rmtree(job, ignore_errors=True)
    events = [json.loads(line) for line in target.read_text().splitlines() if line.strip().startswith("{")]
    kinds = [e.get("type") + ("/" + e["subtype"] if e.get("subtype") else "") for e in events]
    print(f"{name}: exit '{exit_line}', {len(events)} events: {', '.join(dict.fromkeys(kinds))}")
    return {"events": events, "exit": exit_line}


COMMON = ["--output-format", "stream-json", "--verbose", "--model", "opus", "--fallback-model", "sonnet",
          "--permission-mode", "bypassPermissions", "--permission-prompts", "none",
          "--append-system-prompt", "You are working on a coding task for Aura. Work only inside the current directory."]


def scenario(name: str) -> dict:
    readme = {"README.md": "# Fixture\n\nThe phrase is HERON-7.\n", "NOTES.md": "Second file.\n"}
    if name == "success_readonly":
        return run(name, ["Read README.md and report its phrase in the structured report.", *COMMON,
                          "--max-turns", "6", "--json-schema", json.dumps(REPORT)], readme)
    if name == "edit_and_test":
        return run(name, ["calc.py has a bug and test_calc.py fails. Fix calc.py, then run "
                          "`python3 -m unittest -q` to confirm, and summarize what you changed.", *COMMON,
                          "--max-turns", "12", "--json-schema", json.dumps(REPORT)],
                   {"calc.py": BUGGY, "test_calc.py": TEST})
    if name == "max_turns":
        return run(name, ["Read README.md, then NOTES.md, then summarize both.", *COMMON, "--max-turns", "1"], readme)
    if name == "auth_failure":
        return run(name, ["Say hello.", *COMMON, "--max-turns", "2"], {}, token=False,
                   env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "x" * 95}, timeout=180)
    if name == "unknown_model":
        return run(name, ["Say hello.", "--output-format", "stream-json", "--verbose", "--model",
                          "claude-no-such-model-9", "--permission-mode", "bypassPermissions", "--max-turns", "2"], {})
    if name == "interrupted":
        return run(name, ["Run `for i in $(seq 1 120); do echo $i; sleep 1; done` with Bash and then report the "
                          "last number printed.", *COMMON, "--max-turns", "6"], {}, interrupt_after=25)
    if name == "resumed":
        first = run("resume_first", ["Remember the codeword OSPREY-4. Reply only with OK.", *COMMON, "--max-turns", "2"], {})
        session = next(e["session_id"] for e in first["events"] if e.get("type") == "system")
        return run(name, ["What was the codeword I gave you? Reply with it only.", *COMMON, "--max-turns", "2",
                          "--resume", session], {})
    raise SystemExit(f"unknown scenario {name}")


if __name__ == "__main__":
    for name in sys.argv[1:] or ["success_readonly", "edit_and_test", "max_turns", "auth_failure",
                                 "unknown_model", "interrupted", "resumed"]:
        scenario(name)
