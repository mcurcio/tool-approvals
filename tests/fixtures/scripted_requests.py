"""A scripted ApprovalRequests server over real gRPC with TLS and registry auth, for the gate's, the wait
server's and the requests client's tests."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta

from tests.fixtures.certs import TlsFiles
from tool_approvals.config.views import RequestsClientView
from tool_approvals.kernel.names import WaitId
from tool_approvals.kernel.requests import ClaimResult, OpenWaitResult, RequestResult, WaitReport


@dataclass
class Script:
    """Answers in order (each list is consumed front first), and the calls received, in order."""

    request: list[RequestResult] = field(default_factory=list)
    claim: list[ClaimResult] = field(default_factory=list)
    open_wait: list[OpenWaitResult] = field(default_factory=list)
    reports: dict[WaitId, list[WaitReport]] = field(default_factory=dict)
    delay: timedelta = timedelta(0)  # before every answer
    calls: list[tuple[str, object]] = field(default_factory=list)  # (method, the kernel-typed arguments)


def scripted_requests(script: Script, tls: TlsFiles, bearer: str) -> AbstractAsyncContextManager[RequestsClientView]:
    """Serve `script` on a free local port with `tls`, accepting `bearer` with every grant; yields the view a
    RequestsClient connects with."""
    raise NotImplementedError
