# PayPal Checkout Agent

A natural-language checkout assistant. A shopper types what they want; an LLM agent
plans **structured tool calls** that build a cart, create a **PayPal Orders v2 sandbox
order**, hand the buyer an approval link, capture the payment once approved, explain
the refund policy, and issue a refund through the Payments v2 captures API.

The model decides *what to do*. A guardrail layer decides *what is allowed*: prices come
only from the catalogue, every tool argument is schema-validated, and moving money needs
the shopper's explicit confirmation.

> Built for the PayPal AI Hackathon, during its submission period. Sandbox only, synthetic
> catalogue and synthetic shoppers only, no real money. Not affiliated with or endorsed by
> PayPal; "PayPal" is a trademark of its owner.

## How it works

```
shopper text ──► agent loop ──► LLM backend ──► tool calls (JSON)
                    ▲              │ scripted (offline test double)
                    │              └ anthropic (Messages API, tool use)
                    │
              tool results         ┌──────────── guardrails ────────────┐
                    ▲              │ schema validation, extra args denied│
                    └── executor ──┤ catalogue prices, currency allow-list│
                                   │ amount/quantity limits, session ids  │
                                   │ confirmation before capture/refund   │
                                   └──────────────┬───────────────────────┘
                                                  ▼
                                   payment adapter: mock | sandbox
                                   (Orders v2 / Payments v2 shapes)
```

Tools the model can call: `search_catalog`, `build_cart`, `create_order`, `get_order`,
`capture_order`, `explain_refund`, `refund_capture`.

Guardrails (all enforced in code, none left to the prompt):

- The model never supplies a price, total, payee or URL. Tool schemas forbid extra
  properties, so a smuggled `price` or `amount` is rejected before any payment call.
- Currency allow-list (USD, EUR), per-item quantity limit (10), distinct-item limit (8),
  order total limit (200.00).
- The model can only act on order and capture ids created in the current session.
- A capture needs an order the buyer has approved (`APPROVED`); the captured amount is
  compared with the order total.
- Capture and refund call a confirmation callback the model cannot answer. The CLI asks
  the shopper (`--yes` auto-confirms for demos). With no callback, the answer is no.
- Refunds cannot exceed the captured balance and are limited to 3 per capture.
- `PayPal-Request-Id` is set on every create, capture and refund, derived from the
  session and the action, so a retry is idempotent.
- The sandbox adapter refuses to run unless the base URL is exactly
  `https://api-m.sandbox.paypal.com`, and refuses `PAYPAL_ENV=live`-style settings.
  Credentials are read from the environment, held in memory, sent only in the
  `Authorization` header, and never logged or printed.

## Requirements

Python 3.10 or newer. No third-party packages: the code uses only the standard library.

## Run it offline (mock payments, scripted LLM)

```bash
python3 -m unittest discover -s tests -t .          # 63 tests, no network
python3 -m paypal_checkout_agent catalog
printf '%s\n' "I'd like 2 mugs and a tee" "/approve" "I approved it, please capture" \
  "how do refunds work?" 'refund $5.00 please' | python3 -m paypal_checkout_agent chat --yes
```

`/approve` (mock only) stands in for the buyer logging in to PayPal and approving. The
scripted backend is a **rule-based test double, not a language model**, and says so in
every reply. It exists so the loop, the guardrails and the adapters can be tested offline.

## Run it with a real model (Anthropic tool use)

```bash
export ANTHROPIC_API_KEY=...            # from your own secret store; never commit it
export CHECKOUT_AGENT_MODEL=claude-sonnet-5-5   # optional; this is the default
python3 -m paypal_checkout_agent chat --llm anthropic          # mock payments
```

## Run it against the PayPal sandbox

1. In the PayPal developer dashboard, create a **Sandbox** REST app and note its client id
   and secret. Use (or create) a **sandbox personal** account to play the buyer.
2. Put the keys in your environment from your own secret store:

   ```bash
   export PAYPAL_SANDBOX_CLIENT_ID=...
   export PAYPAL_SANDBOX_CLIENT_SECRET=...
   ```

3. Check configuration (prints only `set`/`MISSING`; `--live` also asks the sandbox for an
   OAuth token):

   ```bash
   python3 -m paypal_checkout_agent doctor --payments sandbox --live
   ```

