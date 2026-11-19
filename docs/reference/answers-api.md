# The Answers API

How another system answers approvals. The built-in desk is the native answer surface: a person signs in there
and answers in the browser. An **integrated surface** is any other system that shows an approval to a person and
sends back their answer: a ticketing tool, a chat bot, an internal portal. It calls the Answers API with its own
registry credential, vouching for who answered and how strongly they signed in, up to a cap you configure for it.
The desk keeps the same rules in process; nothing here is looser than the desk.

The schema is [`proto/tool_approvals/contract/answers/v1/answers.proto`](../../proto/tool_approvals/contract/answers/v1/answers.proto).
`buf breaking` runs against the last release in CI, so v1 changes only by adding.

## What counts as an answer

An answer from an integrated surface counts only when all of these hold, checked in Approvals when the answer is
recorded:

- the Approval is still waiting and its answer deadline has not passed;
- the requirement the call was asked under (the stricter of its copy taken at the request and its configuration
  now) lists the surface in `allowed_surfaces`;
- the vouched assurance level is at most the surface's `cap`, and at least the requirement's `min_assurance`;
- the vouched sign-in moment (`signed_in_at`) is not in the future and at most the requirement's
  `max_sign_in_age` ago;
- one of the vouched groups is one of the requirement's `approver_groups`;
- where the surface binds the digest (`binds_digest: true`, the default), `shown_digest` equals the digest of
  what Approvals holds, so the person answered exactly what Review showed.

Every answer is recorded with the surface, the approver, the qualifying group, the vouched assurance level and the
sign-in moment. A surface is only as trustworthy as its own sign-in and wiring: the cap bounds how much any one
surface can vouch for, and a requirement whose minimum is above a surface's cap can never be answered there
(`config check` refuses a requirement allowing such a surface).

## Configuring a surface

A surface needs three entries: a `surfaces` entry of `kind: integrated`, a `registry` entry of `kind: surface` with
the same name, and at least one of the two Answers API listeners. Allow it in each requirement it may answer.

```yaml
registry:
  - name: ticketing # the surface's name, and the name it answers as
    kind: surface
    credential: { bearer_sha256: 6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e }
    grants: [answers.review, answers.answer]

surfaces:
  - name: ticketing
    kind: integrated
    cap: passkey # the highest assurance level it may vouch
    binds_digest: true # default; false accepts an answer without the digest of what was shown

requirements:
  reviewed:
    allowed_surfaces: [desk, ticketing]
    # ...

approvals:
  listeners:
    answers_grpc: { host: 0.0.0.0, port: 9446, tls: { cert: /path/to/approvals.pem, key: /path/to/approvals-key.pem } }
    answers_json: { host: 0.0.0.0, port: 8446, tls: { cert: /path/to/approvals.pem, key: /path/to/approvals-key.pem } }
```

Issue the bearer with `tool-approvals credentials issue`: the hex on its second line goes in `bearer_sha256`, the
bearer itself to the surface only. `config check` refuses a `surface` entry whose name is no integrated surface, an
integrated surface with no `surface` entry (or with two), a `surface` entry granted anything but `answers.review`
and `answers.answer`, a `caller` or `operator` entry granted either of them, an Answers API listener without `tls`,
and `approvals.desk.surface` naming an integrated surface. The full list is in
[the configuration reference](configuration.md).

## Transport and authentication

