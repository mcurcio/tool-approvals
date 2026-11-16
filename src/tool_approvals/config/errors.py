"""The one error a configuration problem raises."""

from collections.abc import Sequence


class ConfigError(Exception):
    """The file, or a command's view of it, is refused. Every problem found is listed, not only the first."""

    problems: tuple[str, ...]

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))
