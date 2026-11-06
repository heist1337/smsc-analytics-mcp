"""Bounded SMSC HTTP client. Fixed commands, POST-only credentials, no automatic retries."""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import math
import re
from datetime import datetime
from email import policy
from email.parser import BytesParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalog import OPERATIONS, Operation

MAX_RESPONSE = 8 * 1024 * 1024
MAX_REQUEST = 8 * 1024 * 1024
SERVERS = {"primary": "https://smsc.ru/sys/", "reserve": "https://www2.smsc.ru/sys/"}
PRIVATE_KEYS = {"login", "apikey", "psw", "password", "pswc", "token", "secret", "subclient_password"}
PHONE_KEYS = {"phone", "phones", "to_phone", "new_phone", "phone2", "pho", "all_phones"}
TEXT_KEYS = {
    "message",
    "mes",
    "mes2",
    "msg",
    "example",
    "answ",
    "answ_call",
    "code",
    "subject",
    "comment",
    "dtmf",
    "text",
}
IDENTITY_KEYS = {
    "email",
    "mail",
    "new_mail",
    "email2",
    "fio",
    "fnm",
    "mnm",
    "lnm",
    "bd",
    "address",
    "props",
    "cmt",
    "notes",
    "emails",
    "imsi",
    "msc",
}