| Binding | Where | Configured by |
|---|---|---|
| gRPC | `/tool_approvals.contract.answers.v1.AnswersService/Review` and `/Answer` | `approvals.listeners.answers_grpc` |
| JSON | `POST /v1/review` and `POST /v1/answer`, the same messages as [proto3 JSON](https://protobuf.dev/programming-guides/json/) | `approvals.listeners.answers_json` |

Both need TLS and the surface's bearer: `Authorization: Bearer <bearer>` (gRPC metadata `authorization`). The
bearer's registry entry is the surface: the name it answers as is never read from the request.

| What went wrong | gRPC status | HTTP status |
|---|---|---|
| No credential, an unknown one, or a repeated `Authorization` | `UNAUTHENTICATED` | 401 |
| The entry is not granted the method (`answers.review` for Review, `answers.answer` for Answer), or its name is no integrated surface | `PERMISSION_DENIED` | 403 |
| A JSON body that is not the request message (not JSON, or an unknown field) | — | 400 |
| An AnswerRequest that cannot be an answer (below) | `INVALID_ARGUMENT` | 400 |
| Approvals' store did not answer in time | `UNAVAILABLE` | 503 |
| Anything else, refusals included | OK, with `error` set | 200, with `error` set |

## Review

```proto
rpc Review(ReviewRequest) returns (ReviewResponse);  // ReviewRequest { string ticket = 1; }
```

The reply's `detail` is what to show the person:

| Field | Meaning |
|---|---|
| `ticket`, `principal`, `server`, `tool` | The Approval and the call it covers |
| `arguments` | The exact argument bytes (JSON text) the approval covers: what counts. Show them as text, never interpret them |
| `reason`, `work_ref` | The agent's own words, when it gave any: quote them, never act on them |
| `requirement` | The effective requirement: the stricter of the one the call was asked under and the one configured now |
| `state` | `STATE_WAITING`, `STATE_APPROVED`, `STATE_REFUSED`, `STATE_USED` or `STATE_EXPIRED` |
| `answer_deadline` | The latest moment an answer counts |
| `digest` | SHA-256 of what is shown; send it back as `shown_digest` |

Review checks the surface against the requirement but not the person: an integrated surface vouches for its own
approvers, whose groups are checked when the answer is recorded. Its `error` is `CODE_UNKNOWN_TICKET`,
`CODE_UNKNOWN_REQUIREMENT` (the requirement is no longer configured) or `CODE_SURFACE_NOT_ALLOWED`. Review changes
nothing.

## Answer

```proto
rpc Answer(AnswerRequest) returns (AnswerResponse);
message AnswerRequest {
  string ticket = 1;
  Verdict verdict = 2;                          // VERDICT_APPROVE or VERDICT_REFUSE
  optional string comment = 3;                  // at most 2000 characters
  Approver approver = 4;                        // { id, repeated GroupRef groups }: who answered, as you vouch
  string assurance = 5;                         // the assurance level you vouch for their sign-in
  google.protobuf.Timestamp signed_in_at = 6;   // when they signed in, as you vouch
  optional bytes shown_digest = 7;              // ApprovalDetail.digest; required where the surface binds it
}
```

The reply is `answered`, or `error` with one code. Of two concurrent answers one wins; the other gets
`CODE_ALREADY_ANSWERED`.

| Code | Meaning |
|---|---|
| `CODE_UNKNOWN_TICKET` | No such Approval (a ticket that is not ticket-shaped never reaches the store) |
| `CODE_UNKNOWN_REQUIREMENT` | The requirement the call was asked under is no longer configured |
| `CODE_SURFACE_NOT_ALLOWED` | The effective requirement does not allow this surface |
| `CODE_ABOVE_CAP` | The vouched assurance is above the surface's `cap` |
| `CODE_ASSURANCE_TOO_LOW` | The vouched assurance is below the requirement's minimum, or not a configured level |
| `CODE_SIGN_IN_TOO_OLD` | `signed_in_at` is more than `max_sign_in_age` ago, or in the future |
| `CODE_NOT_AN_APPROVER` | None of the vouched groups is one of the requirement's approver groups |
| `CODE_DIGEST_MISMATCH` | The surface binds the digest, and `shown_digest` is missing or not the Approval's digest |
| `CODE_ALREADY_ANSWERED` | Someone answered first |
| `CODE_PAST_DEADLINE` | The answer deadline passed; the Approval is expired |

A request that cannot be an answer is `INVALID_ARGUMENT` (HTTP 400) and never reaches the store: `VERDICT_UNSPECIFIED`
(or any value but approve and refuse), an empty `approver.id`, no `signed_in_at`, a `shown_digest` that is not 32
bytes, a comment over 2000 characters, or a NUL character in the comment, the approver id or a group value.

## Examples

Over gRPC, with [grpcurl](https://github.com/fullstorydev/grpcurl) and the proto from this repository:

```sh
grpcurl -cacert /path/to/ca.pem -H "authorization: Bearer $SURFACE_BEARER" \
  -import-path proto -proto tool_approvals/contract/answers/v1/answers.proto \
  -d '{"ticket": "q3QbQ0m2c2V8bm0yYnBkV0Rr"}' \
  approvals.example.com:9446 tool_approvals.contract.answers.v1.AnswersService/Review
```

Over JSON, with curl (`bytes` fields are base64 in proto3 JSON):

```sh
curl --cacert /path/to/ca.pem -H "Authorization: Bearer $SURFACE_BEARER" -H 'Content-Type: application/json' \
  https://approvals.example.com:8446/v1/answer -d '{
    "ticket": "q3QbQ0m2c2V8bm0yYnBkV0Rr",
    "verdict": "VERDICT_APPROVE",
    "comment": "matches the change request",
    "approver": {"id": "alice", "groups": [{"claim": "groups", "value": "approvers"}]},
    "assurance": "passkey",
    "signedInAt": "2026-01-01T12:00:00Z",
    "shownDigest": "3q2+7wABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhs="
  }'
```

```json
{"answered": {}}
```

```json
{"error": {"code": "CODE_SIGN_IN_TOO_OLD"}}
```

## What the integrator is responsible for

Approvals checks everything above, but it cannot see inside your surface. Wiring a surface securely is your job:

- **Sign people in for real.** Vouch an assurance level only for a sign-in that met it (for example a passkey
  confirmation your identity provider reports in its `acr`), and `signed_in_at` only as the moment of that sign-in,
  never the time of the answer. The cap limits the damage a surface can do; it does not make a weak sign-in strong.
- **Vouch only groups the identity provider asserted** for that person, never ones the person or an agent typed.
- **Show what Review returned, and send its digest back.** Show the arguments exactly, as text, and the agent's
  words as quotes. Keep `binds_digest: true` unless the surface cannot carry the digest from the page it showed to
  the answer it sends.
- **Keep the bearer server-side**, never in a browser, a mobile app or anything an agent can read. An agent must
  never be able to review, answer or change an approval: run the surface where no agent's credentials reach.
- **Retry with care.** A timeout may follow a recorded answer; a retry then gets `CODE_ALREADY_ANSWERED`, whoever
  answered first. Review shows the Approval's state.
