"""nftables rules that keep aura-coder off this host's services and private networks.

Traffic from the aura-coder user to the loopback ports in ``coding.blocked_tcp_ports`` is refused,
and so is any traffic to private, link-local and carrier-grade address ranges, which covers the
Docker bridge networks and the cloud metadata address. DNS on loopback, the internet (Anthropic's
API, package indexes, git hosts) and ephemeral ports its own test servers open stay reachable.
The runner refuses to start a job while the table is missing.

    venv/bin/python -m app.coding.firewall {apply,remove,status,render}
"""
from __future__ import annotations

import argparse
import pwd
import re
import subprocess
import sys
import tempfile
from typing import Iterable, List, Sequence, Union

from app.coding.units import CODER_USER

TABLE = "aura_coder"
EPHEMERAL_FROM = 32768
# Reachable by design: DNS (systemd-resolved on 127.0.0.53).
OPEN_BY_DESIGN = frozenset({53})
PRIVATE_V4 = ("10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16")
PRIVATE_V6 = ("fc00::/7", "fe80::/10")
_RANGE = re.compile(r"^(\d{1,5})-(\d{1,5})$")

Port = Union[int, str]


def port_elements(ports: Iterable[Port]) -> List[str]:
    """nft set elements for ports and "a-b" ranges; anything else is an error."""
    out: List[str] = []
    for port in ports:
        text = str(port).strip()
        m = _RANGE.match(text)
        if m:
            low, high = int(m.group(1)), int(m.group(2))
            if not 1 <= low < high <= 65535:
                raise ValueError(f"bad port range {text!r}")
            out.append(f"{low}-{high}")
        elif text.isdigit() and 1 <= int(text) <= 65535:
            out.append(text)
        else:
            raise ValueError(f"bad port {text!r}")
    if not out:
        raise ValueError("no ports to block")
    return out


def covers(elements: Sequence[str], port: int) -> bool:
    for element in elements:
        low, _, high = element.partition("-")
        if int(low) <= port <= int(high or low):
            return True
    return False


def render(ports: Iterable[Port], user: str = CODER_USER) -> str:
    """The table, replaced atomically: nft applies the whole file or nothing."""
    elements = ", ".join(port_elements(ports))
    return (
        f"add table inet {TABLE}\n"
        f"delete table inet {TABLE}\n"
        f"table inet {TABLE} {{\n"
        f"  set local_ports {{ type inet_service; flags interval; elements = {{ {elements} }} }}\n"
        f"  chain output {{\n"
        f"    type filter hook output priority filter; policy accept;\n"
        f'    meta skuid "{user}" oifname "lo" tcp dport @local_ports counter reject with tcp reset\n'
        f'    meta skuid "{user}" oifname "lo" udp dport @local_ports counter reject\n'
        f'    meta skuid "{user}" ip daddr {{ {", ".join(PRIVATE_V4)} }} counter reject\n'
        f'    meta skuid "{user}" ip6 daddr {{ {", ".join(PRIVATE_V6)} }} counter reject\n'
        f"  }}\n"
        f"}}\n"
    )


def _configured_ports() -> list:
    from app.config import get_coding_config

    return list(get_coding_config()["blocked_tcp_ports"])


def apply(ports: Iterable[Port]) -> None:
    ruleset = render(ports)
    with tempfile.NamedTemporaryFile("w", suffix=".nft") as fh:
        fh.write(ruleset)
        fh.flush()
        subprocess.run(["nft", "-f", fh.name], check=True, capture_output=True, text=True)


def remove() -> None:
    subprocess.run(["nft", "delete", "table", "inet", TABLE], capture_output=True, text=True)


def active() -> bool:
    """The table is loaded with all four user rules (the runner's precondition)."""
    try:
        uid = pwd.getpwnam(CODER_USER).pw_uid
    except KeyError:
        return False
    run = subprocess.run(["nft", "list", "table", "inet", TABLE], capture_output=True, text=True)
    # nft lists the user by uid once it resolved the name.
    rules = re.findall(rf'meta skuid (?:{uid}|"{re.escape(CODER_USER)}") ', run.stdout)
    return run.returncode == 0 and len(rules) == 4 and "@local_ports" in run.stdout


def uncovered_listeners(ports: Iterable[Port]) -> List[str]:
    """Loopback or wildcard TCP listeners below the ephemeral range that the rules leave open."""
    elements = port_elements(ports)
    run = subprocess.run(["ss", "-H", "-ltn"], capture_output=True, text=True, check=True)
    open_ports = []
    for line in run.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        address, _, port_text = fields[3].rpartition(":")
        if not port_text.isdigit():
            continue
        port = int(port_text)
        local = address.strip("[]").split("%")[0]
        if local in ("127.0.0.1", "::1", "0.0.0.0", "::", "*") or local.startswith("127."):
            if port < EPHEMERAL_FROM and port not in OPEN_BY_DESIGN and not covers(elements, port):
                open_ports.append(fields[3])
    return sorted(set(open_ports))


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("apply", "remove", "status", "render"))
    action = parser.parse_args(argv).action
    if action == "render":
        print(render(_configured_ports()), end="")
    elif action == "apply":
        apply(_configured_ports())
        print(f"nftables table inet {TABLE}: applied")
    elif action == "remove":
        remove()
        print(f"nftables table inet {TABLE}: removed")
    else:
        ok = active()
        uncovered = uncovered_listeners(_configured_ports())
        print(f"active={ok} uncovered_listeners={uncovered}")
        return 0 if ok and not uncovered else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
