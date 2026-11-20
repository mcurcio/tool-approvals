"""The desk's pages: Jinja2 templates with autoescaping on, so whatever an agent wrote (its words, the call's
names and arguments) reaches the page only as escaped text, and only in element content, never in an attribute,
a URL or a script. Control, format and separator characters are shown as visible escapes (a right-to-left override
or a zero-width space would otherwise change what a person reads without showing up), and so is a combining mark
that starts a line or follows whitespace (it would otherwise sit on the character before it, unseen).

The arguments are shown twice: the exact bytes the approval covers, which are what counts, and beside them the
same value re-rendered as indented JSON with sorted keys, so escapes and layout in the bytes cannot hide what the
value is."""

import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Final

from jinja2 import Environment, PackageLoader

from tool_approvals.approvals.app.answers import ApprovalDetail
from tool_approvals.approvals.app.waiting import WaitingSummary

_PACKAGE: Final = "tool_approvals.approvals.edges.desk"

STYLESHEET: Final = files(_PACKAGE).joinpath("desk.css").read_bytes()
SCRIPT: Final = files(_PACKAGE).joinpath("desk.js").read_bytes()  # the push page's registration
SERVICE_WORKER: Final = files(_PACKAGE).joinpath("desk-sw.js").read_bytes()  # shows the push notices
_HIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})
_COMBINING: Final = "Mn"  # a nonspacing mark: hidden only where nothing visible carries it
_KEPT: Final = frozenset("\n")
_TITLE: Final = "Review a tool call"
_LIST_TITLE: Final = "Waiting for your answer"
_PUSH_TITLE: Final = "Notices in this browser"


def visible(text: str) -> str:
    """`text` with every control, format and line or paragraph separator character but the line feed written as
    a visible escape (`\\u202e`), and every combining mark at the start of a line or after whitespace too."""
    return "".join(_shown(char, before) for char, before in zip(text, ["\n", *text], strict=False))


def _shown(char: str, before: str) -> str:
    category = unicodedata.category(char)
    unsupported = category == _COMBINING and before.isspace()  # the text's start counts as a line's
    if char in _KEPT or (category not in _HIDDEN_CATEGORIES and not unsupported):
        return char
    point = ord(char)
    return f"\\u{point:04x}" if point <= 0xFFFF else f"\\U{point:08x}"


_ENVIRONMENT: Final = Environment(loader=PackageLoader(_PACKAGE), autoescape=True, auto_reload=False)
_ENVIRONMENT.filters["visible"] = visible
_REVIEW: Final = _ENVIRONMENT.get_template("review.html")
_MESSAGE: Final = _ENVIRONMENT.get_template("message.html")
_LIST: Final = _ENVIRONMENT.get_template("list.html")
_PUSH: Final = _ENVIRONMENT.get_template("push.html")


@dataclass(frozen=True, slots=True)
class PushOffer:
    """What the push page's script needs to register this browser for push notices: none of it agent-supplied."""

    script: str  # the URL of desk.js
    service_worker: str  # the URL of desk-sw.js
    service_worker_scope: str  # its scope: the script's own directory, which holds no review page and not the list
    subscriptions: str  # the URL of /push/subscriptions
    application_server_key: str  # the VAPID public key, base64url
    token: str  # the push token of this session


def review_page(detail: ApprovalDetail, *, stylesheet: str, action: str, digest: str, form_token: str | None) -> str:
    """The review of one Approval; with the answer form when `form_token` is given."""
    return _REVIEW.render(
        title=_TITLE,
        stylesheet=stylesheet,
        detail=detail,
        arguments=detail.call.arguments_json.decode(errors="replace"),
        rerendered=_rerendered(detail.call.arguments_json),
        action=action,
        digest=digest,
        form_token=form_token,
    )


def _rerendered(arguments_json: bytes) -> str:
    """The arguments as indented JSON with sorted keys; empty when the call has none."""
    if not arguments_json:
        return ""
    return json.dumps(json.loads(arguments_json), indent=2, sort_keys=True, ensure_ascii=False)


def list_page(rows: Sequence[WaitingSummary], *, stylesheet: str, review_base: str, push_page: str | None) -> str:
    """The Waiting Approvals the viewer may answer, each linking to `review_base`/<ticket>; with a link to
    `push_page` when given. No script."""
    return _LIST.render(
        title=_LIST_TITLE, stylesheet=stylesheet, rows=rows, review_base=review_base, push_page=push_page
    )


def push_page(push: PushOffer, *, stylesheet: str, list_page: str | None) -> str:
    """The push offer: the one page that loads a script (`push.script`), with a link back to `list_page` when
    given."""
    return _PUSH.render(title=_PUSH_TITLE, stylesheet=stylesheet, push=push, list_page=list_page)


def message_page(title: str, message: str, *, stylesheet: str) -> str:
    return _MESSAGE.render(title=title, message=message, stylesheet=stylesheet)
