# The audit report

Operators read every change Approvals has kept, in commit order, from the admin listener. It is the pull
counterpart of an audit sink ([channels](channels.md)): the same records, read when you want them.

```
GET /v1/audit?after=<id>
```

| | |
|---|---|
| Listener | `approvals.listeners.admin` (optional; needs `tls`) |
| Credential | A registry bearer of kind `operator` granted `admin.audit` ([configuration](configuration.md#registry-who-may-call-whom)) |
| `after` | The last id already read; default `0`. A non-negative integer, else 400 |
| Reply | 200, `Content-Type: application/jsonl`: one `AuditRecord` per line, ids above `after`, ascending, at most 1000 lines |

No credential is 401; a credential without the `admin.audit` grant (a gateway's, a surface's) is 403. A store that
does not answer is 503: try again.

## A record

Each line is the proto3 JSON of `tool_approvals.contract.audit.v1.AuditRecord`
(`proto/tool_approvals/contract/audit/v1/audit.proto`):

```json
{"id":"4183","time":"2026-01-01T12:03:10.512Z","type":"approval.answered","data":{"ticket":"Zp3Q0m8bV1wqk7cT2hLx9nA4","principal":"agent-1","server":"files","tool":"delete","requirement":"reviewed","approverGroups":[{"claim":"groups","value":"approvers"}],"actor":"approver:alice via desk","answered":{"verdict":"VERDICT_APPROVE","surface":"desk","approver":"alice","qualifyingGroup":{"claim":"groups","value":"approvers"},"vouchedAssurance":"passkey","signInMoment":"2026-01-01T12:02:41Z","answeredAt":"2026-01-01T12:03:10.512Z"}}}
```

| Field | Value |
|---|---|
| `id` | The event id (a decimal string, as proto3 JSON writes 64-bit integers) |
| `time` | When the change was committed |
| `type` | `approval.waiting`, `approval.worded`, `approval.answered`, `approval.used` or `approval.expired`: the type an audit sink's CloudEvent carries |
| `data` | The `AuditEvent` an audit sink receives for the same event; `answered` only on `approval.answered` |

The call's arguments, the agent's words and the approver's comment are never in the report.

## Reading it all

Read pages until one is short:

```sh
after=0
while :; do
  page=$(curl -sf --cacert ca.pem -H "Authorization: Bearer $AUDIT_BEARER" \
    "https://approvals.example.com:8447/v1/audit?after=$after")
  [ -n "$page" ] && printf '%s\n' "$page"
  [ "$(printf '%s' "$page" | grep -c '')" -lt 1000 ] && break
  after=$(printf '%s\n' "$page" | tail -n 1 | jq -r .id)
done
```

Ids rise in commit order (a commit holds a lock while it writes its events), so a page read after id *n* never
misses an event with a lower id committed later: keep the last id you read and continue from it.

The report holds what Approvals still keeps: an ended Approval and its events are deleted `approvals.retention`
after it ended (default 90 days). For a longer trail, configure an audit sink, or read the report more often than
the retention.
