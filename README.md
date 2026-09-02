# Edge Serverless Functions

DigitalOcean serverless functions for Edge. All Intercom webhooks are
handled by a single `intercom/webhook` function that routes by topic.

## Intercom article draft bridge

`intercom-article-drafts/upload` is a small authenticated bridge for creating
and updating Intercom drafts. It supports three draft outcomes while keeping
publication, deletion, placement, scheduling, and arbitrary upstream calls
unreachable:

- create a new unpublished article draft;
- update an existing never-published draft; or
- stage unpublished changes over an existing published article while leaving
  its live version unchanged.

It deliberately reuses the existing Intercom app token already supplied to
`intercom/webhook`; that app must retain `Read and write articles` permission.

Create a new draft:

```json
{
  "operation": "create",
  "operation_id": "kb-occurrence-0123456789abcdef",
  "title": "How to reset 2FA",
  "description": "Recovery steps for Edge accounts",
  "body_markdown": "# How to reset 2FA\n\n..."
}
```

Send requests as `POST` with
`Authorization: Bearer $INTERCOM_DRAFT_BRIDGE_SECRET`.

Update a known article by its exact Intercom article ID:

```json
{
  "operation": "update",
  "operation_id": "kb-occurrence-0123456789abcdef",
  "article_id": "987654",
  "title": "How to reset 2FA safely",
  "description": "Current recovery steps for Edge accounts",
  "body_markdown": "# How to reset 2FA safely\n\n..."
}
```

The broker reads the current article state. A `draft` article is updated
through `PUT /articles/{id}` and read back as an unpublished draft. A
`published` article is updated through `PUT /articles/{id}/draft`, read back
through the staged-draft endpoint, and checked again to ensure the live article
did not change. A published article that already has staged unpublished changes
at either the initial read or the immediate version-bound prewrite read is
rejected with `409`. Any target with an existing publish or unpublish schedule
is likewise rejected before mutation, and every post-mutation readback must
remain unscheduled.

A human publishing or scheduling a never-published draft in the short window
between its initial read and update is an explicitly accepted operational race.
Because the general update intentionally omits `state`, a concurrent publish
can make the submitted title/body/description/author live before mandatory
readback detects the drift. The broker then returns a non-retryable
reconciliation response; it cannot roll that live overwrite back. The pipeline
serializes its own calls, and this residual human-action race is accepted for
the low-volume Review workflow.

Published staging has a second explicitly accepted human-action race. The
immediate prewrite GET binds live content, `updated_at`, schedule state, and
staged-draft state, which narrows the window substantially. Intercom exposes no
conditional-write token for `PUT /articles/{id}/draft`, however, so a human who
stages a revision after that final GET but before the PUT can have that pending
revision overwritten. The broker cannot eliminate or detect that sub-request
race atomically. Jared accepts it for this low-volume serialized workflow; the
operator must avoid editing the same article during an authorized pipeline run.

A successful response includes the exact Intercom article identity,
submitted-content hash, selected draft mode, and audit fields needed by the
caller and its post-draft review-link resolver:

```json
{
  "ok": true,
  "operation_id": "kb-occurrence-0123456789abcdef",
  "operation": "create",
  "result": "created",
  "article_id": "987654",
  "title": "How to reset 2FA",
  "before_state": null,
  "after_state": "draft",
  "state": "draft",
  "draft_mode": "new",
  "completed_at": "2026-08-24T22:00:00Z",
  "submitted_content_hash": "<sha256>"
}
```

For updates, `result` is `updated_draft` or `staged_draft`; the latter reports
`after_state: published_with_draft`, `state: published`, and
`has_unpublished_changes: true`. The broker never exposes a publish, delete,
placement, or scheduling operation, and rejects every request field outside its
small content-and-identity allowlist.

