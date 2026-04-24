# Edge Serverless Functions

DigitalOcean serverless functions for Edge. Deployed automatically on push to
`main` via DigitalOcean's built-in GitHub integration.

## Functions

### `intercom/lead-to-user` — Lead-to-User Auto-Converter

Automatically converts Intercom leads into users whenever a lead has an email
address. Listens to three webhook topics:

- **`contact.lead.created`** — lead created with an email
- **`contact.lead.added_email`** — email added to a lead that had none
- **`contact.email.updated`** — lead's email changed

**How it works:**

1. Intercom fires a webhook
2. The function verifies the HMAC-SHA1 signature
3. If the contact is a lead with an email:
   - Searches for an existing user with that email
   - Creates one if none exists
   - Merges the lead into the user (lead is deleted)
4. Returns 200 so Intercom does not retry

### `intercom/call-timezone` — Inbound Call Timezone Inference

Automatically infers a caller's timezone when an inbound call starts in
Intercom. Listens to the **`call.started`** webhook topic and filters to
inbound calls only.

**How it works:**

1. Intercom fires a `call.started` webhook for an inbound call
2. The function verifies the HMAC-SHA1 signature
3. Parses the caller's E.164 phone number with `phonenumbers` (Google's
   libphonenumber) to determine country and timezone
4. For US/CA numbers, uses the 3-digit area code to narrow to a specific
   timezone
5. Creates an internal note on the contact with timezone details (visible in
   all Inbox views)
6. Sets an `inferred_timezone` custom attribute on the contact (filterable,
   usable in reports)

## Project Structure

```
edge-serverless-functions/
├── project.yml                            # DO Functions config (all packages)
├── .env.example                           # Template for local dev secrets
├── README.md
└── packages/
    └── intercom/
        ├── lead-to-user/                   # Lead-to-user auto-converter
        │   ├── __main__.py                # Entry point
        │   ├── intercom_client.py         # Intercom API client
        │   ├── requirements.txt           # Python dependencies
        │   └── build.sh                   # Dependency installer for DO
        ├── call-timezone/                  # Inbound call timezone inference
        │   ├── __main__.py                # Webhook handler (call.started)
        │   ├── timezone.py                # Phone → timezone inference
        │   ├── intercom_client.py         # Note + attribute API client
        │   ├── requirements.txt
        │   └── build.sh
        └── tests/                         # Dev/test (not deployed)
            ├── test_webhook.py            # Test suite (pytest)
            ├── test_payload.json          # Sample webhook payload
            └── serve_local.py             # Local dev server (ngrok)
```

## Adding a New Function

1. Create a new package under `packages/`:

```
packages/
└── your-package/
    ├── your-function/
    │   ├── __main__.py
    │   ├── requirements.txt
    │   └── build.sh
    └── tests/
        └── test_your_function.py
```

2. Add it to `project.yml`:

```yaml
packages:
  - name: your-package
    environment:
      YOUR_SECRET: ${YOUR_SECRET}
    functions:
      - name: your-function
        runtime: python:3.12
        web: raw
```

3. Add any new environment variables in the DO Functions dashboard and
   update `.env.example`.

4. Push to `main` — DO automatically deploys.

## Deployment

Handled by DigitalOcean's built-in GitHub integration. When code is
pushed/merged to `main`, DO automatically rebuilds and deploys all functions.

### Required DigitalOcean Environment Variables

Set these in the DO Functions dashboard:

| Variable                  | Description                              |
|---------------------------|------------------------------------------|
| `INTERCOM_ACCESS_TOKEN`   | Intercom API bearer token                |
| `WEBHOOK_SECRET`          | Intercom app client secret               |

### Manual Deployment

If you need to deploy without the GitHub integration:

```bash
doctl auth init
doctl serverless connect
cp .env.example .env  # fill in real values
doctl serverless deploy . --remote-build
doctl serverless functions get intercom/lead-to-user --url
```

## Local Development

### Unit tests (no API keys needed)

```bash
pip install pytest requests
pytest packages/intercom/tests/test_webhook.py -v
```

### Live local testing (real Intercom webhooks)

```bash
cp .env.example .env
# Fill in real credentials

pip install requests python-dotenv
python3 packages/intercom/tests/serve_local.py

# In another terminal:
ngrok http 8080
# Copy the ngrok URL into Intercom webhook settings
```
