#!/usr/bin/env python3
"""
x402-flask-gate — Flask x402 Payment Gate (template)

A minimal Flask service that puts an x402 USDC paywall in front of a
POST endpoint. Use it as a starting point for charging per API call.

Payment Required:
  - POST /query    → QUERY_PRICE_USDC per call (default 0.03 USDC)
  - GET  /health   → free
  - GET  /         → free (discovery)

What this template does NOT do:
  - /query returns only a canned acknowledgement. There is no agent
    logic here — replace the handler body with your own work.
  - It verifies payment by checking the USDC Transfer log in the
    transaction receipt via a public RPC endpoint. It does not use a
    facilitator service.

Configuration is entirely through environment variables (see
.env.example). No wallet address, key, or RPC URL is hardcoded.
"""

from flask import Flask, request, jsonify
import logging
import time
import os
import json
import hmac
import fcntl
from pathlib import Path
from functools import wraps

# httpx is preferred when available; fall back to requests.
try:
    import httpx
    _HTTP_CLIENT = "httpx"
except ImportError:
    import requests
    _HTTP_CLIENT = "requests"

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('x402-flask-gate')

AGENT_NAME = os.getenv("AGENT_NAME", "x402-flask-gate")
AGENT_VERSION = "1.0.0"

# ===========================================================================
# x402 Configuration
# ===========================================================================
# Required: the wallet that receives payment. No default — set it in .env.
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
QUERY_PRICE_USDC = float(os.getenv("QUERY_PRICE_USDC", "0.03"))
DEFAULT_CHAIN = os.getenv("X402_CHAIN", "base")

CHAINS = {
    "base": {
        "rpc_url": os.getenv("ALCHEMY_BASE_RPC", "https://mainnet.base.org"),
        "usdc_contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "chain_id": 8453,
        "label": "Base",
        "explorer": "https://basescan.org",
    },
    "skale": {
        "rpc_url": "https://skale-base.skalenodes.com/v1/base",
        "usdc_contract": "0x85889c8c714505E0c94b30fcfcF64fE3Ac8FCb20",
        "chain_id": 1187947933,
        "label": "SKALE on Base",
        "explorer": "https://base.explorer.skalenodes.com",
    },
}

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDC_DECIMALS = 6
MAX_TX_AGE_SECONDS = int(os.getenv("X402_TX_MAX_AGE", "300"))

# ===========================================================================
# Transaction Store (replay protection)
# ===========================================================================
TX_STORE_PATH = Path(os.getenv("X402_TX_STORE", "/tmp/x402_tx_store.json"))
TX_STORE_LOCK_PATH = TX_STORE_PATH.with_suffix(".lock")


