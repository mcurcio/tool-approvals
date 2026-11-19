"""The rules between `channels`, `approvals.delivery` and `approvals.retention`, checked when the file is read,
before any secret is resolved (milestone 3, event delivery). `config.checks.problems` runs them.

To be checked (each a problem string, every one reported):
- channel names unique; at most one `web_push` channel;
- a webhook `url` is an absolute http or https URL;
- every Web Push `push_origins` entry is an https origin (scheme and host, optional port, no path);
- `vapid_subject` is a `mailto:` or `https:` URI;
- with any channel, `approvals.retention` at least `approvals.delivery.give_up_after` (retention never deletes what a
  lagging channel is still owed);
- `approvals.delivery.attempt_timeout` below `approvals.delivery.give_up_after`.
"""

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool_approvals.config.file import ConfigFile


def problems(cfg: "ConfigFile") -> Iterator[str]:
    """Every channel and delivery problem in `cfg`."""
    yield from ()
