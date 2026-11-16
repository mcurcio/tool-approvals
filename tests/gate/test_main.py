"""The gate's composition root: it serves only the mappings configured, answers health, and exits 0 on stop."""

import asyncio
import dataclasses
import ssl
from datetime import timedelta
from pathlib import Path

import httpx

from tests.fixtures.certs import TlsFiles, tls_files
from tests.fixtures.scripted_requests import Script, scripted_requests
from tests.gate.harness import gate_view, running_gate
from tool_approvals.auth.registry import issue_bearer
from tool_approvals.gate.main import EXIT_OK, serve


async def test_health_answers_while_serving(tmp_path: Path) -> None:
    tls = tls_files(tmp_path, ["localhost"])
    async with running_gate(Script(), tls) as gate:
        context = ssl.create_default_context(cafile=tls.ca)
        async with httpx.AsyncClient(verify=context) as client:
            base = f"https://localhost:{gate.view.health.port}"
            assert (await client.get(f"{base}/healthz")).status_code == 200
            assert (await client.get(f"{base}/readyz")).status_code == 200


async def test_with_no_mapping_configured_it_serves_health_and_stops_cleanly(tmp_path: Path) -> None:
    tls: TlsFiles = tls_files(tmp_path, ["localhost"])
    async with scripted_requests(Script(), tls, issue_bearer()[0]) as approvals:
        view = dataclasses.replace(
            gate_view(approvals, tls, budget=timedelta(seconds=1), entries=()),
            check_api=None,
            ext_mcp=None,
            ext_authz=None,
        )
        stop = asyncio.Event()
        serving = asyncio.create_task(serve(view, stop))
        context = ssl.create_default_context(cafile=tls.ca)
        async with httpx.AsyncClient(verify=context) as client, asyncio.timeout(10):
            while True:
                try:
                    reply = await client.get(f"https://localhost:{view.health.port}/healthz")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.01)
        assert reply.status_code == 200
        stop.set()
        assert await serving == EXIT_OK


async def test_on_stop_it_turns_unready_for_the_drain_then_closes(tmp_path: Path) -> None:
    tls: TlsFiles = tls_files(tmp_path, ["localhost"])
    drain = timedelta(seconds=2)
    async with scripted_requests(Script(), tls, issue_bearer()[0]) as approvals:
        view = dataclasses.replace(gate_view(approvals, tls, budget=timedelta(seconds=1), entries=()), drain=drain)
        stopping = asyncio.Event()
        serving = asyncio.create_task(serve(view, stopping))
        context = ssl.create_default_context(cafile=tls.ca)
        base = f"https://localhost:{view.health.port}"

        async def get(path: str) -> httpx.Response:
            async with httpx.AsyncClient(verify=context) as client:  # a new connection each time
                return await client.get(f"{base}{path}")

        async with asyncio.timeout(10):
            while True:
                try:
                    ready = await get("/readyz")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.01)
            assert ready.status_code == 200
            loop = asyncio.get_running_loop()
            started = loop.time()
            stopping.set()
            await asyncio.sleep(0.5)  # well inside the drain
            draining = await get("/readyz")
            assert (draining.status_code, draining.text) == (503, "not ready: stopping")
            assert (await get("/healthz")).status_code == 200  # still accepting new connections
            assert await serving == EXIT_OK
        assert loop.time() - started >= drain.total_seconds()