def _acquire_lock():
    lock_file = open(TX_STORE_LOCK_PATH, "a")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def _release_lock(lock_file):
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _load_tx_store() -> dict:
    if not TX_STORE_PATH.exists():
        return {"hashes": []}
    try:
        with open(TX_STORE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        logger.warning("Failed to load x402 tx_store, starting fresh")
        return {"hashes": []}


def _save_tx_store(data: dict):
    temp_path = TX_STORE_PATH.with_suffix(".tmp")
    with open(temp_path, "w") as f:
        json.dump(data, f)
    temp_path.replace(TX_STORE_PATH)


def _cleanup_expired_hashes(data: dict) -> dict:
    cutoff = time.time() - MAX_TX_AGE_SECONDS
    original = len(data["hashes"])
    data["hashes"] = [e for e in data["hashes"] if e["timestamp"] > cutoff]
    if len(data["hashes"]) < original:
        logger.debug("Cleaned up %d expired x402 tx hashes", original - len(data["hashes"]))
    return data


def is_tx_hash_used(tx_hash: str) -> bool:
    lock_file = _acquire_lock()
    try:
        data = _cleanup_expired_hashes(_load_tx_store())
        for entry in data["hashes"]:
            if hmac.compare_digest(entry["hash"], tx_hash.lower()):
                return True
        return False
    finally:
        _release_lock(lock_file)


def mark_tx_hash_used(tx_hash: str) -> bool:
    """Mark a tx hash as used. Returns False if already used."""
    lock_file = _acquire_lock()
    try:
        data = _cleanup_expired_hashes(_load_tx_store())
        tx_hash_lower = tx_hash.lower()
        for entry in data["hashes"]:
            if hmac.compare_digest(entry["hash"], tx_hash_lower):
                return False
        data["hashes"].append({"hash": tx_hash_lower, "timestamp": time.time()})
        _save_tx_store(data)
        return True
    finally:
        _release_lock(lock_file)


def remove_tx_hash_reservation(tx_hash: str):
    """Release a reserved tx hash (on verification failure)."""
    lock_file = _acquire_lock()
    try:
        data = _cleanup_expired_hashes(_load_tx_store())
        tx_hash_lower = tx_hash.lower()
        original = len(data["hashes"])
        data["hashes"] = [e for e in data["hashes"] if not hmac.compare_digest(e["hash"], tx_hash_lower)]
        if len(data["hashes"]) < original:
            _save_tx_store(data)
    finally:
        _release_lock(lock_file)


# ===========================================================================
# Payment Verification
# ===========================================================================
def verify_payment(tx_hash: str, expected_amount: float, expected_sender: str = None, chain: str = None) -> dict:
    """Verify a USDC payment by checking eth_getTransactionReceipt via RPC."""
    tx_hash = tx_hash.strip().lower()
    chain_key = chain if chain in CHAINS else DEFAULT_CHAIN
    chain_cfg = CHAINS[chain_key]
    rpc_url = chain_cfg["rpc_url"]
    usdc_contract = chain_cfg["usdc_contract"]

    # Atomic check-and-reserve
    if not mark_tx_hash_used(tx_hash):
        return {"valid": False, "error": "Transaction already used"}

    try:
        rpc_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getTransactionReceipt", "params": [tx_hash],
        }

        if _HTTP_CLIENT == "httpx":
            import httpx
            resp = httpx.post(rpc_url, json=rpc_payload, timeout=15)
            receipt = resp.json().get("result")
        else:
            resp = requests.post(rpc_url, json=rpc_payload, timeout=15)
            receipt = resp.json().get("result")

        if not receipt:
            remove_tx_hash_reservation(tx_hash)
            return {"valid": False, "error": "Transaction not found or invalid"}
        if receipt.get("status") != "0x1":
            remove_tx_hash_reservation(tx_hash)
            return {"valid": False, "error": "Transaction reverted"}

        # Check block timestamp for recency
        block_hex = receipt.get("blockNumber")
        if block_hex:
            block_payload = {
                "jsonrpc": "2.0", "id": 2,
                "method": "eth_getBlockByNumber", "params": [block_hex, False],
            }
            try:
                if _HTTP_CLIENT == "httpx":
                    block_resp = httpx.post(rpc_url, json=block_payload, timeout=15)
                    block_result = block_resp.json().get("result")
                else:
                    block_resp = requests.post(rpc_url, json=block_payload, timeout=15)
                    block_result = block_resp.json().get("result")

                if block_result:
                    block_ts = int(block_result.get("timestamp", "0x0"), 16)
                    if time.time() - block_ts > MAX_TX_AGE_SECONDS:
                        remove_tx_hash_reservation(tx_hash)
                        return {"valid": False, "error": f"Transaction too old (>{MAX_TX_AGE_SECONDS}s)"}
            except Exception:
                pass  # Don't fail if block timestamp check errors

        expected_raw = int(expected_amount * (10 ** USDC_DECIMALS))
        for log_entry in receipt.get("logs", []):
            if log_entry.get("address", "").lower() != usdc_contract.lower():
                continue
            topics = log_entry.get("topics", [])
            if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() != WALLET_ADDRESS.lower():
                continue

            from_addr = "0x" + topics[1][-40:]
            if expected_sender:
                if not hmac.compare_digest(from_addr.lower(), expected_sender.strip().lower()):
                    remove_tx_hash_reservation(tx_hash)
                    return {"valid": False, "error": f"Sender mismatch: expected {expected_sender}, got {from_addr}"}

            raw_amount = int(log_entry.get("data", "0x0"), 16)
            if raw_amount >= expected_raw:
                return {"valid": True, "error": None}
            got = raw_amount / (10 ** USDC_DECIMALS)
            remove_tx_hash_reservation(tx_hash)
            return {"valid": False, "error": f"Insufficient payment: expected {expected_amount} USDC, got {got}"}

        remove_tx_hash_reservation(tx_hash)
        return {"valid": False, "error": "No matching USDC transfer found"}

    except Exception as exc:
        remove_tx_hash_reservation(tx_hash)
        return {"valid": False, "error": f"Verification error: {str(exc)}"}


