"""
Local development server for the call-timezone function.

Usage:
    1. cp .env.example .env   (fill in real tokens)
    2. python3 serve_local_call_timezone.py
    3. In another terminal: ngrok http 8081
    4. Paste the ngrok URL into Intercom's webhook settings for call.started
"""

import hashlib
import hmac
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv

load_dotenv()

FUNC_DIR = os.path.join(os.path.dirname(__file__), "..", "call-timezone")
sys.path.insert(0, FUNC_DIR)

import importlib.util
spec = importlib.util.spec_from_file_location(
    "call_tz_handler",
    os.path.join(FUNC_DIR, "__main__.py"),
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
        print(f"\n── DEBUG ──────────────────────────────────")
        print(f"  Received sig:  {sig_header}")
        print(f"  Computed sig:  sha1={computed}")
        print(f"  Secret (len):  {len(secret)} chars")
        print(f"  Body (len):    {len(raw_body)} chars")

        try:
            payload = json.loads(raw_body)
            item = payload.get("data", {}).get("item", {})
            print(f"  Topic:         {payload.get('topic')}")
            print(f"  Direction:     {item.get('direction')}")
            print(f"  Phone:         {item.get('phone')}")
            print(f"  Contact ID:    {item.get('contact_id')}")
        except Exception:
            print(f"  (could not parse payload)")
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
        self.wfile.write(b"call-timezone webhook handler is running. POST to this URL.")


def run(port=8082):
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
    print(f"call-timezone local server running on http://localhost:{port}")
    print()
    print("Next steps:")
    print(f"  1. In another terminal: ngrok http {port}")
    print("  2. Copy the ngrok https URL")
    print("  3. In Intercom Developer Hub → Webhooks:")
    print("     - Set the endpoint URL to your ngrok URL")
    print("     - Subscribe to the 'call.started' topic")
    print("  4. Make an inbound call — watch this terminal for logs")
    print()
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    run()
