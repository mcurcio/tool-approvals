"""A throwaway CA and server certificate for TLS listeners in tests (trustme)."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import trustme


@dataclass(frozen=True, slots=True)
class TlsFiles:
    ca: Path  # PEM of the test CA
    cert: Path  # PEM chain of a server certificate for every host asked
    key: Path  # its private key


def tls_files(directory: Path, hosts: Sequence[str]) -> TlsFiles:
    """Write a new CA and a server certificate for `hosts` under `directory`."""
    ca = trustme.CA()
    issued = ca.issue_cert(*hosts)
    files = TlsFiles(ca=directory / "ca.pem", cert=directory / "cert.pem", key=directory / "key.pem")
    ca.cert_pem.write_to_path(str(files.ca))
    files.cert.write_bytes(b"".join(blob.bytes() for blob in issued.cert_chain_pems))
    issued.private_key_pem.write_to_path(str(files.key))
    return files
