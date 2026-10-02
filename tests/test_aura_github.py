"""aura-github, Claude's way to GitHub in a direct session: branches and pull requests, never main or a merge."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "aura_github.sh"
URL = "https://github.com/Hyper-AI-Lab/openclaw-jev.git"


def run(*args, cwd, env):
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **env})


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    remote, work = tmp_path / "github.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "a.txt").write_text("a\n")
    git(work, "add", "a.txt")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a")
    git(work, "remote", "add", "origin", URL)
    git(work, "checkout", "-q", "-b", "aura/fix")
    token = tmp_path / "pat"
    token.write_text("tok-123\n")
    # GitHub's URL reaches the local remote instead; git reads this config from the environment.
    env = {"AURA_GITHUB_TOKEN_FILE": str(token), "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": f"url.{remote.as_uri()}.insteadOf", "GIT_CONFIG_VALUE_0": URL}
    return SimpleNamespace(work=work, remote=remote, env=env, tmp=tmp_path)


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_a_branch_is_pushed_to_auras_repository(clone):
    out = run("push", cwd=clone.work, env=clone.env)
    assert out.returncode == 0, out.stderr
    assert git(clone.remote, "rev-parse", "refs/heads/aura/fix") == git(clone.work, "rev-parse", "HEAD")


def test_main_another_repository_and_a_merge_are_refused(clone):
    git(clone.work, "checkout", "-q", "main")
    main = run("push", cwd=clone.work, env=clone.env)
    assert main.returncode == 2 and "never push main" in main.stderr
    git(clone.work, "remote", "set-url", "origin", "https://github.com/someone/else.git")
    other = run("push", "aura/fix", cwd=clone.work, env=clone.env)
    assert other.returncode == 2 and "Kirill's go-ahead" in other.stderr
    merge = run("gh", "pr", "merge", "12", cwd=clone.work, env=clone.env)
    assert merge.returncode == 2 and "deploy_pr" in merge.stderr
    nothing = run(cwd=clone.work, env=clone.env)
    assert nothing.returncode == 2 and "usage" in nothing.stderr
    assert "refs/heads/main" not in git(clone.remote, "for-each-ref")


def test_gh_gets_the_token_and_the_repository_in_its_environment_only(clone):
    fake = clone.tmp / "bin"
    fake.mkdir()
    (fake / "gh").write_text('#!/bin/sh\necho "argv=$* token=$GH_TOKEN repo=$GH_REPO"\n')
    (fake / "gh").chmod(0o755)
    out = run("gh", "pr", "view", "12", cwd=clone.work, env={**clone.env, "PATH": f"{fake}:{os.environ['PATH']}"})
    assert out.stdout.strip() == "argv=pr view 12 token=tok-123 repo=Hyper-AI-Lab/openclaw-jev"