4. Chat. Open the approval URL in a browser signed in as the **sandbox buyer**, approve,
   then tell the agent you approved:

   ```bash
   python3 -m paypal_checkout_agent chat --llm anthropic --payments sandbox
   ```

   Without an Anthropic key you can still drive the real sandbox with the scripted backend
   by using `--llm scripted --payments sandbox`.

5. Adapter smoke run without any LLM (create and read an order; then, after approving it,
   capture and refund):

   ```bash
   python3 -m paypal_checkout_agent smoke --payments sandbox
   python3 -m paypal_checkout_agent smoke --payments sandbox --capture <ORDER_ID> --refund
   ```

6. One-command live proof (checks configuration and the OAuth token, then creates and reads
   one order; prints ids and statuses only): `python3 scripts/live_smoke.py`

Optional environment: `PAYPAL_RETURN_URL` and `PAYPAL_CANCEL_URL` (default
`https://example.com/...`; the order is approved on PayPal whether or not that page exists).

## What is verified, and what is not

**Verified (offline, repeatable):**

- 63 unit tests pass with no network: schema validation, the core guardrails (schema check,
  approval check, refund balance and count limits, session-scoped ids, order-total limit,
  sandbox-host check, confirmation: each was broken on purpose and a test failed), the full journey
  purchase, approve, capture, explain refund, partial refund, full refund and over-refund
  refusal on the mock adapter, a hostile-model test (forged ids, smuggled prices, unknown
  tools all refused and none reaches the payment adapter), the Anthropic request/response
  handling against a fake HTTP function, and the CLI.
- The sandbox adapter's request construction (URLs, headers including `PayPal-Request-Id`,
  JSON bodies, error mapping, token caching, sandbox-only URL enforcement, and that no
  secret reaches logs, reprs or error text) is tested against a **fake transport**.
- `python3 scripts/secret_scan.py --history` finds no key-shaped strings in the tree or in
  git history.

**Verified live on the PayPal sandbox (2026-11-09):**

- The whole agent journey ran against `api-m.sandbox.paypal.com` with real sandbox keys: OAuth2
  client-credentials token; the agent's `search_catalog`, `build_cart` and `create_order` calls
  produced a 44.00 USD order in `PAYER_ACTION_REQUIRED` with a `payer-action` approval link; a
  capture request before approval was refused; a sandbox buyer approved the order on PayPal's
  hosted checkout; the agent read `APPROVED`, asked for confirmation, captured (`COMPLETED`),
  explained the refund balance, and refunded 5.00 USD (`COMPLETED`, capture then
  `PARTIALLY_REFUNDED`); a 50-mug request was refused by the guardrail before any payment call.
- First live contact found one difference from the documentation as this adapter used it: the
  refund endpoint answers with only `id`, `status` and `links` unless it is sent
  `Prefer: return=representation`, so the refunded amount was missing from the agent's reply.
  The adapter now sends that header (checked live: the answer carries the amount), and the
  executor reports the requested amount if an answer still lacks one. Both changes have tests.
- `python3 scripts/live_smoke.py` passes against the live sandbox.

**Not verified:**

- The live run above used the `scripted` test double as the model, so it proves the tools,
  guardrails and PayPal calls, not a real model's choices. The Anthropic backend has been
  tested against a fake HTTP function only, not a live call.
- The mock adapter mirrors the Orders v2 shape; it is a test double, not PayPal behaviour.

## Layout

```
paypal_checkout_agent/
  agent.py        agent loop and system prompt
  tools.py        tool schemas and the guard-railed executor
  guardrails.py   policy and schema validation
  catalog.py cart.py money.py
  llm/            base, scripted (test double), anthropic (Messages API tool use)
  payments/       base, mock, sandbox (Orders v2 / Payments v2)
  cli.py          chat, ask, catalog, doctor, smoke
scripts/secret_scan.py   secret scan (tree, staged diff, git history); .githooks/pre-commit
scripts/live_smoke.py    live sandbox proof (doctor --live, then create and read one order)
tests/                   offline tests
```

## Secret hygiene

```bash
python3 scripts/secret_scan.py --history     # add --gitleaks to also run gitleaks if installed
git config core.hooksPath .githooks          # scan staged changes on every commit
```

## Limits

A sample shop with a synthetic catalogue; no shipping, tax, inventory, webhooks or user
accounts; the refund policy text is a demo policy. It is a prototype, not a payment service.

## License

MIT. See [LICENSE](LICENSE).
