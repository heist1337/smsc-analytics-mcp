"""Rolling account-wide request budget. Reservations are never refunded."""


def budget_state(value: dict, now: float, operation: str, interval: int) -> dict:
    """Rolling windows; reserve before transport and retain reservations on failure."""
    events = [e for e in value.get("events", []) if e["at"] > now - 3600]
    if value.get("next_at", 0) > now:
        raise RuntimeError("smsc_request_spacing; retry later")
    if operation == "messages" and sum(e["op"] == operation for e in events) >= 20:
        raise RuntimeError("smsc_history_hourly_budget; retry later")
    if operation == "statistics" and sum(e["op"] == operation and e["at"] > now - 60 for e in events) >= 2:
        raise RuntimeError("smsc_statistics_minute_budget; retry later")
    return {"events": [*events, {"op": operation, "at": now}], "next_at": now + max(21, interval)}
