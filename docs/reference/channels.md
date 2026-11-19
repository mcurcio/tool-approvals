# Channels: events Approvals sends

Approvals tells other systems what happens to each Approval: a chat bot or pager can announce that an approval is
waiting, an archive can keep every change as an audit trail, and approvers' browsers can show a notification. Each
destination is a **channel** in the `channels` section of the configuration ([configuration](configuration.md#channels-where-approvals-sends-events)).

| Kind | Receives | Data |
|---|---|---|
| `webhook` with `events: notices` | `approval.waiting`, `approval.resolved` | `ApprovalNotice` |
| `webhook` with `events: audit` (an audit sink) | every event | `AuditEvent` |
| `web_push` | `approval.waiting`, `approval.resolved`, to each browser an approver registered on the desk | `ApprovalNotice` |

No channel ever receives the call's arguments, the agent's words or the approver's comment. Those stay in
Approvals and are shown only on the desk and through the Answers API.

## Event types

Every committed change to an Approval is one event, with an id that rises in commit order.

| What happened | `notices`, Web Push | `audit` |
|---|---|---|
| A gated call asked for approval | `approval.waiting` | `approval.waiting` |
| The agent added its words | (not sent) | `approval.worded` |
| An approver answered yes or no | `approval.resolved` | `approval.answered` |
| The approved call ran | (not sent) | `approval.used` |
| The Approval expired unanswered or unused | `approval.resolved` | `approval.expired` |

## The CloudEvent

Each event is a [CloudEvents 1.0](https://github.com/cloudevents/spec) event in structured JSON mode,
`Content-Type: application/cloudevents+json`:

```json
{
  "specversion": "1.0",
  "id": "4182",
  "source": "https://approvals.example.com",
  "type": "approval.waiting",
  "subject": "Zp3Q0m8bV1wqk7cT2hLx9nA4",
  "time": "2026-01-01T12:00:00.123456Z",
  "datacontenttype": "application/json",
  "data": {
    "ticket": "Zp3Q0m8bV1wqk7cT2hLx9nA4",
    "principal": "agent-1",
    "server": "files",
    "tool": "delete",
    "approverGroups": [{ "claim": "groups", "value": "approvers" }],
    "deskLink": "https://approvals.example.com/approvals/Zp3Q0m8bV1wqk7cT2hLx9nA4"
  }
}
```

| Field | Value |
|---|---|
| `id` | The event id, as a decimal string. The same on every retry |
| `source` | `approvals.desk.public_url` |
| `subject` | The Approval's ticket |
| `time` | When the change was committed, RFC 3339 in UTC |
| `data` | The proto3 JSON of `tool_approvals.contract.events.v1.ApprovalNotice` (notices, Web Push) or `AuditEvent` (audit); the schema is `proto/tool_approvals/contract/events/v1/events.proto` |

`ApprovalNotice` carries routing data only: ticket, principal, server, tool, the approver groups of the requirement
the Approval was asked under (sorted), and `deskLink`, the desk page that shows and answers it. A receiver routes
a notice to the people in those groups and links them to the desk.

`AuditEvent` adds the requirement's name and `actor`, who made the change (`principal:<p> via <caller>`,
`approver:<subject> via <surface>`, `deadline`, `requirement`, ...). On `approval.answered` it also carries
`answered`: the verdict, the surface, the approver, the group that qualified them, the assurance the surface
vouched, the sign-in moment and, when known, the moment of the answer.

## Verifying a webhook

Webhooks are signed per [Standard Webhooks](https://www.standardwebhooks.com/). Each request carries:

| Header | Value |
|---|---|
| `webhook-id` | The event id (the CloudEvent `id`) |
| `webhook-timestamp` | When this attempt was sent, in unix seconds |
| `webhook-signature` | `v1,` and the base64 HMAC-SHA256 of `<webhook-id>.<webhook-timestamp>.<body>` |

The key is the base64 after `whsec_` in the channel's `signing_secret`; generate one with, for example,
`echo "whsec_$(openssl rand -base64 32)"`. A receiver should:

1. compute the HMAC over the exact body bytes and compare it in constant time with each `v1,` value in
   `webhook-signature` (space-separated, so a key rotation can carry two);
2. refuse a `webhook-timestamp` far from its own clock (five minutes is usual), against replays;
3. answer 2xx once it has stored the event.

Any Standard Webhooks library verifies these requests.

## Order, retries and duplicates

- **At least once, in id order per channel.** Each channel has one cursor, the last event id it has finished.
  One replica at a time delivers a channel (it holds the channel's lock); the others take over if it stops.
- **Deduplicate by id.** A retry, or a takeover after a replica stops mid-send, sends an event again with the
  same `webhook-id`. Keep the highest id you have stored and ignore anything at or below it.
- **Order holds per sender only.** During a takeover two replicas can briefly overlap: order by id, not by arrival.

What each response does:

| Response | What happens |
|---|---|
| 2xx | Delivered; the next event follows |
| Timeout (`approvals.delivery.attempt_timeout`), connection error, 5xx, 429 | The channel **pauses** and the same event is sent again: after 1 s, doubling to at most 5 minutes |
| 401, 403, 404 | The channel pauses the same way and Approvals logs an ERROR naming the channel and the status: the URL or secret is wrong |
| Any other 4xx | The event is **dead-lettered** for this channel and delivery moves on |

While a channel is paused, an event older than `approvals.delivery.give_up_after` (default 24 hours) is
dead-lettered instead of sent, so a receiver that comes back after a long outage gets the recent events, not a day
of stale ones. A dead letter is a row in the `dead_letters` table (channel, event id, reason such as `http 400` or
`gave up after 24h`) and an ERROR log line. An Approval's dead letters are deleted with it when its retention ends;
`approvals.retention` must be at least `give_up_after` when any channel is configured, so retention never deletes
an event a lagging channel is still owed.

A new channel starts from the first event Approvals still keeps.

## Liveness

`GET /healthz` on the health listener answers 503 `not live: delivery <channel>` when the replica delivering a
channel has made no progress for 3 x `attempt_timeout` + 5 minutes while events are pending and the channel is not
paused. The orchestrator then restarts the process, its connection (and the channel's lock) closes, and another
replica takes the channel over.

## Web Push

With a `web_push` channel, the desk offers approvers to turn on notifications in their browser. The browser
subscribes with the push service its vendor runs, and the desk stores the subscription with the approver's groups.

- **Push origins.** `push_origins` lists the https origins of the push services you allow
  (for example `https://push.example.com`). A subscription whose endpoint is on another origin is refused when it
  is registered, and the origin is checked again before every send: nothing is ever sent anywhere else. List the
  push services of the browsers your approvers use.
- **VAPID.** `vapid_private_key` is a PEM P-256 private key (for example
  `openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt`); the desk gives browsers its
  public half. Each push carries a VAPID token for the endpoint's origin with `vapid_subject` (a `mailto:` or
  `https:` contact the push service can use). Changing the key invalidates every subscription.
- **What is sent.** The same CloudEvent JSON a `notices` webhook receives, encrypted for the browser
  (`aes128gcm`, RFC 8291). `TTL` is `give_up_after`, `Topic` is the ticket (a newer notice for the same Approval
  replaces one not yet shown), and `Urgency` is `high` for `approval.waiting`, `normal` otherwise.
- **Routing.** Each event goes to every subscription whose groups include one of the event's approver groups.
- **Responses.** 2xx is delivered; 404 or 410 means the browser unsubscribed, so the subscription is deleted;
  429, 5xx, a timeout or a connection error pauses the whole channel (every subscription gets the event again);
  any other 4xx dead-letters the event for the channel.
