---
name: tradingview-webhook
description: Run a local receiver for TradingView alert webhooks, logging each alert to JSONL, and summarise what has arrived. Records signals only - it never places trades. Use when asked to capture TradingView alerts or Pine strategy signals live.
---

# TradingView alert webhook receiver

TradingView's only supported push integration. A Pine `alert()` POSTs to a URL
you control; this listens, authenticates, and appends to a log.

```bash
python tools/tv_webhook.py --secret "$TV_WEBHOOK_SECRET"
python tools/tv_webhook.py --secret "..." --port 8080 --allow-tv-ips
python tools/tv_webhook.py --show                    # summarise the log
```

## It records. It does not trade.

This server holds no broker credentials and calls no execution endpoint. Do not
extend it into one on your own initiative — wiring an alert receiver straight
to order placement turns a research tool into an unattended trading bot, which
has entirely different failure modes and needs the user's explicit, separate
decision. If asked for that, confirm first and treat it as its own piece of
work.

## Setup, in order

1. **Start the receiver.** It binds to `127.0.0.1` by default. Keep it there.
2. **Expose it.** TradingView cannot reach localhost:
   `cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`.
3. **Paste the public URL** into the alert's Webhook URL field. Webhooks
   require a paid TradingView plan (Essential and above).
4. **Put the secret in the alert body.** TradingView cannot send custom
   headers, so the shared secret has to travel in the payload:

   ```json
   {"secret":"<your secret>","ticker":"{{ticker}}",
    "action":"{{strategy.order.action}}","price":"{{close}}","time":"{{timenow}}"}
   ```

5. **Check reachability** with `curl https://<public-url>/health` before waiting
   on a live alert.

## Behaviour worth knowing

- Both JSON and plain-text alert bodies are accepted; plain text is wrapped as
  `{"message": ...}`.
- The secret is stripped from keys *and* from string values at any nesting
  depth before anything is written, so the log is safe to read and share.
- Bodies over 64 KB are rejected — an alert is a few hundred bytes.
- `--allow-tv-ips` restricts to TradingView's published source addresses. Behind
  a tunnel the source is the tunnel's own address, so the flag only helps when
  the port is exposed directly.
- Without `--secret` the server starts but warns: anyone who finds the URL can
  write to the log.

## Reporting

`--show` gives counts by ticker and the most recent alerts. When reporting,
state plainly that these are recorded signals and nothing was executed. Alert
counts say how often a strategy fired, not whether it was right — for
performance, export the trades and use `tradingview-import`.
