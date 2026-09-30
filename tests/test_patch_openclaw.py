"""RMP's OpenClaw patcher on the real shapes of 2026.9.1 and 2026.9.7 (tests/fixtures/openclaw_dist)."""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ops import openclaw_patch_audit as audit
from ops import openclaw_preflight as pf

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "openclaw_dist"
# A copy the patcher must reach in each release: the only one in 2026.9.1, the recovery bundle
# and a state-read worker in 2026.9.7.
DUPLICATES = {"2026.9.1": ["builtin-openclaw-zQV8Wwjr.js"],
              "2026.9.7": ["package-update-activation-recovery.mjs", "state/openclaw-state-read.worker.js"]}


def patch(dist):
    return subprocess.run(["bash", str(REPO / "patch_openclaw.sh")], capture_output=True, text=True, timeout=900,
                          env={**os.environ, "OPENCLAW_DIST_DIR": str(dist)})


def snapshot(dist):
    return {p.relative_to(dist): hashlib.sha256(p.read_bytes()).hexdigest() for p in dist.rglob("*") if p.is_file()}


@pytest.mark.parametrize("version", sorted(DUPLICATES))
def test_every_patch_lands_once_and_a_half_patched_dist_fails(tmp_path, version):
    dist = tmp_path / version
    shutil.copytree(FIXTURES / version, dist)
    assert audit.audit(dist), "the pristine fixtures carry every target"

    first = patch(dist)
    assert first.returncode == 0, first.stdout[-3000:]
    assert f"OK: all {len(audit.PATCHES)} RMP patches in place" in first.stdout
    assert audit.audit(dist) == []

    patched = snapshot(dist)
    second = patch(dist)
    assert second.returncode == 0 and "Already patched (idempotent re-run)." in second.stdout
    assert snapshot(dist) == patched

    for rel in DUPLICATES[version]:
        shutil.copyfile(FIXTURES / version / rel, dist / rel)
        left = [f for f in audit.audit(dist) if f.startswith("unpatched code left for")]
        assert left and all(f.endswith(rel) for f in left), left
        run = subprocess.run([sys.executable, str(REPO / "ops" / "openclaw_patch_audit.py"), str(dist)],
                             capture_output=True, text=True)
        assert run.returncode == 1 and "Do not start an unpatched gateway." in run.stdout
        # Patching again finishes the job.
        assert patch(dist).returncode == 0 and audit.audit(dist) == []


def test_patches_a_release_no_longer_needs_are_not_required():
    texts_97 = audit.dist_texts(FIXTURES / "2026.9.7")
    texts_91 = audit.dist_texts(FIXTURES / "2026.9.1")
    # GPT-6 thinking levels ship upstream in 2026.9.7; its startup placement work was reworked.
    assert any("OPENAI_GPT_6_MODEL_IDS" in t for t in texts_97.values())
    assert not any("function buildOpenAIThinkingProfile(params) {" in t for t in texts_97.values())
    assert not any("cleanedWorkspaceRoots = " in t for t in texts_97.values())
    assert any("cleanedWorkspaceRoots = " in t for t in texts_91.values())


def test_the_pre_flight_rehearses_with_the_same_patcher(tmp_path):
    dist = tmp_path / "dist"
    shutil.copytree(FIXTURES / "2026.9.7", dist)
    applied, detail = pf.patches_apply(dist)
    assert applied, detail
    (tmp_path / "empty").mkdir()
    applied, detail = pf.patches_apply(tmp_path / "empty")
    assert not applied and detail.startswith("ERROR: required patch missing after apply: hook-persistence")
