# x402-flask-gate

A minimal Flask template that puts an **x402 USDC paywall** in front of a
`POST` endpoint. Fork it, set your wallet, replace the handler with your
own logic, and you have a pay-per-call API.

## What this is — and what it is not

**It is:**

- A working payment gate. `POST /query` returns `402 Payment Required`
  with payment instructions until the caller proves they paid.
- Payment verification against the chain: it fetches the transaction
  receipt from a Base (or SKALE) RPC endpoint and checks the USDC
  `Transfer` log — right recipient, right amount, confirmed, recent.
- Replay-protected: each transaction hash can be used once, enforced by
  a file-backed store with a lock (hashes expire after
  `X402_TX_MAX_AGE` seconds, default 300).

**It is not:**

- A working agent. The `/query` handler returns only a canned
  acknowledgement (`"status": "accepted"` plus the task you sent). There
  is no agent logic, no model call, no tool use. The handler is a stub
  for you to replace.
- A facilitator-based x402 implementation. Verification is done directly
  against the RPC — no third-party facilitator service is involved.

## How the 402 flow works

1. Client calls `POST /query` with no payment headers → **402** with a
   `payment_details` body: amount, currency (USDC), network, chain id,
   recipient wallet, USDC contract, and instructions.
2. Client sends `QUERY_PRICE_USDC` USDC on Base to the recipient wallet.
3. Client retries `POST /query` with headers:
   - `X-Payment-TxHash: 0x...` — the payment transaction hash
   - `X-Payer-Address: 0x...` — the wallet that paid
   - `X-Payment-Chain: base` (optional, defaults to `X402_CHAIN`)
4. The server verifies the receipt: transaction exists, status is
   success, block is recent, and a USDC `Transfer` log pays at least the
   price to the recipient from the claimed payer. The tx hash is reserved
   atomically so it cannot be replayed.
5. On success the request reaches your handler, which sees the payment
   in `request.x402_payment`. On any failure the server answers **402**
   with the reason.

Default price: **0.03 USDC per call** (change with `QUERY_PRICE_USDC`).

## Quickstart

```bash
cp .env.example .env
# Edit .env — WALLET_ADDRESS is required (your receiving wallet).
pip install -r requirements.txt
./run-server.sh
# or: PORT=8080 python3 server.py
```

Then in another terminal:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/
curl -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json' \
  -d '{"task": "hello", "params": {}}'
# → 402 Payment Required, with payment_details
```

Run the test suite (no real payment is made — the tests only check the
free endpoints and that unpaid/invalid calls get 402):

```bash
pip install requests
python3 test_x402.py
# BASE_URL=http://localhost:8080 python3 test_x402.py
```

## Configuration

All settings come from environment variables (see `.env.example`).
Nothing is hardcoded — no wallet address, no private key, no RPC URL.

| Variable | Default | Description |
| --- | --- | --- |
| `WALLET_ADDRESS` | *(required)* | Wallet that receives USDC payments |
| `QUERY_PRICE_USDC` | `0.03` | Price per `POST /query` call, in USDC |
| `X402_CHAIN` | `base` | Default chain (`base` or `skale`) |
| `ALCHEMY_BASE_RPC` | `https://mainnet.base.org` | Base RPC endpoint for verification |
| `X402_TX_MAX_AGE` | `300` | Max payment age in seconds |
| `X402_TX_STORE` | `/tmp/x402_tx_store.json` | Replay-protection store path |
| `PORT` | `8080` | Listen port |
| `AGENT_NAME` | `x402-flask-gate` | Name shown in `/health` and `/` |

Supported chains (see `CHAINS` in `server.py`): **Base** (chain id 8453,
USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) and **SKALE on
Base** (chain id 1187947933). The USDC contract addresses above are the
canonical public token contracts, not secrets.

## Registering on a marketplace (optional)

This repo deliberately ships **no registration script**. The original
project this template was cleaned from had a registration script with a
hardcoded wallet private key and an API token in it — that file was
excluded, and any key that was ever hardcoded like that should be
treated as exposed and rotated.

If you want to list the agent on a marketplace (e.g. OpenAgent), do it
with environment variables only:

1. Create a fresh wallet for the agent, or use an existing one whose
   private key has never been pasted into a file or chat.
2. Set `WALLET_KEY` (signing private key) and `PINATA_JWT` (for the
   metadata upload) in your `.env` — never commit `.env`.
3. Register with the marketplace's SDK/CLI using those env vars, with
   the agent name, description, skills, and the same price you set in
   `QUERY_PRICE_USDC`.

The marketplace registration and the payment gate are separate concerns:
registration advertises the agent, `server.py` enforces payment.

## Project structure

```
server.py        Flask app: payment gate + stub /query handler
test_x402.py     Smoke tests (free endpoints + 402 behaviour)
run-server.sh    Start the server (PORT env, default 8080)
requirements.txt Flask, httpx, requests
.env.example     Placeholder config — copy to .env
.gitignore       Python, .env, databases, editor files
LICENSE          MIT
```

## Security notes

- Never put a private key, seed phrase, or API token in a source file,
  a shell script, or chat. Use `.env` (gitignored) or a secret manager.
- The replay-protection store is a local JSON file with a file lock. It
  is fine for a single instance; use a real database if you run
  multiple replicas.
- Payment verification trusts the RPC endpoint you configure. For
  production, use your own node or a paid RPC provider rather than a
  public endpoint.
- `verify_payment` checks the receipt's USDC `Transfer` log, the
  recipient, the amount, and the block timestamp — but it does not check
  that the payer is who they claim to be beyond the `X-Payer-Address`
  header matching the transfer's sender. Treat the payer address as a
  claim, not an identity proof.

## License

MIT — see `LICENSE`.
