"""A throwaway CA, server certificate and client certificates for TLS listeners in tests (trustme)."""

import hashlib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import trustme


@dataclass(frozen=True, slots=True)
class TlsFiles:
    ca: Path  # PEM of the test CA
    cert: Path  # PEM chain of a server certificate for every host asked
    key: Path  # its private key
    authority: trustme.CA  # the CA itself, to issue client certificates it signed (`client_cert`)


@dataclass(frozen=True, slots=True)
class ClientCert:
    cert: Path  # PEM of the client's leaf certificate
    key: Path  # its private key
    pem: str  # the leaf certificate as PEM text
    der: bytes  # the leaf certificate as DER

    @property
    def sha256(self) -> bytes:
        """What a registry entry names: the SHA-256 of the DER leaf certificate."""
        return hashlib.sha256(self.der).digest()


def tls_files(directory: Path, hosts: Sequence[str]) -> TlsFiles:
    """Write a new CA and a server certificate for `hosts` under `directory`."""
    ca = trustme.CA()
    issued = ca.issue_cert(*hosts)
    files = TlsFiles(ca=directory / "ca.pem", cert=directory / "cert.pem", key=directory / "key.pem", authority=ca)
    ca.cert_pem.write_to_path(str(files.ca))
    files.cert.write_bytes(b"".join(blob.bytes() for blob in issued.cert_chain_pems))
    issued.private_key_pem.write_to_path(str(files.key))
    return files


def client_cert(authority: trustme.CA, directory: Path, name: str) -> ClientCert:
    """Write a client certificate `authority` signed for `name` under `directory` (as `<name>.pem`, `<name>.key`)."""
    issued = authority.issue_cert(f"{name}.client.test", common_name=name)
    cert, key = directory / f"{name}.pem", directory / f"{name}.key"
    leaf = issued.cert_chain_pems[0].bytes()
    cert.write_bytes(leaf)
    issued.private_key_pem.write_to_path(str(key))
    pem = leaf.decode()
    return ClientCert(cert, key, pem, ssl.PEM_cert_to_DER_cert(pem))
