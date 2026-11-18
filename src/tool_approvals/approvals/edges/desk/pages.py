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
from importlib.resources import files
from typing import Final

from jinja2 import Environment, PackageLoader

from tool_approvals.approvals.app.answers import ApprovalDetail

_PACKAGE: Final = "tool_approvals.approvals.edges.desk"

STYLESHEET: Final = files(_PACKAGE).joinpath("desk.css").read_bytes()
_HIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})
_COMBINING: Final = "Mn"  # a nonspacing mark: hidden only where nothing visible carries it
_KEPT: Final = frozenset("\n")
_TITLE: Final = "Review a tool call"


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


def message_page(title: str, message: str, *, stylesheet: str) -> str:
    return _MESSAGE.render(title=title, message=message, stylesheet=stylesheet)
