"""Small helpers for end-to-end testing.

Run via:  docker compose exec web python /app/docker/test-helpers.py <command> [args]

Commands:
  resend-bounce <connection_id> <recipient> <email_id> [<signing_secret>]
  resend-delivered <connection_id> <recipient> <email_id> [<signing_secret>]
  postal-delivered <connection_id> <token> [<private_key_pem_path>]   (advanced)

Without a signing secret, looks it up from the Connection row by id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.request


def _fetch_resend_secret(connection_id: int) -> str:
    # Run inside the container so we can decrypt.
    import django

    django.setup()
    from apps.connections.models import Connection
    from apps.core.encryption import decrypt

    conn = Connection.objects.get(pk=connection_id, provider_code="resend")
    return decrypt(bytes(conn.webhook_secret_encrypted))


def post_resend(connection_id: int, event_type: str, recipient: str, email_id: str, secret: str | None = None) -> None:
    body = {
        "type": event_type,
        "created_at": "2026-05-17T12:00:00.000Z",
        "data": {"email_id": email_id, "to": [recipient]},
    }
    if event_type == "email.bounced":
        body["data"]["bounce"] = {"message": "auto-test bounce", "type": "Permanent"}
    raw = json.dumps(body).encode()

    if secret is None:
        secret = _fetch_resend_secret(connection_id)
    svix_id = f"msg_{int(time.time())}"
    svix_ts = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{svix_id}.{svix_ts}.".encode() + raw
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    req = urllib.request.Request(
        f"http://web:8000/webhooks/resend/{connection_id}/",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "svix-id": svix_id,
            "svix-timestamp": svix_ts,
            "svix-signature": f"v1,{sig}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"{resp.status} {resp.read().decode()}")
    except urllib.error.HTTPError as e:
        print(f"{e.code} {e.read().decode()}")


if __name__ == "__main__":
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mailtivo_relay.settings.prod")

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd in ("resend-bounce", "resend-delivered"):
        type_ = "email.bounced" if cmd == "resend-bounce" else "email.delivered"
        connection_id = int(sys.argv[2])
        recipient = sys.argv[3]
        email_id = sys.argv[4]
        secret = sys.argv[5] if len(sys.argv) > 5 else None
        post_resend(connection_id, type_, recipient, email_id, secret)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
