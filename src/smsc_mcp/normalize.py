from __future__ import annotations

import hashlib
import hmac
import re
from decimal import Decimal, InvalidOperation

from .client import SmscClient, integer


def normalize(row: dict, client: SmscClient, fetched_at: str, store_text: bool) -> dict:
    identifier = str(row.get("int_id", ""))
    integer("int_id", identifier, 1, 2**64 - 1)
    sent = integer("send_timestamp", row.get("send_timestamp"), 0, 4102444800)
    parts = row.get("sms_cnt")
    parts = int(str(parts)) if not isinstance(parts, bool) and re.fullmatch(r"\d+", str(parts)) else None
    if parts is not None and not 0 <= parts <= 1000:
        parts = None
    try:
        cost = Decimal(str(row.get("cost")))
        cost = (
            str(cost) if cost.is_finite() and 0 <= cost <= Decimal("1e12") and cost.as_tuple().exponent >= -8 else None
        )
    except InvalidOperation:
        cost = None
    text = str(row.get("message") or "")
    # Families are hypotheses, not operator registration identities.
    candidate = re.sub(r"\d+", "{number}", re.sub(r"\s+", " ", text)).strip()
    family = (
        hmac.new(client.redaction_key.encode(), candidate.encode(), hashlib.sha256).hexdigest()[:32]
        if text
        else "unknown"
    )
    changed = row.get("last_timestamp")
    changed = int(str(changed)) if not isinstance(changed, bool) and re.fullmatch(r"\d+", str(changed)) else None
    if changed is not None and not 0 <= changed <= 4102444800:
        changed = None
    value = {
        "int_id": identifier,
        "send_timestamp": sent,
        "last_timestamp": changed,
        "status": str(row["status"]) if row.get("status") is not None else "unknown",
        "error": str(row["err"]) if row.get("err") is not None else "unknown",
        "operator": str(row.get("operator") or "unknown"),
        "sender": str(row.get("sender_id") or "unknown"),
        "country": str(row.get("country") or "unknown"),
        "region": str(row.get("region") or "unknown"),
        "mccmnc": str(row.get("mccmnc") or "unknown"),
        "type": str(row.get("type", "unknown")),
        "format": str(row.get("format", "unknown")),
        "cost": cost,
        "parts": parts,
        "currency": client.currency,
        "template_candidate": family,
        "text_length": len(text),
        "utf16_units": len(text.encode("utf-16-le")) // 2,
        "recipient": client.redact({"phone": row.get("phone", "")})["phone"] if row.get("phone") else "unknown",
        "text_fingerprint": hmac.new(client.redaction_key.encode(), text.encode(), hashlib.sha256).hexdigest()[:32]
        if text
        else "unknown",
        "fetched_at": fetched_at,
    }
    if store_text:
        value["message"] = client.redact({"message": text}, include_text=True)["message"]
    return value
