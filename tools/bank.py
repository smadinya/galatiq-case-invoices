"""The bank stub.

`mock_payment` is copied verbatim from README.md, section "Mock Payment API". Do not change its
behaviour; `agents/payment.py` wraps it (retries, idempotency, stdout capture).
"""


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
