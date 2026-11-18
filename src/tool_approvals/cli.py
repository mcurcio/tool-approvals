"""The `tool-approvals` command: one subcommand per service command, all reading one configuration file.

    tool-approvals [--config FILE] gate | wait | approvals serve | approvals migrate | config check
    tool-approvals credentials issue

`--config` defaults to $TOOL_APPROVALS_CONFIG. Exit 0 ok, 1 runtime failure, 2 configuration refused (every
problem listed on stderr). Each service command builds its own view of the file and hands it to that service's
composition root; this module composes and computes nothing else.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from tool_approvals.approvals import main as approvals_main
from tool_approvals.auth.registry import issue_bearer
from tool_approvals.config.errors import ConfigError
from tool_approvals.config.file import ConfigFile, load
from tool_approvals.config.views import approvals_view, gate_view, migrate_view, wait_view
from tool_approvals.gate import main as gate_main
from tool_approvals.wait import main as wait_main

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1  # an uncaught error: Python's own exit status
EXIT_CONFIG_REFUSED: Final = 2
CONFIG_ENV: Final = "TOOL_APPROVALS_CONFIG"

type _Command = Callable[[argparse.Namespace], int]


def _config(args: argparse.Namespace) -> ConfigFile:
    path: str | None = args.config
    if not path:
        raise ConfigError([f"no configuration file: pass --config or set {CONFIG_ENV}"])
    return load(Path(path))


def _serve[V](
    view_of: Callable[[ConfigFile, Mapping[str, str]], V], run: Callable[[V], Coroutine[Any, Any, int]]
) -> _Command:
    def command(args: argparse.Namespace) -> int:
        view = view_of(_config(args), os.environ)
        return asyncio.run(run(view))

    return command


def _config_check(args: argparse.Namespace) -> int:
    _config(args)
    print("configuration ok")
    return EXIT_OK


def _credentials_issue(args: argparse.Namespace) -> int:
    bearer, sha256_hex = issue_bearer()
    print(bearer)
    print(f"sha256:{sha256_hex}")
    return EXIT_OK


def _add(subparsers: Any, name: str, help_text: str, command: _Command) -> None:
    sub: argparse.ArgumentParser = subparsers.add_parser(name, help=help_text, description=help_text)
    sub.set_defaults(command=command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tool-approvals", description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--config", default=os.environ.get(CONFIG_ENV), help=f"configuration file (default: ${CONFIG_ENV})"
    )
    commands = parser.add_subparsers(title="commands", required=True)
    _add(commands, "gate", "serve the gate: the Check API and the gateway hooks", _serve(gate_view, gate_main.run))
    _add(commands, "wait", "serve the wait server (MCP)", _serve(wait_view, wait_main.run))
    approvals = commands.add_parser("approvals", help="the Approvals service").add_subparsers(required=True)
    _add(approvals, "serve", "serve Approvals and the desk", _serve(approvals_view, approvals_main.serve))
    _add(approvals, "migrate", "migrate Approvals' database", _serve(migrate_view, approvals_main.migrate))
    config = commands.add_parser("config", help="the configuration file").add_subparsers(required=True)
    _add(config, "check", "check the file's structure and cross-section rules (no secret resolved)", _config_check)
    credentials = commands.add_parser("credentials", help="service credentials").add_subparsers(required=True)
    _add(credentials, "issue", "print a new bearer, then the sha256 line the registry stores", _credentials_issue)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command: _Command = args.command
    try:
        return command(args)
    except ConfigError as refused:
        for problem in refused.problems:
            print(f"config: {problem}", file=sys.stderr)
        return EXIT_CONFIG_REFUSED


if __name__ == "__main__":
    sys.exit(main())
