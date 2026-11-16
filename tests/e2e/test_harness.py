"""The harness and the image, before the services serve: the image runs, the rendered service configuration
passes `config check` inside it, both guarded gateway configurations load in their gateways, and both gateways
route an ungated call to the echo tool with the guardrails off. The full stack's cases are S1.7's."""

import asyncio
import json
from pathlib import Path

import pytest

from tests.e2e import stack
from tests.fixtures.echo_tool import EchoTool

pytestmark = pytest.mark.timeout(300)  # a cold run pulls the gateway and Postgres images

# Odd whitespace, escapes and non-ASCII: the bytes must reach the tool unchanged.
_ARGUMENTS = b'{ "path" :"a\\u00e9/b\\"c",\t"n": 1.50, "s": "\xc3\xa9\xe2\x80\x8b" }'


async def _docker(*args: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    output, _ = await process.communicate()
    assert process.returncode is not None
    return process.returncode, output.decode(errors="replace")


async def test_image_runs_help_with_a_read_only_root() -> None:
    status, output = await _docker("run", "--rm", "--read-only", "--cap-drop", "ALL", stack.image(), "--help")
    assert status == 0, output
    assert output.startswith("usage: tool-approvals")
    status, output = await _docker("run", "--rm", "--entrypoint", "id", stack.image(), "-u")
    assert (status, output.strip()) == (0, "65532")


async def test_rendered_configuration_passes_config_check(tmp_path: Path) -> None:
    rendered = stack.render(tmp_path, guardrails=True)
    async with stack.project(rendered) as compose:
        output = await compose.run("run", "--rm", "--no-deps", "gate", "config", "check")
    assert "configuration ok" in output


async def test_guarded_gateway_configurations_load(tmp_path: Path) -> None:
    rendered = stack.render(tmp_path, guardrails=True)
    async with stack.project(rendered) as compose:
        # Each refuses a configuration it cannot load with a non-zero exit, which compose.run raises.
        await compose.run("run", "--rm", "--no-deps", "agentgateway", "--validate-only", "-f", "/e2e/agentgateway.yaml")
        envoy = await compose.run("run", "--rm", "--no-deps", "envoy", "--mode", "validate", "-c", "/e2e/envoy.json")
    assert "OK" in envoy, envoy


async def test_both_gateways_route_an_ungated_call_to_the_echo_tool(tmp_path: Path) -> None:
    rendered = stack.render(tmp_path, guardrails=False)
    echo = EchoTool(rendered.records)
    async with stack.project(rendered) as compose:
        await compose.up("postgres", "echo", "agentgateway", "envoy")
        gateways = await stack.gateways(compose, rendered, echo)
        for name, gateway in gateways.items():
            outcome = await gateway.call("agent-1", "notes", "echo", _ARGUMENTS)
            assert outcome.ran, (name, outcome)
            assert outcome.code is None, (name, outcome)
            received = echo.received()[-1]
            assert (received.server, received.tool) == ("notes", "echo"), name
            assert json.loads(received.arguments_json) == json.loads(_ARGUMENTS), name
    # Envoy forwards the body untouched; agentgateway re-encodes the params it parsed (`1.50` arrives as `1.5`).
    assert echo.received()[-1].arguments_json == _ARGUMENTS
