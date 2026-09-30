"""The repository's copy of the always-on architecture rule stays byte-identical to the host's."""
from pathlib import Path

import pytest

HOST_RULE = Path("/root/.cursor/rules/rmp-architecture.mdc")
REPO_RULE = Path(__file__).resolve().parents[1] / ".cursor" / "rules" / "rmp-architecture.mdc"


def test_the_nested_rule_matches_the_host_rule():
    try:
        host = HOST_RULE.read_bytes()
    except OSError:
        pytest.skip("the host rule exists only on the production host")
    assert REPO_RULE.read_bytes() == host