class SmscError(RuntimeError):
    """Safe errors never include the provider response or request credentials."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def integer(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{name}_must_be_integer")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name}_out_of_range")
    return result


def _date(name: str, value: str):
    try:
        result = datetime.strptime(value, "%d.%m.%Y").date()
        if result.strftime("%d.%m.%Y") != value:
            raise ValueError
        return result
    except ValueError:
        raise ValueError(f"{name}_must_be_dd_mm_yyyy") from None


def validate(operation: str, parameters: dict[str, Any] | None) -> tuple[Operation, dict[str, str]]:
    if operation not in OPERATIONS:
        raise ValueError("operation_not_allowed")
    spec = OPERATIONS[operation]
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ValueError("parameters_must_be_object")
    if set(parameters) - set(spec.parameters):
        raise ValueError("parameter_not_allowed")
    normalized = {}
    for key, value in parameters.items():
        if not isinstance(value, (str, int, float, bool)) or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError("parameter_must_be_scalar")
        value = str(int(value)) if isinstance(value, bool) else str(value)
        if "\x00" in value or len(value.encode("utf-8")) > 100_000:
            raise ValueError("parameter_too_large_or_invalid")
        normalized[key] = value
    if any(not normalized.get(key) for key in spec.required):
        raise ValueError("required_parameter_missing")
    if any(not any(normalized.get(key) for key in group) for group in spec.one_of):
        raise ValueError("alternative_parameter_missing")
    for key in ("start", "end", "from_date", "bd"):
        if key in normalized:
            _date(key, normalized[key])
    if "end" in normalized and "start" not in normalized:
        raise ValueError("start_required_with_end")
    if "start" in normalized:
        if operation == "messages" and "end" not in normalized:
            raise ValueError("history_end_required_with_start")
        start = _date("start", normalized["start"])
        end = _date("end", normalized["end"]) if "end" in normalized else start
        if end < start:
            raise ValueError("date_range_reversed")
        if operation == "messages":
            month = start.month + 6
            year, month = start.year + (month - 1) // 12, (month - 1) % 12 + 1
            boundary = start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))
            if end > boundary:
                raise ValueError("history_span_exceeds_six_months")
    # Every paginated read has an explicit bounded page size, even when omitted by the caller.
    if "cnt" in spec.parameters and "cnt" not in normalized:
        normalized["cnt"] = "1000"
    if "cnt" in normalized:
        maximum = 15000 if operation == "senders" else 10000 if operation == "incoming" else 1000
        normalized["cnt"] = str(integer("cnt", parameters.get("cnt", 1000), 1, maximum))
    if "hour" in normalized:
        integer("hour", parameters["hour"], 1, 168)
    for key in ("prev_id", "after_id"):
        if key in normalized:
            integer(key, parameters[key], 1, 2**64 - 1)
    if "format" in normalized and operation == "messages" and normalized["format"] not in {"0", "8"}:
        raise ValueError("history_format_not_allowed")
    if "mes" in normalized and len(normalized["mes"]) > 1000:
        raise ValueError("message_exceeds_1000_characters")
    return spec, normalized


def _post(endpoint: str, body: bytes, content_type: str, timeout: int) -> tuple[bytes, str]:
    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json, application/pdf, text/csv"},
        method="POST",
    )
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise SmscError("response_too_large")
            return data, response.headers.get("Content-Type", "")
    except HTTPError as error:
        raise SmscError(f"smsc_http_{error.code}; no automatic retry") from None
    except (URLError, TimeoutError, OSError):
        raise SmscError("smsc_transport_failure; no automatic retry") from None


def _json(data: bytes) -> Any:
    try:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1251")
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise SmscError("smsc_invalid_json; no response body disclosed") from None
    if not isinstance(value, (dict, list)):
        raise SmscError("smsc_unexpected_json_shape")
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if isinstance(entry, dict) and ("error" in entry or "error_code" in entry):
            code = str(entry.get("error_code", "unknown"))
            code = code if re.fullmatch(r"\d{1,3}", code) else "unknown"
            raise SmscError(f"smsc_api_error_{code}; no automatic retry", code=code if len(entries) == 1 else None)
    return value


def _decode(data: bytes, content_type: str, kind: str) -> Any:
    if kind == "json" or data.lstrip().startswith((b"{", b"[")):
        return _json(data)
    if kind == "multipart":
        if not content_type.lower().startswith("multipart/"):
            raise SmscError("smsc_expected_multipart_invoice")
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + data
        )
        if not message.is_multipart():
            raise SmscError("smsc_invalid_multipart_invoice")
        metadata = None
        document = None
        for part in message.iter_parts():
            payload = part.get_payload(decode=True) or b""
            if part.get_content_type() == "application/json":
                metadata = _json(payload)
            elif part.get_content_type() == "application/pdf" and payload.startswith(b"%PDF-"):
                document = base64.b64encode(payload).decode()
        if metadata is None or document is None:
            raise SmscError("smsc_incomplete_multipart_invoice")
        return {"metadata": metadata, "content_type": "application/pdf", "data_base64": document}
    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise SmscError("smsc_expected_pdf")
        return {"content_type": "application/pdf", "data_base64": base64.b64encode(data).decode()}
    if kind == "csv":
        # Never return HTML login/error pages as successful exports.
        if "csv" not in content_type.lower() and "octet-stream" not in content_type.lower():
            raise SmscError("smsc_expected_csv_download")
        if data.lstrip().lower().startswith((b"<", b"error")):
            raise SmscError("smsc_invalid_csv_download")
        return {"content_type": "text/csv", "data_base64": base64.b64encode(data).decode()}
    raise SmscError("response_kind_not_allowed")


class SmscClient:
    def __init__(self, resource: dict[str, Any], *, reserve_request):
        self._reserve_request = reserve_request
        if not isinstance(resource, dict):
            raise ValueError("smsc_resource_unavailable")
        server = resource.get("server", "primary")
        if server not in SERVERS:
            raise ValueError("smsc_server_not_allowed")
        self.base = SERVERS[server]
        key, login, password = (resource.get(name) for name in ("apikey", "login", "psw"))

        def provisioned(value):
            return isinstance(value, str) and bool(value.strip()) and not value.startswith("$var:")

        if key:
            if login or password:
                raise ValueError("smsc_authentication_ambiguous")
            if not provisioned(key):
                raise ValueError("smsc_api_key_not_provisioned")
            self.auth = {"apikey": key}
            redaction_fallback = key
        else:
            if not provisioned(login) or not provisioned(password):
                raise ValueError("smsc_login_password_not_provisioned")
            self.auth = {"login": login, "psw": password}
            redaction_fallback = password
        self.redaction_key = resource.get("redaction_key") or redaction_fallback
        if not isinstance(self.redaction_key, str):
            raise ValueError("invalid_redaction_key")
        self.timeout = integer("timeout_seconds", resource.get("timeout_seconds", 20), 1, 60)
        self.currency = resource.get("currency", "unknown")
        self.timezone = resource.get("timezone", "unknown")
        if any(
            key in resource
            for key in ("allow_mutations", "enabled_operations", "subclient_password", "subclient_current_password")
        ):
            raise ValueError("administrative_resource_fields_forbidden")
        self.resource = resource

    def redact(
        self, value: Any, *, include_text: bool = False, include_personal_data: bool = False, operation: str = ""
    ) -> Any:
        secrets = [
            *self.auth.values(),
            self.redaction_key,
            self.resource.get("subclient_password", ""),
            self.resource.get("subclient_current_password", ""),
        ]

        def walk(item, field=""):
            if field.lower() in PRIVATE_KEYS:
                return "[redacted]"
            if not include_text and field.lower() in TEXT_KEYS:
                return "[text withheld]"
            private_contact_name = field.lower() == "name" and operation in {"contacts", "email_contacts"}
            # get_price.mail is a monetary tariff, not a recipient address.
            personal_field = field.lower() in PHONE_KEYS | IDENTITY_KEYS
            if operation == "prices" and field.lower() == "mail" and re.fullmatch(r"\d+(?:[.,]\d+)?", str(item)):
                personal_field = False
            if not include_personal_data and (personal_field or private_contact_name):
                # Stable keyed identifier supports duplicate analysis without disclosing the recipient.
                token = hmac.new(self.redaction_key.encode(), str(item).encode(), hashlib.sha256).hexdigest()[:20]
                return f"recipient:{token}"
            if isinstance(item, dict):
                return {k: walk(v, k) for k, v in item.items()}
            if isinstance(item, list):
                return [walk(v, field) for v in item]
            if isinstance(item, str):
                for secret in secrets:
                    if secret:
                        item = item.replace(secret, "[redacted]")
            return item

        return walk(value)

    def execute(
        self, operation: str, parameters=None, *, include_text: bool = False, include_personal_data: bool = False
    ) -> dict[str, Any]:
        spec, normalized = validate(operation, parameters)
        if operation in {"invoice", "act", "download_file"} and not include_personal_data:
            raise PermissionError("binary_export_requires_personal_data_opt_in")
        request_parameters = {
            **normalized,
            **{k: str(v) for k, v in spec.command.items()},
            **self.auth,
            "charset": "utf-8",
            "fmt": "3",
        }
        body, content_type = urlencode(request_parameters).encode(), "application/x-www-form-urlencoded; charset=utf-8"
        if len(body) > MAX_REQUEST:
            raise ValueError("smsc_request_too_large")
        # Fail closed: shared reservation covers every public tool and every job.
        self._reserve_request(operation, spec.min_interval)
        raw, response_type = _post(self.base + spec.endpoint, body, content_type, self.timeout)
        try:
            data = _decode(raw, response_type, spec.response)
        except SmscError as error:
            # Outbox documents code 3 as no matching message. Other endpoints and
            # errors remain failures; never retry or interpret auth/rate errors as empty.
            if operation != "messages" or error.code != "3":
                raise
            data = []
        if "cnt" in normalized and (not isinstance(data, list) or len(data) > int(normalized["cnt"])):
            raise SmscError("smsc_paginated_read_unexpected_shape_or_size")
        if operation == "statistics":
            # Preserve original field names; add explicit semantics without changing units.
            semantics = {"credit": "expense", "debit": "receipts", "sms": "provider_reported_count"}
        else:
            semantics = {}
        result = {
            "source": "smsc_live",
            "account": "insurance_lm",
            "operation": operation,
            "fetched_at": datetime.now().astimezone().isoformat(),
            "currency": self.currency,
            "timezone": self.timezone,
            "field_semantics": semantics,
            "data": self.redact(
                data, include_text=include_text, include_personal_data=include_personal_data, operation=operation
            ),
            "mutated": False,
        }
        if isinstance(data, list) and "cnt" in normalized:
            full = len(data) >= int(normalized["cnt"])
            cursor_key = "int_id" if operation in {"messages", "contacts", "email_contacts"} else "id"
            cursor_parameter = "prev_id" if "prev_id" in spec.parameters else "after_id"
            identifiers = [str(row.get(cursor_key, "")) for row in data if isinstance(row, dict)]
            cursor = None
            if identifiers and all(identifier.isdigit() for identifier in identifiers):
                choose = min if cursor_parameter == "prev_id" else max
                cursor = str(choose(int(identifier) for identifier in identifiers))
            result.update(
                page_full=full,
                complete=False if full else True,
                next_cursor=cursor,
                cursor_parameter=cursor_parameter,
                completeness_scope="Single filtered page; empty/short response is not proof of archive retention.",
            )
        return result
