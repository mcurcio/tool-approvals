"""GrpcAuth on a real TLS gRPC server: the bearer or client certificate, the grant and the transport, for unary and
streaming methods."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import grpc.aio
import pytest

from tests.fixtures.certs import ClientCert, TlsFiles, client_cert, tls_files
from tests.fixtures.ports import free_port
from tool_approvals.auth.grpc_auth import GrpcAuth, current_caller
from tool_approvals.auth.registry import Registry, issue_bearer
from tool_approvals.config.views import ListenerView, RegistryEntryView, TlsView
from tool_approvals.contract.calls.v1 import calls_pb2
from tool_approvals.contract.check.v1 import check_pb2, check_pb2_grpc
from tool_approvals.contract.requests.v1 import requests_pb2, requests_pb2_grpc
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import CallerName
from tool_approvals.serving.grpc_server import grpc_server

CHECK = "/tool_approvals.contract.check.v1.CheckService/Check"
AWAIT = "/tool_approvals.contract.requests.v1.ApprovalRequestsService/AwaitOutcome"


class _Check(check_pb2_grpc.CheckServiceServicer):
    async def Check(
        self, request: check_pb2.CheckRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> check_pb2.CheckResponse:
        return check_pb2.CheckResponse(agent_text=current_caller.get().name)


class _Requests(requests_pb2_grpc.ApprovalRequestsServiceServicer):
    async def AwaitOutcome(
        self, request: requests_pb2.AwaitOutcomeRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> AsyncIterator[requests_pb2.AwaitOutcomeResponse]:
        for _ in range(3):
            await asyncio.sleep(0)  # let another RPC's handler run in between
            yield requests_pb2.AwaitOutcomeResponse(refused=calls_pb2.Refused(comment=current_caller.get().name))

    # Never reached in these tests: Claim has no grant mapping, the others are not called.
    async def Request(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        raise NotImplementedError

    async def Claim(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        raise NotImplementedError

    async def OpenWait(self, request: Any, context: grpc.aio.ServicerContext[Any, Any]) -> Any:
        raise NotImplementedError


class Served:
    def __init__(self, tls: TlsFiles, port: int, bearers: dict[str, str]) -> None:
        self.tls = tls
        self.port = port
        self.bearers = bearers

    def channel(self) -> grpc.aio.Channel:
        credentials = grpc.ssl_channel_credentials(root_certificates=self.tls.ca.read_bytes())
        return grpc.aio.secure_channel(f"localhost:{self.port}", credentials)


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[Served]:
    tls = tls_files(tmp_path, ["localhost"])
    bearers = {name: issue_bearer()[0] for name in ("checker", "waiter", "nothing")}
    grants: dict[str, set[Method]] = {
        "checker": {Method.CHECK, Method.AWAIT_OUTCOME},
        "waiter": {Method.AWAIT_OUTCOME},
        "nothing": set(),
    }
    registry = Registry(
        [
            RegistryEntryView(CallerName(name), hashlib.sha256(bearer.encode()).digest(), frozenset(grants[name]))
            for name, bearer in bearers.items()
        ]
    )
    port = free_port()
    # Claim is served but mapped to no grant: an unlisted path.
    server = grpc_server(
        ListenerView("localhost", port, TlsView(tls.cert, tls.key, None)),
        [GrpcAuth(registry, {CHECK: Method.CHECK, AWAIT: Method.AWAIT_OUTCOME})],
    )
    check_pb2_grpc.add_CheckServiceServicer_to_server(_Check(), server)
    requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(_Requests(), server)
    await server.start()
    try:
        yield Served(tls, port, bearers)
    finally:
        await server.stop(None)


def _auth(bearer: str) -> tuple[tuple[str, str]]:
    return (("authorization", f"Bearer {bearer}"),)


async def test_granted_caller_is_served_as_itself(served: Served) -> None:
    async with served.channel() as channel:
        reply = await check_pb2_grpc.CheckServiceStub(channel).Check(
            check_pb2.CheckRequest(), metadata=_auth(served.bearers["checker"])
        )
    assert reply.agent_text == "checker"


async def test_streaming_handler_runs_as_the_caller(served: Served) -> None:
    async with served.channel() as channel:
        stream = requests_pb2_grpc.ApprovalRequestsServiceStub(channel).AwaitOutcome(
            requests_pb2.AwaitOutcomeRequest(), metadata=_auth(served.bearers["waiter"])
        )
        comments = [reply.refused.comment async for reply in stream]
    assert comments == ["waiter"] * 3


async def test_concurrent_rpcs_each_run_as_their_own_caller(served: Served) -> None:
    async def comments(caller: str) -> list[str]:
        stream = requests_pb2_grpc.ApprovalRequestsServiceStub(channel).AwaitOutcome(
            requests_pb2.AwaitOutcomeRequest(), metadata=_auth(served.bearers[caller])
        )
        return [reply.refused.comment async for reply in stream]

    async with served.channel() as channel:
        results = await asyncio.gather(*(comments(caller) for caller in ["waiter", "checker"] * 4))
    assert results == [[caller] * 3 for caller in ["waiter", "checker"] * 4]


@pytest.mark.parametrize("metadata", [(), (("authorization", "Bearer wrong"),), (("authorization", "Basic x"),)])
async def test_without_a_registry_bearer_is_unauthenticated(
    served: Served, metadata: tuple[tuple[str, str], ...]
) -> None:
    async with served.channel() as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(check_pb2.CheckRequest(), metadata=metadata)
        assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED
        stream = requests_pb2_grpc.ApprovalRequestsServiceStub(channel).AwaitOutcome(
            requests_pb2.AwaitOutcomeRequest(), metadata=metadata
        )
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await stream.read()
        assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def test_two_authorization_values_are_unauthenticated(served: Served) -> None:
    metadata = (*_auth(served.bearers["checker"]), *_auth(served.bearers["checker"]))
    async with served.channel() as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(check_pb2.CheckRequest(), metadata=metadata)
    assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.parametrize("caller", ["waiter", "nothing"])
async def test_a_caller_without_the_grant_is_denied(served: Served, caller: str) -> None:
    async with served.channel() as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(
                check_pb2.CheckRequest(), metadata=_auth(served.bearers[caller])
            )
    assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED


async def test_a_method_path_without_a_grant_mapping_is_denied_to_everyone(served: Served) -> None:
    async with served.channel() as channel:
        for bearer in served.bearers.values():
            with pytest.raises(grpc.aio.AioRpcError) as refused:
                await requests_pb2_grpc.ApprovalRequestsServiceStub(channel).Claim(
                    requests_pb2.ClaimRequest(), metadata=_auth(bearer)
                )
            assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED


async def test_plaintext_is_refused(served: Served) -> None:
    async with grpc.aio.insecure_channel(f"localhost:{served.port}") as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(
                check_pb2.CheckRequest(), metadata=_auth(served.bearers["checker"]), timeout=5
            )
    assert refused.value.code() is grpc.StatusCode.UNAVAILABLE


async def test_a_server_the_client_does_not_trust_is_refused(served: Served, tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    other = tls_files(tmp_path / "other", ["localhost"])
    credentials = grpc.ssl_channel_credentials(root_certificates=other.ca.read_bytes())
    async with grpc.aio.secure_channel(f"localhost:{served.port}", credentials) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(
                check_pb2.CheckRequest(), metadata=_auth(served.bearers["checker"]), timeout=5
            )
    assert refused.value.code() is grpc.StatusCode.UNAVAILABLE


# Client certificates: a listener with a client CA, and registry entries naming certificates it signed.


class MtlsServed:
    def __init__(self, tls: TlsFiles, port: int, certs: dict[str, ClientCert], bearer: str) -> None:
        self.tls = tls
        self.port = port
        self.certs = certs
        self.bearer = bearer

    def channel(self, presenting: str) -> grpc.aio.Channel:
        cert = self.certs[presenting]
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self.tls.ca.read_bytes(),
            private_key=cert.key.read_bytes(),
            certificate_chain=cert.cert.read_bytes(),
        )
        return grpc.aio.secure_channel(f"localhost:{self.port}", credentials)


@pytest.fixture
async def mtls(tmp_path: Path) -> AsyncIterator[MtlsServed]:
    tls = tls_files(tmp_path, ["localhost"])
    certs = {name: client_cert(tls.authority, tmp_path, name) for name in ("gateway", "waiter", "stranger")}
    bearer = issue_bearer()[0]
    grants = {"gateway": {Method.CHECK}, "waiter": {Method.AWAIT_OUTCOME}}  # the stranger is in no entry
    registry = Registry(
        [
            *(
                RegistryEntryView(
                    CallerName(name), None, client_cert_sha256=certs[name].sha256, grants=frozenset(granted)
                )
                for name, granted in grants.items()
            ),
            RegistryEntryView(CallerName("hook"), hashlib.sha256(bearer.encode()).digest(), frozenset({Method.CHECK})),
        ]
    )
    port = free_port()
    server = grpc_server(
        ListenerView("localhost", port, TlsView(tls.cert, tls.key, tls.ca)),
        [GrpcAuth(registry, {CHECK: Method.CHECK, AWAIT: Method.AWAIT_OUTCOME})],
    )
    check_pb2_grpc.add_CheckServiceServicer_to_server(_Check(), server)
    requests_pb2_grpc.add_ApprovalRequestsServiceServicer_to_server(_Requests(), server)
    await server.start()
    try:
        yield MtlsServed(tls, port, certs, bearer)
    finally:
        await server.stop(None)


async def _check(served: MtlsServed, presenting: str, metadata: tuple[tuple[str, str], ...] = ()) -> str:
    async with served.channel(presenting) as channel:
        reply = await check_pb2_grpc.CheckServiceStub(channel).Check(check_pb2.CheckRequest(), metadata=metadata)
    return reply.agent_text


async def test_a_certificate_entry_granted_check_is_served_as_itself(mtls: MtlsServed) -> None:
    assert await _check(mtls, "gateway") == "gateway"


async def test_a_certificate_entry_streams_as_itself(mtls: MtlsServed) -> None:
    async with mtls.channel("waiter") as channel:
        stream = requests_pb2_grpc.ApprovalRequestsServiceStub(channel).AwaitOutcome(requests_pb2.AwaitOutcomeRequest())
        comments = [reply.refused.comment async for reply in stream]
    assert comments == ["waiter"] * 3


async def test_a_certificate_entry_without_the_grant_is_denied(mtls: MtlsServed) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await _check(mtls, "waiter")
    assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED


async def test_an_unregistered_certificate_is_unauthenticated(mtls: MtlsServed) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await _check(mtls, "stranger")
    assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def test_a_bearer_with_an_unregistered_certificate_is_the_bearers_caller(mtls: MtlsServed) -> None:
    assert await _check(mtls, "stranger", _auth(mtls.bearer)) == "hook"


async def test_a_bearer_with_another_entrys_certificate_is_unauthenticated(mtls: MtlsServed) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await _check(mtls, "gateway", _auth(mtls.bearer))
    assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def test_a_repeated_authorization_is_unauthenticated_even_with_a_registered_certificate(
    mtls: MtlsServed,
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await _check(mtls, "gateway", (*_auth(mtls.bearer), *_auth(mtls.bearer)))
    assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED


async def test_a_listener_with_a_client_ca_refuses_a_caller_without_a_certificate(mtls: MtlsServed) -> None:
    credentials = grpc.ssl_channel_credentials(root_certificates=mtls.tls.ca.read_bytes())
    async with grpc.aio.secure_channel(f"localhost:{mtls.port}", credentials) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await check_pb2_grpc.CheckServiceStub(channel).Check(
                check_pb2.CheckRequest(), metadata=_auth(mtls.bearer), timeout=5
            )
    assert refused.value.code() is grpc.StatusCode.UNAVAILABLE
