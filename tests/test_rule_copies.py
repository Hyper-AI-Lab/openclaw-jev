"""The repository's copy of the always-on architecture rule stays byte-identical to the host's."""
from pathlib import Path

import pytest

HOST_RULE = Path("/root/.cursor/rules/rmp-architecture.mdc")
REPO_RULE = Path(__file__).resolve().parents[1] / ".cursor" / "rules" / "rmp-architecture.mdc"


@pytest.mark.skipif(not HOST_RULE.exists(), reason="the host rule exists only on the production host")
def test_the_nested_rule_matches_the_host_rule():
    assert REPO_RULE.read_bytes() == HOST_RULE.read_bytes()
