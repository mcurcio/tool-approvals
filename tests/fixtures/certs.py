"""A throwaway CA and server certificate for TLS listeners in tests (trustme)."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TlsFiles:
    ca: Path  # PEM of the test CA
    cert: Path  # PEM chain of a server certificate for every host asked
    key: Path  # its private key


def tls_files(directory: Path, hosts: Sequence[str]) -> TlsFiles:
    """Write a new CA and a server certificate for `hosts` under `directory`."""
    raise NotImplementedError
