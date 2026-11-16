"""The `:requests` edge served over real gRPC with TLS and registry auth, on the SQL store, with a client per
registry entry."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tests.approvals.domain.examples import HOOK, RULES, WAITER
from tests.approvals.store.postgres import empty_database
from tests.approvals.store.world import StoreWorld, running_feed, store_world
from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.ports import free_port
from tool_approvals.approvals.edges.requests import RequestsServicer
from tool_approvals.auth.grpc_auth import GrpcAuth
from tool_approvals.auth.registry import Registry, issue_bearer
from tool_approvals.config.methods import Method
from tool_approvals.config.views import ListenerView, RegistryEntryView, RequestsClientView, TlsView
from tool_approvals.contract.requests.v1 import requests_pb2_grpc
from tool_approvals.contract_mapping.requests import REQUESTS_METHODS
from tool_approvals.kernel.names import CallerName
from tool_approvals.requests_client.client import RequestsClient
from tool_approvals.serving.grpc_server import grpc_server

# Each registry entry and its grants: the gate's hook asks and claims, the wait server waits.
GRANTS = {
    HOOK: frozenset({Method.REQUEST, Method.CLAIM}),
    WAITER: frozenset({Method.OPEN_WAIT, Method.AWAIT_OUTCOME}),
    CallerName("checker"): frozenset({Method.CHECK}),
}
EDGE_RULES = replace(RULES, max_live_waits=2)


@dataclass
class Edge:
    world: StoreWorld
    clients: dict[CallerName, RequestsClient]  # one per registry entry
    stranger: RequestsClient  # a bearer no entry holds

    @property
    def hook(self) -> RequestsClient:
        return self.clients[HOOK]

    @property
    def waiter(self) -> RequestsClient:
        return self.clients[WAITER]


@asynccontextmanager
async def serving_edge(world: StoreWorld, tls: TlsFiles) -> AsyncIterator[Edge]:
    bearers = {name: issue_bearer()[0] for name in GRANTS}
    registry = Registry(
        [RegistryEntryView(name, hashlib.sha256(bearers[name].encode()).digest(), GRANTS[name]) for name in GRANTS]
    )
    port = free_port()
    server = grpc_server(
        ListenerView("localhost", port, TlsView(tls.cert, tls.key, None)), [GrpcAuth(registry, REQUESTS_METHODS)]
    )
    requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(
        RequestsServicer(world.requests, world.waits), server
    )
    await server.start()

    def client(bearer: str) -> RequestsClient:
        return RequestsClient.connect(RequestsClientView(f"dns:///localhost:{port}", tls.ca, bearer))

    clients = {name: client(bearer) for name, bearer in bearers.items()}
    stranger = client(issue_bearer()[0])
    try:
        async with running_feed(world.feed):
            yield Edge(world, clients, stranger)
    finally:
        for each in [*clients.values(), stranger]:
            await each.close()
        await server.stop(None)


@pytest.fixture
def tls(tmp_path: Path) -> TlsFiles:
    return tls_files(tmp_path, ["localhost"])


@pytest.fixture
def dsn() -> str:
    return empty_database()


@pytest.fixture
async def edge(dsn: str, tls: TlsFiles) -> AsyncIterator[Edge]:
    async with store_world(dsn) as base, serving_edge(StoreWorld(dsn, base.engine, EDGE_RULES), tls) as served:
        yield served