Each invocation starts a 30-second internal upstream deadline and uses short
connect/read-inactivity timeouts inside DigitalOcean's 45-second hard function
limit. The HTTP library's read timeout is inactivity-based, so DigitalOcean's
limit remains the final wall-clock stop for an adversarial slow-drip response.
A timeout, connection loss, 5xx, malformed success
response, or failed verification after a write returns
`reconciliation_required: true` and `retry_safe: false`. In particular, create
is not idempotent: a lost `POST /articles` response may have created a draft
without returning its ID. Never retry such an occurrence automatically; search
and reconcile the exact operation/title before deciding whether another write
is safe.

The public Articles API does not expose Knowledge Hub's private
`activeContentId`. The bridge therefore never guesses a review URL from
`article_id` or `content_id`. The local Knowledge Base workflow resolves
that ID after the mutation through an authenticated teammate-session search,
then inserts it into the fixed review URL template.

### Draft-broker deployment scope

Deploy the broker by its exact function path. Do not use the whole-project
deployment examples later in this README for a broker-only release; DigitalOcean
discovers immediate package subdirectories as actions.

```bash
doctl serverless deploy . \
  --remote-build \
  --env .env \
  --include intercom-article-drafts/upload
```

The broker package must contain only the `upload` action directory. Its tests
live at repository-root `tests/`, outside the deployable package.

## Webhook Handlers

### Lead-to-User Auto-Converter (DISABLED by default)

> ⚠️ **Server-side lead→user conversion is disabled by default and should stay
> that way unless you understand the tradeoff below.**
>
> Converting a lead to a user requires Intercom's merge API, which
> **permanently deletes the lead profile**. A customer's live Messenger session
> is bound to the lead's Intercom contact `id` (not their email or
> `external_id`), so the merge **orphans the session** — the next message fails
> with "Couldn't send." Intercom confirms this is expected behaviour of merge,
> and there is **no REST way to promote a lead to a user in place**.
>
> The correct fix is **client-side**: when the person logs in / provides an
> email, your app should call `Intercom('shutdown')` then boot with `user_id`
> + `email` (+ `user_hash` if you use Identity Verification). The Messenger then
> transitions the lead to a user natively and keeps the session alive.

Handles three webhook topics:

- **`contact.lead.created`** — lead created with an email
- **`contact.lead.added_email`** — email added to a lead that had none
- **`contact.email.updated`** — lead's email changed

**How it works:**

1. Intercom fires a webhook to the single endpoint
2. The router verifies the HMAC-SHA1 signature
3. The topic is matched and dispatched to the lead-to-user handler
4. If `LEAD_TO_USER_CONVERSION_ENABLED` is **not** truthy (the default), the
   handler logs and returns without touching Intercom — the lead stays a lead
   and keeps chatting.
5. Only if conversion is explicitly enabled does it search for an existing user
   (by the lead's id, then email), create one with email only if needed, merge
   the lead in, and reassign the lead's id onto the surviving user. **This
   breaks active Messenger sessions** (see warning above).
6. Returns 200 so Intercom does not retry

### Inbound Call Timezone Inference

Automatically infers a caller's timezone when an inbound call starts in
Intercom. Handles the **`call.started`** webhook topic and filters to
inbound calls only.

**How it works:**

1. Intercom fires a `call.started` webhook to the single endpoint
2. The router verifies the HMAC-SHA1 signature
3. The topic is matched and dispatched to the call-timezone handler
4. Parses the caller's E.164 phone number with `phonenumbers` (Google's
   libphonenumber) to determine country and timezone
5. For US/CA numbers, uses the 3-digit area code to narrow to a specific
   timezone
6. Creates an internal note on the contact with timezone details and a stable
   call-ID receipt (visible in all Inbox views). On webhook retry, an existing
   receipt suppresses the duplicate note.
7. Sets an `inferred_timezone` custom attribute on the contact (filterable,
   usable in reports)

## Project Structure