# ===========================================================================
# Flask Decorator: require_x402_payment
# ===========================================================================
def require_x402_payment(price: float):
    """Decorator that gates a Flask endpoint behind x402 USDC payment."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            tx_hash = request.headers.get("X-Payment-TxHash")
            payer = request.headers.get("X-Payer-Address")
            payment_chain = request.headers.get("X-Payment-Chain", DEFAULT_CHAIN).lower()

            if not tx_hash:
                default_cfg = CHAINS[DEFAULT_CHAIN]
                networks = [
                    {
                        "chain": key,
                        "label": cfg["label"],
                        "chain_id": cfg["chain_id"],
                        "usdc_contract": cfg["usdc_contract"],
                        "rpc_url": cfg["rpc_url"],
                    }
                    for key, cfg in CHAINS.items()
                ]
                return jsonify({
                    "error": "Payment Required",
                    "payment_details": {
                        "amount": str(price),
                        "currency": "USDC",
                        "network": default_cfg["label"],
                        "chain_id": default_cfg["chain_id"],
                        "recipient": WALLET_ADDRESS,
                        "usdc_contract": default_cfg["usdc_contract"],
                        "networks": networks,
                        "instructions": (
                            f"Send {price} USDC on {default_cfg['label']} to {WALLET_ADDRESS}, "
                            "then retry with headers: X-Payment-TxHash: 0x... and X-Payer-Address: 0x..."
                        ),
                    },
                }), 402

            check = verify_payment(tx_hash, price, expected_sender=payer, chain=payment_chain)
            if not check["valid"]:
                return jsonify({"error": check["error"]}), 402

            # Attach payment info to request for endpoint access
            request.x402_payment = {
                "tx_hash": tx_hash.strip().lower(),
                "amount_charged": str(price),
                "currency": "USDC",
                "chain": payment_chain,
            }
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ===========================================================================
# Logging Middleware
# ===========================================================================
@app.before_request
def log_request():
    logger.info(
        f"→ {request.method} {request.path} | "
        f"remote={request.remote_addr} | "
        f"content-type={request.content_type or '-'}"
    )


@app.after_request
def log_response(response):
    logger.info(
        f"← {response.status_code} | "
        f"content-length={response.content_length or '-'}"
    )
    return response


# ===========================================================================
# Endpoints
# ===========================================================================
@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint — free."""
    return jsonify({
        "status": "ok",
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "timestamp": int(time.time())
    })


@app.route('/query', methods=['POST'])
@require_x402_payment(price=QUERY_PRICE_USDC)
def query():
    """Accept task queries — requires x402 USDC payment.

    TEMPLATE STUB: this handler only acknowledges the task. Replace the
    body below with your own logic; the payment gate above is the part
    this template provides.
    """
    data = request.get_json(force=True, silent=True) or {}

    task = data.get('task', 'unknown')
    params = data.get('params', {})

    logger.info(f"PAID TASK: {task} | params={params} | payment={getattr(request, 'x402_payment', {})}")

    response = {
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "task": task,
        "status": "accepted",
        "timestamp": int(time.time()),
        "message": f"Task '{task}' received. Agent is operational.",
        "capabilities": [
            "example-capability"
        ],
        "payment": getattr(request, 'x402_payment', {}),
        "note": "Template stub — replace this handler with your own logic."
    }

    return jsonify(response)


@app.route('/', methods=['GET'])
def root():
    """Root endpoint with agent info — free."""
    default_cfg = CHAINS[DEFAULT_CHAIN]
    return jsonify({
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "endpoints": {
            "health": "/health",
            "query": "/query (POST) — requires x402 payment",
        },
        "payment": {
            "price_usdc": QUERY_PRICE_USDC,
            "currency": "USDC",
            "network": default_cfg["label"],
            "chain_id": default_cfg["chain_id"],
            "recipient": WALLET_ADDRESS,
            "usdc_contract": default_cfg["usdc_contract"],
            "headers_required": ["X-Payment-TxHash", "X-Payer-Address"],
        },
        "status": "operational"
    })


if __name__ == '__main__':
    if not WALLET_ADDRESS:
        raise SystemExit(
            "WALLET_ADDRESS environment variable is required "
            "(the wallet that receives USDC payments). "
            "Copy .env.example to .env and set it."
        )
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting {AGENT_NAME} v{AGENT_VERSION} on port {port}")
    logger.info(f"x402 payment enabled: {QUERY_PRICE_USDC} USDC per /query call")
    logger.info(f"Recipient: {WALLET_ADDRESS} on {DEFAULT_CHAIN}")
    app.run(host='0.0.0.0', port=port, threaded=True)
