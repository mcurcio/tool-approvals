# The desk and OIDC step-up

The desk is the built-in page where a person reviews one Approval and answers it. It is served by Approvals on
`approvals.listeners.approvers`, signs people in with your OpenID Provider, and answers as the native surface
`approvals.desk.surface`. Agents never reach it: keep that listener off every network an agent can reach.

**Built now:** one Approval per page, reached by its link. **Planned (milestone 3):** a list of waiting Approvals,
notices to approvers (CloudEvents webhooks, Web Push), and integrated answer surfaces. Until notices exist, the
link has to reach approvers some other way; it is `<public_url>/approvals/<ticket>`.

## A person answering

```
GET /approvals/T ─ no sign-in ─► /login?ticket=T ─► IdP (plain sign-in: who are you?) ─► /callback
   └ an approver of T's requirement, but short of its level or sign-in age
        ─► /login?ticket=T ─► IdP with acr_values and max_age ─► /callback ─► the review page
POST /approvals/T  (approve | refuse, optional comment, the shown digest, the form token) ─► recorded ─► the page
```

1. **Who is it?** Without a sign-in the desk cannot tell whether the person may see the Approval at all, so it
   first signs them in plainly.
2. **Step up.** If they are in one of the requirement's approver groups but their sign-in is below its minimum
   level or older than its `max_sign_in_age`, the desk sends them back to the identity provider once, asking for
   `acr_values` (every `acr` of the requirement's minimum level, space-separated) and `max_age` (its
   `max_sign_in_age` in seconds). A sign-in that asked for the requirement and still falls short is refused, never
   asked for again in a loop.
3. **Review.** The page shows the principal, server, tool and arguments, the agent's words (marked as not
   verified), the requirement, the answer deadline and the state. The arguments appear twice: the exact bytes the
   approval covers (what counts), and the same value re-rendered as indented JSON with sorted keys, so escapes or
   layout in the bytes cannot hide what the value is. Everything the agent supplied is escaped text; control and
   direction-changing characters, and a combining mark at the start of a line or after whitespace, are shown as
   visible escapes. The review and the answer are sent with `Cache-Control: no-store`.
4. **Answer.** Approve or refuse, with an optional comment; on a refusal the agent reads it as a quotation. The form carries
   the digest of what the page showed and a form token; the answer counts only if both still match.

The requirement used is the stricter of the one the Approval was asked under and the one configured now.

| Refusal | HTTP |
|---|---|
| Unknown ticket | 404 |
| The digest no longer matches, already answered, or past the answer deadline | 409 |
| Not an approver; surface not allowed; level too low or above the surface's cap; sign-in too old; a form token from another page | 403 |
| The identity provider did not answer | 502 |
| The answer could not be stored just now | 503 |

## What the desk checks in the ID token

The desk uses the authorization code flow with PKCE (S256), a `nonce` and `state`, scope `openid`, and
authenticates to the token endpoint with `idp.client_id` and `idp.client_secret`. Authlib verifies the ID token's
signature (the provider's published keys), expiry and nonce; the desk then requires, in order:

| Claim | Rule |
|---|---|
| `iss` | Equal to `idp.issuer` |
| `aud` | `idp.client_id` (alone) |
| `azp` | If present, `idp.client_id` |
| `sub` | Present: it is the approver's id |
| `acr` | One of the `acr` values of a level in `assurance`: that is the sign-in's level |
| `auth_time` | Present and not in the future |
| `idp.groups_claim` (default `groups`) | A list of strings: the approver's groups (none otherwise) |

The **sign-in moment** is the earlier of `auth_time` and the moment the desk received the token, and an answer
counts only while it is at most `max_sign_in_age` old. There is no allowance for clock skew: a skew allowance
would let an older sign-in count.

## Identity provider requirements

- A confidential client with the redirect URI `<public_url>/callback` and discovery at
  `<issuer>/.well-known/openid-configuration`.
- `acr`, `auth_time` and the groups claim in the **ID token** for scope `openid` alone: the desk asks for no
  other scope and does not call the UserInfo endpoint.
- **`acr_values` honoured** with a stronger sign-in when asked, and `acr` set to the level actually met.
- **`max_age` honoured**, and `auth_time` set to the moment of the sign-in ceremony that met the requested level.
  A provider that reports an earlier ceremony's time for a stronger `acr`, or a stronger `acr` it did not just
  perform, defeats the step-up.
- The groups claim's name equals the `claim` of every requirement's `approver_groups`
  ([configuration](configuration.md#assurance-floor-requirements-surfaces-what-an-answer-must-meet)).

Map each `acr` value your provider issues to a level in `assurance`, weakest first. The example names levels
`password` and `passkey`; use whatever your provider distinguishes.

## Sessions, keys and the page

| | |
|---|---|
| Session | A signed cookie, `desk_session` (`Secure`, `HttpOnly`, `SameSite=Lax`), holding the sign-in: subject, groups, level and moment. Signed with HMAC-SHA-256 under `desk.session_key`; whoever holds that key can mint a qualifying session, so treat it as answer authority. Its `Max-Age` is the floor's `max_sign_in_age`: no older sign-in can answer anything |
| Form token | An HMAC with `desk.form_key` over the session, the ticket and the shown digest: the CSRF defence, and the binding of an answer to the page that showed it |
| Both keys | At least 32 bytes each, separate, and the same on every replica |
| HTTPS | The cookie is `Secure`, so serve `public_url` over HTTPS: a TLS-terminating proxy in front of the plain-HTTP approvers listener |
| Content-Security-Policy | `default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'` on every response |

Every answer is stored with the surface, the approver's subject, the group through which they qualified, the
level the surface vouched, and the sign-in moment.
