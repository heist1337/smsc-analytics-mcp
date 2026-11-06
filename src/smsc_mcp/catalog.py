"""Explicit SMSC HTTP API capability and parameter catalog (read-only revision 2026-10-11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DOCS = "https://smsc.ru/api/http/"


@dataclass(frozen=True)
class Operation:
    endpoint: str
    command: dict[str, int] = field(default_factory=dict)
    parameters: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    one_of: tuple[tuple[str, ...], ...] = ()
    response: str = "json"
    min_interval: int = 21
    notes: str = ""


def op(
    endpoint: str,
    command: str = "",
    parameters: str = "",
    required: str = "",
    *,
    one_of: tuple[tuple[str, ...], ...] = (),
    response: str = "json",
    min_interval: int = 21,
    notes: str = "",
) -> Operation:
    name, _, value = command.partition("=")
    return Operation(
        endpoint,
        {name: int(value or 1)} if name else {},
        tuple(parameters.split()),
        tuple(required.split()),
        one_of,
        response,
        min_interval,
        notes,
    )


USER_STAT = "start end user user_id reseller hide_deleted mycur balance2"
DOC_USER = (("user", "user_id"),)

OPERATIONS: dict[str, Operation] = {
    "balance": op("balance.php", parameters="cur", min_interval=7),
    "statistics": op(
        "get.php",
        "get_stat",
        "start end mycur balance2",
        "start",
        notes="credit=expense; debit=receipts; sms counting unit needs account reconciliation.",
    ),
    "prices": op("get.php", "get_price"),
    "messages": op(
        "get.php",
        "get_messages",
        "start end phone email format cnt prev_id",
        notes="One page, <=1000 rows; <=30 requests/hour for this account; <=6-month query span.",
    ),
    "incoming": op(
        "get.php", "get_answers", "cnt hour after_id to_phone", notes="One page, <=10000 rows, <=168 hours."
    ),
    "status": op(
        "status.php",
        parameters="phone id bot all",
        required="phone id",
        min_interval=61,
        notes="Conservative one request/minute, including list or trailing-comma multi-status queries.",
    ),
    "operator": op(
        "info.php", "get_operator", "phone", "phone", notes="Prefix-based operator information excludes ported numbers."
    ),
    "short_link": op("tinyurls.php", "get", "link id", one_of=(("link", "id"),)),
    "templates": op("templates.php", "get"),
    "operator_template": op(
        "op_templates.php",
        "get",
        "sender msg",
        "sender msg",
        notes="Query a known sender+template; no documented enumerate-all operation.",
    ),
    "senders": op("senders.php", "get", "all sender id user operators from_date prev_id cnt files"),
    "campaign": op("jobs.php", "get", "id", "id"),
    "campaigns": op("jobs.php", "get_all"),
    "subclient_statistics": op("users.php", "get_stat", USER_STAT, "start"),
    "subclient_statistics_detailed": op("users.php", "get_stat_all", USER_STAT, "start"),
    "invoice": op(
        "documents.php", "get_account", "user user_id id", "id", one_of=DOC_USER, response="pdf", min_interval=61
    ),
    "act": op(
        "documents.php",
        "get_act",
        "user user_id id num",
        one_of=(*DOC_USER, ("id", "num")),
        response="pdf",
        min_interval=61,
    ),
    "rental_numbers": op(
        "receive_phones.php",
        "get",
        min_interval=31,
        notes="Available numbers for rental; does not rent or establish an already-rented inventory.",
    ),
    "downloads": op("downloads.php", "get_list", "cnt after_id"),
    "download_file": op("downloads.php", "get_file", "id name", one_of=(("id", "name"),), response="csv"),
}


def catalog() -> dict[str, Any]:
    return {
        "documentation": DOCS,
        "source_read_only": True,
        "operations": {
            name: {
                "endpoint": spec.endpoint,
                "command": spec.command,
                "parameters": list(spec.parameters),
                "required": list(spec.required),
                "one_of": list(spec.one_of),
                "response": spec.response,
                "notes": spec.notes,
            }
            for name, spec in OPERATIONS.items()
        },
    }
