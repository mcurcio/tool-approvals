# The desk and OIDC step-up

The desk is the built-in page where a person reviews one Approval and answers it. It is served by Approvals on
`approvals.listeners.approvers`, signs people in with your OpenID Provider, and answers as the native surface
`approvals.desk.surface`. Agents never reach it: keep that listener off every network an agent can reach.

**Built now:** one Approval per page, reached by its link (`<public_url>/approvals/<ticket>`); the list of the
Approvals waiting for the signed-in approver (`<public_url>/approvals`); and, when a Web Push channel is
configured, registering a browser for push notices from that list. Notices to approvers by CloudEvents webhooks
and the Web Push sending itself are configured as channels ([configuration](configuration.md)).

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

## The list

`GET /approvals` shows every Waiting Approval the signed-in person may answer at the desk, earliest answer deadline
first, at most 100. `GET /` redirects to it. Each row links to the Approval's review and shows the principal,
server, tool, requirement and answer deadline, as escaped text with control and direction-changing characters
shown as visible escapes; never the arguments or the agent's words.

An Approval is listed when it is Waiting and its effective requirement (the stricter of the one it was asked under
and the one configured now) allows the desk and names one of the person's groups: the same rule the review
applies. A requirement changed in configuration to drop a group, or to disallow the desk, takes its Approvals off
the list at once; an answered or expired Approval is never listed.

The list answers nothing, so it needs a sign-in but no step-up: without one, the desk signs the person in plainly
(`/login?next=list`, no `acr_values` or `max_age`) and comes back to the list. A review opened from it asks for
the requirement's level as usual. The list is sent with `Cache-Control: no-store`.

## Push notices from the list

With a Web Push channel configured, the list page offers **Notify this browser** (and **Stop notifying this
browser**). It loads one first-party script, `/desk.js`, which registers the service worker `/desk-sw.js`, asks
the browser for a push subscription with the channel's VAPID public key, and sends it to the desk. The key and the
other values the script needs are data attributes on the page; there is no inline script. The service worker shows
a notification for `approval.waiting` with a link to the review, replaces it (the same tag: the ticket) for
`approval.resolved`, and opens the link on a click; a link that is not the desk's own opens the list instead.

| Request | Body | Answer |
|---|---|---|
| `POST /push/subscriptions` | JSON: `endpoint`, `keys.p256dh`, `keys.auth` (base64url), `form_token` | 204 kept; 400 malformed; 403 not an approver, an endpoint outside the push origins, or a bad token; 415 not `application/json`; 503 not stored just now |
| `DELETE /push/subscriptions` | JSON: `endpoint`, `form_token` | 204 removed; 404 not a subscription this approver registered; 403 a bad token; 415; 503 |

- **Only an approver registers.** The sign-in must hold a group some requirement names; the subscription keeps
  the approver's subject and only those groups, which decide which notices it receives.
- **Only the push origins.** The endpoint must be an `https` URL of at most 2048 characters that starts with one
  of the channel's `push_origins` followed by `/`. `https://push.example.com.attacker.test/...` does not match
  `https://push.example.com`.
- **Well-formed keys.** `p256dh` is an uncompressed P-256 point (65 bytes, starting `0x04`); `auth` is 16 bytes.
- **CSRF.** The request must be `Content-Type: application/json` and carry the page's push token, an HMAC with
  `desk.form_key` over the session and the word `push`.
- Registering an endpoint again replaces its subscription; an approver removes only their own.

Without a Web Push channel these routes and both scripts answer 404, and the list page links no script.

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
| Content-Security-Policy | `default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'` on every response. The review page holds no script. The push scripts also need `script-src 'self'; worker-src 'self'; connect-src 'self'`; until the policy adds them, browsers refuse `/desk.js` and the push offer does nothing |

Every answer is stored with the surface, the approver's subject, the group through which they qualified, the
level the surface vouched, and the sign-in moment.
