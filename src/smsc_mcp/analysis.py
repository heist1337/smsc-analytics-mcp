"""Deterministic cost breakdowns; template grouping and repeated sends are hypotheses."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any


def _amount(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        return None
    return int(value)


def _bucket() -> dict[str, Any]:
    return {"messages": 0, "known_cost": Decimal(0), "known_parts": 0, "missing_cost": 0, "missing_parts": 0}


def _add(bucket: dict[str, Any], cost: Decimal | None, parts: int | None):
    bucket["messages"] += 1
    if cost is None:
        bucket["missing_cost"] += 1
    else:
        bucket["known_cost"] += cost
    if parts is None:
        bucket["missing_parts"] += 1
    else:
        bucket["known_parts"] += parts


def _serialize(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


def analyze_messages(
    rows: list[dict[str, Any]], *, currency: str, timezone: str, complete: bool, include_text: bool = False
) -> dict[str, Any]:
    if not isinstance(rows, list) or len(rows) > 3000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid_analysis_rows")
    totals = defaultdict(_bucket)
    groups = {name: defaultdict(_bucket) for name in ("operator", "sender", "status", "day", "template_candidate")}
    seen_ids = set()
    exact_sends = defaultdict(list)
    deduplicated = 0
    multipart = 0
    missing_timestamp = 0
    template_examples = {}
    for row in rows:
        global_id = row.get("int_id")
        if global_id is not None and str(global_id) != "":
            if str(global_id) in seen_ids:
                deduplicated += 1
                continue
            seen_ids.add(str(global_id))
        row_currency = str(row.get("currency") or currency)
        cost, parts = _amount(row.get("cost")), _count(row.get("sms_cnt"))
        _add(totals[row_currency], cost, parts)
        multipart += int(parts is not None and parts > 1)
        text = str(row.get("message", ""))
        # Deliberately conservative proposal: IDs/numbers vary; wording stays significant.
        candidate = re.sub(r"\d+", "{number}", text)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        fingerprint = hashlib.sha256(candidate.encode()).hexdigest()[:20] if text else "unknown"
        if include_text and text:
            template_examples[fingerprint] = candidate
        timestamp = _count(row.get("send_timestamp"))
        if timestamp is None:
            missing_timestamp += 1
        if text and row.get("phone") and timestamp is not None:
            exact_sends[(str(row["phone"]), text)].append(timestamp)
        dimensions = {
            "operator": str(row.get("operator") or "unknown"),
            "sender": str(row.get("sender_id") or "unknown"),
            "status": str(row.get("status_name") or (row["status"] if row.get("status") is not None else "unknown")),
            # Use the provider's date verbatim; do not invent its timezone.
            "day": str(row.get("send_date", "")).split(" ", 1)[0] or "unknown",
            "template_candidate": fingerprint,
        }
        for name, key in dimensions.items():
            _add(groups[name][(row_currency, key)], cost, parts)
    repeated = sum(
        sum(b - a <= 300 for a, b in zip(sorted(times), sorted(times)[1:], strict=False))
        for times in exact_sends.values()
    )
    breakdown = {
        name: [
            dict(currency=cur, value=key, **bucket)
            for (cur, key), bucket in sorted(buckets.items(), key=lambda item: item[1]["known_cost"], reverse=True)
        ]
        for name, buckets in groups.items()
    }
    return _serialize(
        {
            "source": "smsc_history_analysis",
            "complete": complete,
            "timezone": timezone,
            "totals_by_currency": dict(totals),
            "breakdown": breakdown,
            "multipart_messages": multipart,
            "potential_repeats_within_5_minutes": repeated,
            "duplicate_export_rows_removed": deduplicated,
            "messages_without_timestamp": missing_timestamp,
            "template_candidates": template_examples,
            "limitations": [
                "Totals cover fetched rows only; partial history is not a monthly/account total.",
                "known_cost excludes absent/invalid values; known_parts counts provider sms_cnt, not messages.",
                "Template candidates replace numeric runs only; they are neither registered templates nor proven matches.",
                "Identical messages to one recipient within five minutes are possible repeats, not proven waste.",
                "Costs omit recurring template/sender fees and other ledger movements unless present in message cost.",
                "No forecast, savings estimate or business conversion is inferred from this sample.",
            ],
        }
    )
