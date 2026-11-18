"""free_port: a port a test names before its server starts is one no other test, worker or kernel pick can take."""

import platform
import random
import socket
import subprocess
from itertools import combinations
from pathlib import Path

import pytest

from tests.fixtures.ports import PORTS, PortBlock, free_port, worker_ports

_LINUX_EPHEMERAL = Path("/proc/sys/net/ipv4/ip_local_port_range")


@pytest.mark.parametrize(("family", "host"), [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_a_port_held_on_either_loopback_is_never_handed_out(family: socket.AddressFamily, host: str) -> None:
    with socket.socket(family, socket.SOCK_STREAM) as holder:
        holder.bind((host, 0))
        held = holder.getsockname()[1]
        with pytest.raises(RuntimeError, match="is bound"):
            PortBlock(range(held, held + 1)).take()


def test_a_block_hands_out_each_port_once_before_it_wraps() -> None:
    port = free_port()
    block = PortBlock(range(port, port + 1))  # one port: it comes back each turn while unbound
    assert block.take() == block.take() == port


def test_blocks_for_one_slice_start_on_different_ports() -> None:
    ports = worker_ports("gw0", 4)  # two runs on one host, or a replacement worker sharing gw0's slice
    seeded = (random.Random(seed) for seed in (1, 2))  # noqa: S311 (a seeded start, not a secret)
    first, second = (PortBlock(ports, rng) for rng in seeded)
    assert first.take() != second.take()


@pytest.mark.parametrize("workers", [1, 2, 4, 7, 16, 64])
def test_workers_take_disjoint_nonempty_slices(workers: int) -> None:
    slices = [set(worker_ports(f"gw{index}", workers)) for index in range(workers)]
    assert all(slices)
    assert all(ports <= set(PORTS) for ports in slices)
    assert all(not (a & b) for a, b in combinations(slices, 2))
    assert worker_ports(f"gw{workers}", workers) == worker_ports("gw0", workers)  # a replacement shares gw0's


def _ephemeral_range() -> tuple[int, int]:
    """The kernel's ephemeral port range: Linux's /proc file, or macOS's sysctl pair."""
    if _LINUX_EPHEMERAL.exists():
        text = _LINUX_EPHEMERAL.read_text()
    elif platform.system() == "Darwin":
        text = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "net.inet.ip.portrange.first", "net.inet.ip.portrange.last"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    else:
        pytest.skip("the kernel's ephemeral range is read from Linux's /proc or macOS's sysctl")
    low, high = (int(bound) for bound in text.split())
    return low, high


def test_the_ports_lie_outside_the_kernels_ephemeral_range() -> None:
    low, high = _ephemeral_range()
    assert PORTS.stop <= low or PORTS.start > high
