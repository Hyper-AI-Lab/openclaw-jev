"""The Temporal test helper runs a cached server binary only when no one but root or this user can have changed it."""
import os
import re
import stat
import tempfile
from types import SimpleNamespace

import pytest

from tests import temporal_env
from tests.temporal_env import acceptable, cached_binary

ME, OTHER = 1000, 4321
LINK = stat.S_IFLNK | 0o777


def node(uid, mode):
    return SimpleNamespace(st_uid=uid, st_mode=mode)


@pytest.mark.parametrize("owner", [0, ME])
def test_a_binary_owned_by_root_or_this_user_and_writable_only_by_its_owner_is_accepted(owner):
    assert acceptable(node(owner, stat.S_IFREG | 0o755), node(owner, stat.S_IFREG | 0o755), ME)


@pytest.mark.parametrize("mode", [0o775, 0o757])
def test_a_binary_writable_by_its_group_or_by_others_is_refused(mode):
    assert not acceptable(node(ME, stat.S_IFREG | mode), node(ME, stat.S_IFREG | mode), ME)


def test_a_binary_owned_by_another_user_is_refused():
    assert not acceptable(node(OTHER, stat.S_IFREG | 0o755), node(OTHER, stat.S_IFREG | 0o755), ME)


@pytest.mark.parametrize("link, target, accepted", [
    (node(0, LINK), node(0, stat.S_IFREG | 0o755), True),
    (node(OTHER, LINK), node(0, stat.S_IFREG | 0o755), False),
    (node(0, LINK), node(OTHER, stat.S_IFREG | 0o755), False),
    (node(0, LINK), node(0, stat.S_IFREG | 0o775), False),
], ids=["both-pass", "link-owned-by-another-user", "target-owned-by-another-user", "target-group-writable"])
def test_a_symlink_is_accepted_only_when_the_link_and_its_target_both_pass(link, target, accepted):
    assert acceptable(link, target, ME) is accepted


@pytest.fixture
def temp_dirs(tmp_path, monkeypatch):
    """This process's temp dir and the shared one, both in tmp_path."""
    own, shared = tmp_path / "own", tmp_path / "shared"
    own.mkdir()
    shared.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(own))
    monkeypatch.setattr(temporal_env, "SHARED_TMP", str(shared))
    return own, shared


def binary(path, mode=0o755):
    path.write_text("#!/bin/sh\n")
    os.chmod(path, mode)
    return path


def test_the_first_acceptable_binary_is_used(temp_dirs):
    own, shared = temp_dirs
    binary(shared / "server")
    assert cached_binary("server") == str(shared / "server")
    binary(own / "server")
    assert cached_binary("server") == str(own / "server")


def test_a_group_writable_binary_fails_with_its_path(temp_dirs):
    own, shared = temp_dirs
    binary(own / "server", 0o775)
    binary(shared / "server")
    with pytest.raises(RuntimeError, match=re.escape(str(own / "server"))):
        cached_binary("server")


def test_a_link_to_a_missing_file_fails_with_its_path(temp_dirs):
    own, shared = temp_dirs
    (own / "server").symlink_to(own / "missing")
    binary(shared / "server")
    with pytest.raises(RuntimeError, match=re.escape(str(own / "server"))):
        cached_binary("server")


def test_no_cached_binary_leaves_the_download_to_the_sdk(temp_dirs):
    assert cached_binary("server") is None
