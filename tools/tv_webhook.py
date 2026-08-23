"""Receiver for TradingView alert webhooks.

TradingView's only supported push integration: a Pine `alert()` POSTs a body to
a URL you control. This listens for those, authenticates them, and appends each
one to a JSONL log. Nothing else.

RECORDS ONLY. This server does not place, modify or cancel orders, and it holds
no broker credentials. An alert receiver wired directly to an execution
endpoint is how a research tool becomes an unattended trading bot, which is a
different thing with different failure modes and needs a deliberate decision
rather than an import. Read the log, decide yourself.

Standard library only - no framework, no dependencies.

Usage:
    python tools/tv_webhook.py --secret "some-long-random-string"
    python tools/tv_webhook.py --secret ... --port 8080 --log data/alerts.jsonl
    python tools/tv_webhook.py --show                  # summarise what arrived

TradingView specifics that shape this design:

* **Webhooks need a paid plan** (Essential and above).
* **TradingView cannot send custom headers**, so the shared secret has to
  travel inside the alert body. Put `"secret": "..."` in the alert's JSON
  message.
* **The endpoint must be reachable from the internet**, which localhost is not.
  Use a tunnel - `cloudflared tunnel --url http://localhost:8000` or
  `ngrok http 8000` - and give TradingView the public URL.
* TradingView posts from a fixed set of IPs, so `--allow-tv-ips` restricts the
  listener to them. Behind a tunnel the source address is the tunnel's, so that
  flag only helps when the port is exposed directly.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# Published TradingView webhook source addresses.
TRADINGVIEW_IPS = {"52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"}

MAX_BODY = 64 * 1024  # an alert is a few hundred bytes; anything larger is not one

DEFAULT_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "alerts.jsonl"
)


class Config:
    secret: str = ""
    log_path: str = DEFAULT_LOG
    allow_tv_ips: bool = False
    received: int = 0
    rejected: int = 0


def _parse_body(raw: str):
    """Alerts arrive as JSON or as a plain string, depending on the Pine code."""
    try:
        parsed = json.loads(raw)
        return parsed, "json"
    except json.JSONDecodeError:
        return {"message": raw}, "text"


def _authorised(payload: dict, raw: str) -> bool:
    """Constant-time secret check against the payload.

    TradingView cannot set headers, so the secret travels in the body. It is
    accepted from a `secret` field, or anywhere in a plain-text alert.
    """
    if not Config.secret:
        return True  # explicitly unauthenticated; the server warns loudly at start
    supplied = str(payload.get("secret") or payload.get("passphrase") or "")
    if supplied and hmac.compare_digest(supplied, Config.secret):
        return True
    # Plain-text alerts cannot carry a field, so allow the secret inline
    return Config.secret in raw


def _redact(value):
    """Remove the shared secret before anything is written to disk.

    Stripping the `secret` key alone is not enough: a plain-text alert carries
    the secret inline in its message, and a nested object can carry it at any
    depth. The log is meant to be safe to read, share or commit, so the secret
    is removed from keys and from string values alike.
    """
    if isinstance(value, dict):
        return {
            k: _redact(v) for k, v in value.items()
            if k.lower() not in ("secret", "passphrase", "password", "token")
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and Config.secret and Config.secret in value:
        return value.replace(Config.secret, "[redacted]").strip()
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "tv-webhook/1.0"

    def log_message(self, fmt, *args):  # silence the default stderr access log
        pass

    def _reply(self, code: int, body: str = "") -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # A health check, so you can confirm the tunnel reaches the process
        # without waiting for an alert to fire.
        if self.path in ("/", "/health"):
            self._reply(200, f"tv-webhook up; received={Config.received} rejected={Config.rejected}\n")
        else:
            self._reply(404, "not found\n")

    def do_POST(self):
        peer = self.client_address[0]
        if Config.allow_tv_ips and peer not in TRADINGVIEW_IPS:
            Config.rejected += 1
            print(f"  rejected: source {peer} is not a TradingView address")
            self._reply(403, "forbidden\n")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            Config.rejected += 1
            self._reply(413, "bad body length\n")
            return

        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        payload, fmt = _parse_body(raw)

        if not _authorised(payload, raw):
            Config.rejected += 1
            print(f"  rejected: bad or missing secret from {peer}")
            self._reply(401, "unauthorized\n")
            return

        payload = _redact(payload)

        record = {
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_ip": peer,
            "format": fmt,
            "alert": payload,
        }
        os.makedirs(os.path.dirname(Config.log_path), exist_ok=True)
        with open(Config.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        Config.received += 1
        summary = payload.get("message") if isinstance(payload, dict) else str(payload)
        print(f"  [{record['received_at']}] alert #{Config.received}: {str(summary)[:110]}")
        self._reply(200, "ok\n")


def show(log_path: str) -> int:
    if not os.path.exists(log_path):
        print(f"\nNo alert log at {log_path} - nothing has been received yet.\n")
        return 0
    rows = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if not rows:
        print(f"\n{log_path} is empty.\n")
        return 0

    print(f"\n{len(rows)} alerts   {rows[0]['received_at'][:10]} to {rows[-1]['received_at'][:10]}\n")
    tickers: dict[str, int] = {}
    for r in rows:
        a = r.get("alert") or {}
        key = str(a.get("ticker") or a.get("symbol") or "unspecified")
        tickers[key] = tickers.get(key, 0) + 1
    for k, v in sorted(tickers.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<20}{v:>6}")

    print("\n  most recent:")
    for r in rows[-5:]:
        a = r.get("alert") or {}
        print(f"    {r['received_at']}  {str(a.get('message') or a)[:90]}")
    print("\n  These are recorded signals, not trades. Nothing was executed.\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Receive and log TradingView alert webhooks.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; keep loopback and use a tunnel (default 127.0.0.1)")
    ap.add_argument("--secret", default=os.environ.get("TV_WEBHOOK_SECRET", ""),
                    help="shared secret expected in the alert body")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--allow-tv-ips", action="store_true",
                    help="only accept TradingView's published source addresses")
    ap.add_argument("--show", action="store_true", help="summarise the alert log and exit")
    args = ap.parse_args()

    Config.secret = args.secret
    Config.log_path = args.log
    Config.allow_tv_ips = args.allow_tv_ips

    if args.show:
        return show(args.log)

    if not Config.secret:
        print("\n  WARNING: no --secret set. Anyone who finds the URL can write to your")
        print("  alert log. Set one and include it in the alert body.\n")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\nListening on http://{args.host}:{args.port}  ->  {args.log}")
    print("  This receiver records alerts. It does not place trades.")
    print(f"  Health check: curl http://{args.host}:{args.port}/health")
    print("\n  TradingView cannot reach localhost. Expose it with a tunnel:")
    print(f"    cloudflared tunnel --url http://localhost:{args.port}")
    print(f"    ngrok http {args.port}")
    print("  then paste the public URL into the alert's Webhook URL field.")
    print("\n  Alert message body (JSON):")
    print('    {"secret":"<your secret>","ticker":"{{ticker}}",'
          '"action":"{{strategy.order.action}}","price":"{{close}}","time":"{{timenow}}"}')
    print("\n  Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n\nStopped. {Config.received} alerts received, {Config.rejected} rejected.\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
