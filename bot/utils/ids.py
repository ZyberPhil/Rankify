from __future__ import annotations

import random
import string
from datetime import datetime, UTC


def generate_order_number() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    suffix = "".join(random.choices(string.digits, k=3))
    return f"ORD-{timestamp}-{suffix}"


def normalize_team_prefix(prefix: str) -> str:
    chars = []
    for ch in str(prefix).upper():
        if ch.isalnum():
            chars.append(ch)
    return "".join(chars[:3])


def generate_referral_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choices(alphabet, k=length))
