"""A person at the desk: signs in through the scripted provider, opens one Approval's review and answers it.
The desk's tests build on it, and it is the e2e stack's desk driver."""

from collections.abc import Mapping
from typing import Final, Literal

import httpx

from tests.fixtures.oidc_provider import Browser, ScriptedIdp, SignInScript, hidden_fields

ANSWER_FIELDS: Final = ("digest", "form_token")  # the review form's hidden fields


def answer_fields(page: str) -> dict[str, str]:
    """The review form's hidden fields on `page` (none when it shows no form)."""
    return {name: value for name, value in hidden_fields(page).items() if name in ANSWER_FIELDS}


class DeskPerson:
    def __init__(self, http: httpx.AsyncClient, idp: ScriptedIdp, desk_url: str) -> None:
        self.browser = Browser(http, idp)
        self._idp = idp
        self._desk = desk_url.rstrip("/")

    def review_url(self, ticket: str) -> str:
        return f"{self._desk}/approvals/{ticket}"

    async def open(self, ticket: str, *sign_ins: SignInScript) -> httpx.Response:
        """Open the review, signing in as `sign_ins` script (the provider's current script when none); the page
        the browser ends on."""
        if sign_ins:
            self._idp.script(*sign_ins)
        return await self.browser.visit(self.review_url(ticket))

    async def post(self, ticket: str, verdict: str, fields: Mapping[str, str], comment: str | None = None) -> int:
        """POST an answer with `fields` (the review form's hidden fields); the response's status."""
        data = {**fields, "verdict": verdict, **({"comment": comment} if comment is not None else {})}
        return (await self.browser.submit(self.review_url(ticket), data)).status_code

    async def answer(
        self,
        ticket: str,
        verdict: Literal["approve", "refuse"],
        *sign_ins: SignInScript,
        comment: str | None = None,
    ) -> int:
        """Sign in, open the review and answer: the POST's status (303 when recorded), or the status of the page
        that stopped the flow (a refused sign-in, a review without the form)."""
        page = await self.open(ticket, *sign_ins)
        fields = answer_fields(page.text)
        if page.status_code != 200 or len(fields) != len(ANSWER_FIELDS):
            return page.status_code
        return await self.post(ticket, verdict, fields, comment)
