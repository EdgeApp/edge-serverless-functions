# Edge Serverless Functions

DigitalOcean serverless functions for Edge. All Intercom webhooks are
handled by a single `intercom/webhook` function that routes by topic.

## Webhook Handlers

### Lead-to-User Auto-Converter

Automatically converts Intercom leads into users whenever a lead has an
email address. Handles three webhook topics:

- **`contact.lead.created`** — lead created with an email
- **`contact.lead.added_email`** — email added to a lead that had none
- **`contact.email.updated`** — lead's email changed

**How it works:**

1. Intercom fires a webhook to the single endpoint
2. The router verifies the HMAC-SHA1 signature
3. The topic is matched and dispatched to the lead-to-user handler
4. If the contact is a lead with an email:
   - Searches for an existing user with that email
   - Creates one if none exists
   - Merges the lead into the user (lead is deleted)
5. Returns 200 so Intercom does not retry

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
6. Creates an internal note on the contact with timezone details (visible in
   all Inbox views)
7. Sets an `inferred_timezone` custom attribute on the contact (filterable,
   usable in reports)

## Project Structure

```
edge-serverless-functions/
├── project.yml                            # DO Functions config
├── .env.example                           # Template for local dev secrets
├── README.md
└── packages/
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

6. Deploy: `doctl serverless deploy . --remote-build`

## Deployment

Deploy via the `doctl` CLI:

```bash
doctl auth init
doctl serverless connect
doctl serverless deploy . --remote-build
doctl serverless functions get intercom/webhook --url
```

### Required DigitalOcean Environment Variables

Set these in the DO Functions dashboard under your namespace:

| Variable                  | Description                              |
|---------------------------|------------------------------------------|
| `INTERCOM_ACCESS_TOKEN`   | Intercom API bearer token                |
| `WEBHOOK_SECRET`          | Intercom app client secret               |

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
pytest packages/intercom/tests/ -v
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
