"""Every gateway adapter is optional at install time too: with Envoy's types (the `envoy` extra) absent, the gate's
composition root imports and serves the other mappings."""

import subprocess
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).parents[2]

# Run in a fresh interpreter, so no earlier test has imported Envoy's types already.
_SCRIPT: Final = """
import sys
for blocked in ("envoy", "xds", "udpa", "validate"):
    sys.modules[blocked] = None  # an import of any of them raises ImportError
sys.path.insert(0, sys.argv[1])

import asyncio, dataclasses, ssl, tempfile
from datetime import timedelta
from pathlib import Path

import httpx

from tests.fixtures.certs import tls_files
from tests.fixtures.scripted_requests import Script, scripted_requests
from tests.gate.harness import gate_view
from tool_approvals.auth.registry import issue_bearer
from tool_approvals.gate.main import EXIT_OK, serve


async def main() -> None:
    tls = tls_files(Path(tempfile.mkdtemp()), ["localhost"])
    async with scripted_requests(Script(), tls, issue_bearer()[0]) as approvals:
        view = dataclasses.replace(
            gate_view(approvals, tls, budget=timedelta(seconds=1), entries=()), ext_authz=None
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
    assert sys.modules["envoy"] is None


asyncio.run(main())
print("served")
"""


def test_the_gate_serves_without_envoys_types() -> None:
    ran = subprocess.run(  # noqa: S603 (this interpreter, a fixed script)
        [sys.executable, "-I", "-c", _SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=50, check=False
    )
    assert (ran.returncode, ran.stdout.strip()) == (0, "served"), ran.stderr
