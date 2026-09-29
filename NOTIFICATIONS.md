# ARGUS Discovery Notifications

ARGUS can push a notification to your phone (or any webhook) when it discovers a new
internship opportunity that is open for applications.  This is the **discovery**
path — it fires when a new role is ingested and found eligible, not when the
automation pipeline hits a blocker.

No PII (name, email, phone, CV text) ever leaves your machine.  Payloads contain
only employer, role title, deadline, and application URL.

## Quick start: push notifications to your phone (free, no account)

1.  Install the [ntfy](https://ntfy.sh/) app on your phone (Android / iOS).
2.  Open the app and tap **Subscribe to topic**.
3.  Pick a topic name, e.g. `argus-my-phone`.
4.  Set these environment variables before starting ARGUS:

    ```bash
    ARGUS_ENABLE_NOTIFICATIONS=true
    ARGUS_NTFY_TOPIC=argus-my-phone
    ```

    That's it.  The default server `https://ntfy.sh` is used automatically —
    no account, no sign-up.

5.  Start ARGUS as usual.  Opportunities discovered during a CSV import, Trackr
    sweep, or saved-HTML scan will now ping your phone.

## Deadline reminders

Each sweep also checks human-blocked applications (`NEEDS_USER`, `NEEDS_OA`)
whose next action is due within **3 days** (overdue included) and sends one
`deadline_approaching` reminder each, deep-linked to the exact application.
The same application/state/reason never notifies twice — in memory and in the
durable outbox — and the hourly rate limit still applies. Nothing is inferred:
only actions with a captured deadline are eligible.

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `ARGUS_ENABLE_NOTIFICATIONS` | `false` | Set to `true` to enable push |
| `ARGUS_NTFY_TOPIC` | `""` | ntfy topic name for your phone |
| `ARGUS_NTFY_SERVER` | `https://ntfy.sh` | ntfy server base URL |
| `ARGUS_NOTIFY_WEBHOOK_URL` | `""` | HTTP/HTTPS webhook URL (JSON POST) |
| `ARGUS_HERMES_NOTIFY_TARGET` | `""` | Hermes target: `telegram`, `slack`, etc. |
| `ARGUS_NOTIFY_BATCH_WINDOW_SECONDS` | `30` | Max seconds to batch human-attention events |
| `ARGUS_NOTIFY_RATE_LIMIT_PER_HOUR` | `6` | Max notification digests per hour |

## Example: ntfy via custom server

```bash
ARGUS_ENABLE_NOTIFICATIONS=true \
ARGUS_NTFY_TOPIC=my-private-alerts \
ARGUS_NTFY_SERVER=https://ntfy.example.com
```

## Example: webhook (Discord, Slack, custom)

```bash
ARGUS_ENABLE_NOTIFICATIONS=true \
ARGUS_NOTIFY_WEBHOOK_URL=https://hooks.example.com/webhook/argus
```

## Privacy guarantee

- **No candidate PII** (name, email, phone, CV content) is ever included in any
  notification payload.
- Discovery payloads contain only: employer, role title, deadline, application URL.
- All notification processing is fail-soft: a notification failure never blocks
  discovery or automation.