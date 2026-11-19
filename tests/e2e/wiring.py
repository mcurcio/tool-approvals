"""What both gateway configurations are rendered from: how agents authenticate, where each tool server and hook
lives, and the credential each hook dial carries. Plain values, paths as the gateway container sees them."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class AgentJwt:
    """Agents authenticate to the gateway with a JWT; its `sub` is the principal the gate is handed."""

    issuer: str
    audience: str
    jwks_file: str  # the public key set, in the gateway container


@dataclass(frozen=True, slots=True)
class Hook:
    """A gate hook the gateway dials over TLS, presenting a registry bearer."""

    host: str  # the name the gateway dials and verifies (SNI)
    port: int
    ca_file: str  # the test CA, in the gateway container
    bearer: str


@dataclass(frozen=True, slots=True)
class WaitTarget:
    """The wait server, exposed to agents as one more MCP server behind each gateway."""

    server: str  # the route's server name (`wait_route.server`)
    url: str  # its MCP endpoint, https
    ca_file: str
    bearer: str  # a registry caller granted wait.call
    request_timeout: timedelta  # how long the gateway lets one held call run; above the wait's max_hold
    principal_header: str  # `wait_route.principal_header`: the gateway attests the principal in it
