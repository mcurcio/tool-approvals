"""The e2e's only real tool server: echoes, and records exactly what reached it."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Received:
    server: str  # the route it came through
    tool: str
    arguments_json: bytes  # the exact bytes of `arguments` as received
    headers: Mapping[str, str]


class EchoTool:
    def received(self) -> list[Received]:
        """Every tools/call that reached the tool, in order."""
        raise NotImplementedError
