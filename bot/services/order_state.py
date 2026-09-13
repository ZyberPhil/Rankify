from __future__ import annotations

ORDER_TRANSITIONS: dict[str, set[str]] = {
    "open": {"claimed", "cancelled", "disputed"},
    "claimed": {"in_progress", "completed", "cancelled", "disputed"},
    "in_progress": {"completed", "cancelled", "disputed"},
    "completed": set(),
    "cancelled": set(),
    "disputed": set(),
}


def can_transition_order(current_status: str, target_status: str) -> bool:
    allowed = ORDER_TRANSITIONS.get(current_status)
    if allowed is None:
        return False
    return target_status in allowed
