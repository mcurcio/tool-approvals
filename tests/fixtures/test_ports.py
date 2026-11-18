"""free_port: a port a test names before its server starts is one no other test, worker or kernel pick can take."""

import socket
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
    taken = [free_port() for _ in range(50)]
    assert len(set(taken)) == len(taken)


@pytest.mark.parametrize("workers", [1, 2, 4, 7, 16, 64])
def test_workers_take_disjoint_nonempty_slices(workers: int) -> None:
    slices = [set(worker_ports(f"gw{index}", workers)) for index in range(workers)]
    assert all(slices)
    assert all(ports <= set(PORTS) for ports in slices)
    assert all(not (a & b) for a, b in combinations(slices, 2))
    assert worker_ports(f"gw{workers}", workers) == worker_ports("gw0", workers)  # a restarted worker's id


@pytest.mark.skipif(not _LINUX_EPHEMERAL.exists(), reason="the kernel's ephemeral range is read from Linux's /proc")
def test_the_ports_lie_outside_the_kernels_ephemeral_range() -> None:
    low, high = (int(bound) for bound in _LINUX_EPHEMERAL.read_text().split())
    assert PORTS.stop <= low or PORTS.start > high
