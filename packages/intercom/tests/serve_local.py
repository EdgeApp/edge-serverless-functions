"""
Local development server that simulates the DO Functions runtime.

Wraps the unified webhook router in a simple HTTP server so you can test
end-to-end with real Intercom webhooks via a tunnel (ngrok, etc).

Usage:
    1. cp .env.example .env   (fill in real tokens)
    2. python3 serve_local.py
    3. In another terminal: ngrok http 8080
    4. Paste the ngrok URL into Intercom's webhook settings
"""

import hashlib
import hmac
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "webhook"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "webhook_router",
    os.path.join(os.path.dirname(__file__), "..", "webhook", "__main__.py"),
)
handler_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler_module)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8")

        headers = {k.lower(): v for k, v in self.headers.items()}

        sig_header = headers.get("x-hub-signature", "")
        secret = os.environ.get("WEBHOOK_SECRET", "")
        computed = hmac.new(
            secret.encode("utf-8"), raw_body.encode("utf-8"), hashlib.sha1
        ).hexdigest()

        try:
            payload = json.loads(raw_body)
            topic = payload.get("topic", "(unknown)")
        except Exception:
            topic = "(parse error)"

        print(f"\n── DEBUG ──────────────────────────────────")
        print(f"  Topic:         {topic}")
        print(f"  Received sig:  {sig_header}")
        print(f"  Computed sig:  sha1={computed}")
        print(f"  Secret (len):  {len(secret)} chars")
        print(f"  Body (len):    {len(raw_body)} chars")
        print(f"───────────────────────────────────────────\n")

        event = {
            "http": {
                "method": "POST",
                "headers": headers,
                "body": raw_body,
                "isBase64Encoded": False,
                "path": self.path,
            }
        }

        result = handler_module.main(event, None)

        status = result.get("statusCode", 200)
        body = result.get("body", "")
        if isinstance(body, dict):
            body = json.dumps(body)

        print(f"  → Response: {status} {body}\n")

        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Intercom webhook router is running. POST to this URL.")


def run(port=8080):
    missing = []
    if not os.environ.get("INTERCOM_ACCESS_TOKEN"):
        missing.append("INTERCOM_ACCESS_TOKEN")
    if not os.environ.get("WEBHOOK_SECRET"):
        missing.append("WEBHOOK_SECRET")
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your real values.")
        sys.exit(1)

    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"Local webhook server running on http://localhost:{port}")
    print()
    print("Handles all topics via the unified router:")
    print("  - contact.lead.created, contact.lead.added_email, contact.email.updated")
    print("  - call.started")
    print()
    print("Next steps:")
    print(f"  1. In another terminal: ngrok http {port}")
    print("  2. Copy the ngrok https URL")
    print("  3. Paste it into Intercom > Developer Hub > Webhooks as the endpoint")
    print("  4. Subscribe to the topics above")
    print("  5. Trigger a webhook — watch this terminal for logs")
    print()
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    run()
