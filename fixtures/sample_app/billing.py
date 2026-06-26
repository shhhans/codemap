"""Billing mainline (fixture) — crosses the Auth mainline twice.

  charge(req)
    ├─ parse_jwt(token)            ← ⚠ DANGEROUS crossing: Billing reaches into
    │                                 Auth's INTERMEDIATE step (raw claims) to
    │                                 pull user_id itself — responsibility
    │                                 pollution. If Auth changes how it parses
    │                                 tokens, Billing breaks.
    └─ get_current_user(user_id)   ← ✓ HEALTHY crossing: Billing depends on
                                      Auth's STABLE SINK (the settled User), the
                                      proper seam between features.
    └─ do_charge(user, amount)
"""

from auth import get_current_user, parse_jwt


def to_cents(raw):
    return int(round(float(raw) * 100))


def normalize_amount(raw):
    """Intermediate processing of the amount material (a second branch)."""
    cents = to_cents(raw)
    return max(cents, 0)


def do_charge(user, amount):
    return {"charged": amount, "user": user["id"]}


def charge(req):
    token = req["authorization"]

    # Branch 1 — ⚠ Pollution: tapping Auth's intermediate parsing (parse_jwt)
    # instead of its stable API.
    claims = parse_jwt(token)
    user_id = claims["sub"]

    # ✓ Healthy: consuming Auth's stable user state.
    user = get_current_user(user_id)

    # Branch 2 — an independent amount-processing chain. Two intermediate
    # branches out of charge() => the Coordinator forks two concurrent workers.
    amount = normalize_amount(req["amount"])

    return do_charge(user, amount)
