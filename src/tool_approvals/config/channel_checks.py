"""The rules between `channels`, `approvals.delivery` and `approvals.retention`, checked when the file is read,
before any secret is resolved. `config.checks.problems` runs them.

Each a problem string, every one reported:
- channel names unique: "channels: the channel <name> is named twice";
- at most one `web_push` channel: "channels: more than one web_push channel";
- a webhook `url` is an absolute http or https URL: "channels.<name>.url: not an absolute http or https URL";
- every Web Push `push_origins` entry is an https origin (scheme and host, optional port, no path):
  "channels.<name>.push_origins: <origin> is not an https origin";
- `vapid_subject` is a `mailto:` or `https:` URI: "channels.<name>.vapid_subject: not a mailto: or https: URI";
- with any channel, `approvals.retention` at least `approvals.delivery.give_up_after` (retention never deletes what a
  lagging channel is still owed): "approvals.retention: below approvals.delivery.give_up_after (a lagging channel
  would lose events)";
- `approvals.delivery.attempt_timeout` below `approvals.delivery.give_up_after`:
  "approvals.delivery.attempt_timeout: not below give_up_after".

`approvals serve` also refuses, once it resolves the secrets, a signing secret that is not `whsec_` and base64
(`config.views`) and a VAPID key that is not a PEM P-256 private key (`approvals.main`).
"""

from collections import Counter
from collections.abc import Iterator
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, urlsplit

from tool_approvals.config.sections import WebhookChannel

if TYPE_CHECKING:
    from tool_approvals.config.file import ConfigFile


def problems(cfg: "ConfigFile") -> Iterator[str]:
    """Every channel and delivery problem in `cfg`."""
    channels = cfg.channels or ()
    counts = Counter(channel.name for channel in channels)
    for name in sorted(name for name, count in counts.items() if count > 1):
        yield f"channels: the channel {name!r} is named twice"
    if sum(channel.kind == "web_push" for channel in channels) > 1:
        yield "channels: more than one web_push channel"
    for channel in channels:
        where = f"channels.{channel.name}"
        if isinstance(channel, WebhookChannel):
            if not _http_url(channel.url):
                yield f"{where}.url: not an absolute http or https URL"
            continue
        for origin in channel.push_origins:
            if not _https_origin(origin):
                yield f"{where}.push_origins: {origin!r} is not an https origin"
        if not _contact(channel.vapid_subject):
            yield f"{where}.vapid_subject: not a mailto: or https: URI"
    approvals = cfg.approvals
    if approvals is None:
        return
    delivery = approvals.delivery
    if channels and approvals.retention < delivery.give_up_after:
        yield "approvals.retention: below approvals.delivery.give_up_after (a lagging channel would lose events)"
    if delivery.attempt_timeout >= delivery.give_up_after:
        yield "approvals.delivery.attempt_timeout: not below give_up_after"


def _absolute(url: str) -> SplitResult | None:
    """The parts of an absolute URL with a host, a valid port and no whitespace, or None."""
    if any(character.isspace() for character in url):
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port  # ValueError for a port that is not a number in range
    except ValueError:
        return None
    return parts if parts.hostname else None


def _http_url(url: str) -> bool:
    parts = _absolute(url)
    return parts is not None and parts.scheme.lower() in {"http", "https"}


def _https_origin(origin: str) -> bool:
    parts = _absolute(origin)
    if parts is None or parts.scheme.lower() != "https" or parts.username is not None:
        return False
    return not (parts.path or parts.query or parts.fragment or origin.endswith(("?", "#")))


def _contact(subject: str) -> bool:
    scheme, _, rest = subject.partition(":")
    if scheme.lower() == "mailto":
        return bool(rest) and not any(character.isspace() for character in subject)
    return scheme.lower() == "https" and _http_url(subject)
