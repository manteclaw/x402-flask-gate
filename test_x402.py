#!/usr/bin/env python3
"""
Test script for the x402-flask-gate server.
Run this after starting the server (see README).

Checks:
  1. GET /health returns 200 (free)
  2. GET / returns 200 with payment config (free)
  3. POST /query without payment returns 402
  4. POST /query with a fake tx hash returns 402 (invalid tx)

No real payment is made by this script. The fake tx hash and the
placeholder payer address below are not real transactions.
"""

import os
import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")

# Clearly fake test values — not a real transaction or wallet.
FAKE_TX_HASH = "0x" + "00" * 32
PLACEHOLDER_PAYER = os.getenv("X402_TEST_PAYER", "0x" + "0" * 40)


def test_endpoint(name: str, method: str, path: str, headers=None, json_data=None, expected_status=None):
    url = f"{BASE_URL}{path}"
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers or {}, timeout=5)
        else:
            resp = requests.post(url, headers=headers or {}, json=json_data or {}, timeout=5)

        ok = expected_status is None or resp.status_code == expected_status
        status = "PASS" if ok else "FAIL"
        print(f"{status} {name}: {resp.status_code}")
        if not ok:
            print(f"   Expected {expected_status}, got {resp.status_code}")
        try:
            body = resp.json()
            if resp.status_code == 402 and "payment_details" in body:
                pd = body["payment_details"]
                print(f"   Payment Required: {pd['amount']} {pd['currency']} on {pd['network']}")
                print(f"   Recipient: {pd['recipient']}")
        except Exception:
            pass
        return resp
    except Exception as e:
        print(f"FAIL {name}: ERROR - {e}")
        return None


def main():
    print("=" * 60)
    print("x402-flask-gate Test Suite")
    print("=" * 60)

    # 1. Health check (free)
    test_endpoint("Health (free)", "GET", "/health", expected_status=200)

    # 2. Root info (free)
    r = test_endpoint("Root info (free)", "GET", "/", expected_status=200)
    if r and r.status_code == 200:
        data = r.json()
        p = data.get("payment", {})
        print(f"   Configured price: {p.get('price_usdc')} USDC")
        print(f"   Chain: {p.get('network')} (chain_id={p.get('chain_id')})")

    # 3. Query without payment → 402
    test_endpoint(
        "Query without payment",
        "POST", "/query",
        json_data={"task": "test", "params": {}},
        expected_status=402
    )

    # 4. Query with fake tx hash → 402 (invalid tx)
    test_endpoint(
        "Query with fake tx hash",
        "POST", "/query",
        headers={
            "X-Payment-TxHash": FAKE_TX_HASH,
            "X-Payer-Address": PLACEHOLDER_PAYER,
        },
        json_data={"task": "test", "params": {}},
        expected_status=402
    )

    print("=" * 60)
    print("Done. If all checks passed, the x402 gate is operational.")
    print("=" * 60)


if __name__ == "__main__":
    main()