```
edge-serverless-functions/
├── project.yml                            # DO Functions config
├── .env.example                           # Template for local dev secrets
├── README.md
├── packages/
    ├── intercom-article-drafts/
    │   └── upload/                          # Authenticated draft function
    └── intercom/
        ├── webhook/                        # Single deployed function
        │   ├── __main__.py                # Router: verify sig, dispatch by topic
        │   ├── intercom_client.py         # Shared Intercom API client
        │   ├── requirements.txt           # Python dependencies
        │   ├── build.sh                   # Dependency installer for DO
        │   ├── lead_to_user/              # Lead-to-user handler
        │   │   ├── __init__.py
        │   │   └── handler.py
        │   └── call_timezone/             # Call timezone handler
        │       ├── __init__.py
        │       ├── handler.py
        │       └── timezone.py            # Phone → timezone inference
        └── tests/                          # Dev/test (not deployed)
            ├── test_webhook.py            # Lead-to-user tests
            ├── test_call_timezone.py      # Call-timezone tests
            ├── test_payload.json          # Sample webhook payload
            └── serve_local.py             # Local dev server (ngrok)
└── tests/
    └── test_intercom_article_drafts.py     # Draft bridge + deploy-surface tests
```

## Adding a New Handler

To add a new Intercom webhook handler to the router:

1. Create a subpackage under `packages/intercom/webhook/`:

```
packages/intercom/webhook/
└── your_handler/
    ├── __init__.py
    └── handler.py          # must export a handle(payload) function
```

2. Register its topics in `packages/intercom/webhook/__main__.py`:

```python
from your_handler.handler import handle as handle_your_thing

YOUR_TOPICS = {"your.topic.name"}

# Then in main(), add a dispatch block:
if topic in YOUR_TOPICS:
    return handle_your_thing(payload)
```

3. If you need new Intercom API helpers, add them to `intercom_client.py`.

4. Add any new dependencies to `requirements.txt`.

5. Add any new environment variables in the DO Functions dashboard and
   update `.env.example`.

6. Deploy: `doctl serverless deploy . --remote-build --env .env`

## Deployment

Deploy via the `doctl` CLI:

```bash
doctl auth init
doctl serverless connect
doctl serverless deploy . --remote-build --env .env
doctl serverless functions get intercom/webhook --url
```

### Required DigitalOcean Environment Variables

For CLI deployments, put every `${NAME}` referenced by `project.yml` in the
repository-root `.env` used by `doctl serverless deploy . --remote-build --env
.env`. Do not rely on dashboard edits; a later CLI deploy can replace them.

| Variable                          | Description                              |
|-----------------------------------|------------------------------------------|
| `INTERCOM_ACCESS_TOKEN`           | Shared Intercom app bearer token for the webhook and draft broker; requires article read/write permission. |
| `WEBHOOK_SECRET`                  | Intercom app client secret               |
| `LEAD_TO_USER_CONVERSION_ENABLED` | Optional. Set to `true` to enable server-side lead→user merge. **Off by default** because the merge breaks live Messenger sessions (see Lead-to-User section). |
| `INTERCOM_DRAFT_BRIDGE_SECRET`     | Random bearer secret required by the draft bridge. |
| `INTERCOM_ARTICLE_AUTHOR_ID`       | Intercom teammate/admin ID used as the article author. |

### Intercom Webhook Setup

In your Intercom Developer Hub app, set the webhook endpoint URL to your
function URL and subscribe to these topics:

- `contact.lead.created`
- `contact.lead.added_email`
- `contact.email.updated`
- `call.started`

## Local Development

### Unit tests (no API keys needed)

```bash
pip install pytest requests phonenumbers
pytest tests/test_intercom_article_drafts.py packages/intercom/tests/ -v
```

### Live local testing (real Intercom webhooks)

```bash
cp .env.example .env
# Fill in real credentials

pip install requests python-dotenv phonenumbers
python3 packages/intercom/tests/serve_local.py

# In another terminal:
ngrok http 8080
# Copy the ngrok URL into Intercom webhook settings
```
