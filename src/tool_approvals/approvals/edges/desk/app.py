"""The minimal desk: a person signs in with the IdP at the level the requirement asks, reviews one Approval and
answers it, as the configured native surface.

GET /login?ticket=: authorization code + PKCE S256, nonce, acr_values = the effective minimum level's acr values,
max_age = its max_sign_in_age; the verifier, nonce, state and ticket wait in the signed session cookie (Authlib's
state store) until the callback.
GET /callback: iss; aud and azp (if present) = client_id; nonce; acr on the scale; auth_time present and not in the
future; sign-in moment = min(auth_time, receipt). Session (signed, Secure, HttpOnly, SameSite=Lax): sid, sub,
groups, level, moment.
GET /approvals/{ticket}: the review when the session qualifies, else /login; words and arguments as escaped text.
POST /approvals/{ticket}: verdict, comment, digest, form_token → AnswerService.answer.
Every response: Content-Security-Policy "default-src 'none'; style-src 'self'; form-action 'self';
frame-ancestors 'none'; base-uri 'none'".
"""

from typing import Final

from starlette.applications import Starlette

from tool_approvals.approvals.app.answers import AnswerService
from tool_approvals.config.views import DeskView, IdpView
from tool_approvals.kernel.requirements import AssuranceScale

CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def desk_app(answers: AnswerService, desk: DeskView, idp: IdpView, scale: AssuranceScale) -> Starlette:
    raise NotImplementedError
