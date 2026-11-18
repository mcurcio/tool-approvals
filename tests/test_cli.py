"""The command's surface: every subcommand the entry points name parses and reaches its handler."""

import pytest

from tool_approvals.cli import EXIT_OK, build_parser, main


def test_help_exits_ok(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--help"])
    assert stopped.value.code == EXIT_OK
    out = capsys.readouterr().out
    for command in ("gate", "wait", "approvals", "config", "credentials"):
        assert command in out


@pytest.mark.parametrize(
    "argv",
    [
        ["gate"],
        ["wait"],
        ["approvals", "serve"],
        ["approvals", "migrate"],
        ["config", "check"],
        ["credentials", "issue"],
    ],
)
def test_every_command_has_a_handler(argv: list[str]) -> None:
    args = build_parser().parse_args(["--config", "unused.yaml", *argv])
    assert callable(args.command)


def test_a_group_without_its_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["approvals"])
    assert stopped.value.code == 2  # argparse's usage status
