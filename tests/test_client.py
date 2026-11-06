from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from smsc_mcp import analysis, catalog, client, history, policy


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.smsc = client
        self.catalog = catalog
        self.history = history
        self.analysis = analysis
        self.store = policy
        self.resource = {"apikey": "synthetic-api-key", "currency": "RUB", "timezone": "Europe/Moscow"}

    def client(self, **extra):
        return client.SmscClient({**self.resource, **extra}, reserve_request=Mock())

    def response(self, value):
        return json.dumps(value, ensure_ascii=False).encode(), "application/json"

    def test_all_catalogued_reads_reject_command_auth_format_and_cost_overrides(self):
        for operation, _spec in self.catalog.OPERATIONS.items():
            for parameter in (
                "login",
                "psw",
                "apikey",
                "password",
                "charset",
                "fmt",
                "cost",
                "del",
                "add",
                "get_stat",
                "base_url",
                "endpoint",
                "method",
                "headers",
                "fl2[27]",
            ):
                with self.subTest(operation=operation, parameter=parameter), self.assertRaises(ValueError):
                    self.smsc.validate(operation, {parameter: "1"})

    def test_provider_errors_http_redirects_and_timeouts_never_retry_or_disclose_payload(self):
        for value in (
            {"error": "synthetic-api-key private text", "error_code": 9},
            [{"error": "private", "error_code": 2}],
            {"error": "private"},
        ):
            with self.subTest(value=value), patch.object(self.smsc, "_post", return_value=self.response(value)) as post:
                with self.assertRaises(self.smsc.SmscError) as caught:
                    self.client().execute("balance")
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("synthetic-api-key", str(caught.exception))
                self.assertEqual(post.call_count, 1)
        for error in (
            HTTPError("https://example.invalid/secret", 302, "private", {}, None),
            HTTPError("https://example.invalid/secret", 429, "private", {}, None),
            URLError("synthetic-api-key"),
            TimeoutError("synthetic-api-key"),
        ):
            opener = Mock()
            opener.open.side_effect = error
            with patch.object(self.smsc, "build_opener", return_value=opener):
                with self.assertRaises(self.smsc.SmscError) as caught:
                    self.smsc._post("https://smsc.ru/sys/balance.php", b"body", "application/json", 5)
                self.assertNotIn("synthetic-api-key", str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)
        self.assertIsNone(self.smsc.NoRedirect().redirect_request(None, None, 302, "", {}, "https://bad.invalid"))

    def test_transport_is_post_and_bounds_response(self):
        response = Mock()
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b"{}"
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(self.smsc, "build_opener", return_value=opener):
            self.smsc._post("https://smsc.ru/sys/balance.php", b"apikey=test", "application/json", 5)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        response.read.assert_called_once_with(self.smsc.MAX_RESPONSE + 1)
        response.read.return_value = b"x" * (self.smsc.MAX_RESPONSE + 1)
        with patch.object(self.smsc, "build_opener", return_value=opener), self.assertRaises(self.smsc.SmscError):
            self.smsc._post("https://smsc.ru/sys/balance.php", b"", "application/json", 5)

    def test_statistics_semantics_and_cp1251_response(self):
        data = '[{"sms": "10", "credit": "100.50", "debit": "200", "currency": "RUB", "name": "Проверка"}]'.encode(
            "cp1251"
        )
        with patch.object(self.smsc, "_post", return_value=(data, "application/json")):
            result = self.client().execute("statistics", {"start": "01.09.2026", "end": "30.09.2026"})
        self.assertEqual(result["data"][0]["credit"], "100.50")
        self.assertEqual(result["field_semantics"]["credit"], "expense")
        self.assertEqual(result["field_semantics"]["debit"], "receipts")
        self.assertEqual(result["data"][0]["name"], "Проверка")

    def test_history_start_requires_end_before_network(self):
        with patch.object(self.smsc, "_post") as post:
            with self.assertRaisesRegex(ValueError, "history_end_required"):
                self.client().execute("messages", {"start": "01.01.2020"})
            post.assert_not_called()
        self.smsc.validate("messages", {})  # Bounded latest page still needs no dates.

    def test_multi_status_budget_covers_full_minute_even_with_trailing_comma(self):
        spec, params = self.smsc.validate("status", {"phone": "79999999999,", "id": "10,"})
        self.assertEqual(params["id"], "10,")
        state = self.store.budget_state({}, 100, "status", spec.min_interval)
        for now in (121, 160):
            with self.subTest(now=now), self.assertRaises(RuntimeError):
                self.store.budget_state(state, now, "status", spec.min_interval)
        self.store.budget_state(state, 161, "status", spec.min_interval)

    def test_only_documented_outbox_not_found_is_an_empty_page(self):
        with patch.object(
            self.smsc, "_post", return_value=self.response({"error": "not found", "error_code": 3})
        ) as post:
            page = self.history.fetch_history(self.client(), "01.09.2026", "01.09.2026")
            self.assertTrue(page["complete"])
            self.assertEqual(page["rows"], [])
            self.assertEqual(post.call_count, 1)
            with self.assertRaises(self.smsc.SmscError):
                self.client().execute("status", {"phone": "79999999999", "id": 1})
        for response in (
            {"error": "auth", "error_code": 2},
            {"error": "blocked", "error_code": 4},
            {"error": "rate", "error_code": 9},
            [{"int_id": "1"}, {"error": "not found", "error_code": 3}],
        ):
            with (
                self.subTest(response=response),
                patch.object(self.smsc, "_post", return_value=self.response(response)),
            ):
                with self.assertRaises(self.smsc.SmscError):
                    self.history.fetch_history(self.client(), "01.09.2026", "01.09.2026")

    def test_redaction_is_keyed_stable_and_secrets_never_return_even_with_opt_in(self):
        data = {
            "phone": "79999999999",
            "message": "OTP 123456",
            "email": "test@example.invalid",
            "apikey": "synthetic-api-key",
            "nested": [{"text": "key=synthetic-api-key"}],
        }
        client = self.client()
        redacted = client.redact(data)
        self.assertEqual(redacted["phone"], client.redact(data)["phone"])
        self.assertNotEqual(redacted["phone"], self.client(redaction_key="other").redact(data)["phone"])
        self.assertNotIn("79999999999", json.dumps(redacted))
        self.assertNotIn("123456", json.dumps(redacted))
        self.assertNotIn("test@example.invalid", json.dumps(redacted))
        self.assertNotIn("synthetic-api-key", json.dumps(redacted))
        opted_in = client.redact(data, include_text=True, include_personal_data=True)
        self.assertEqual(opted_in["message"], "OTP 123456")
        self.assertEqual(opted_in["phone"], "79999999999")
        self.assertNotIn("synthetic-api-key", json.dumps(opted_in))

    def test_history_reports_partial_and_stops_on_stalled_or_missing_cursor(self):
        client = self.client()
        with patch.object(self.smsc, "_post", return_value=self.response([{"int_id": "42"}])):
            result = self.history.fetch_history(client, "01.09.2026", "02.09.2026", page_size=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["next_cursor"], "42")
        for batch, kwargs in (([{"id": 4}], {}), ([{"int_id": "42"}], {"prev_id": "42"})):
            with (
                patch.object(self.smsc, "_post", return_value=self.response(batch)),
                self.assertRaises(self.smsc.SmscError),
            ):
                self.history.fetch_history(self.client(), "01.09.2026", "02.09.2026", page_size=1, **kwargs)
        with self.assertRaises(ValueError):
            self.history.fetch_history(self.client(), "01.09.2026", "02.09.2026", max_pages=4)

        for batch, kwargs in (
            ([{"int_id": "42"}], {"page_size": 2, "prev_id": "42"}),
            ([{"int_id": "42"}, {"int_id": "41"}], {"page_size": 1}),
        ):
            with (
                patch.object(self.smsc, "_post", return_value=self.response(batch)),
                self.assertRaises(self.smsc.SmscError),
            ):
                self.history.fetch_history(self.client(), "01.09.2026", "02.09.2026", **kwargs)

    def test_analytics_decimal_cost_currency_parts_and_possible_repeats(self):
        rows = [
            {"int_id": "1", "cost": "0.10", "sms_cnt": 2, "phone": "7", "message": "Code 123", "send_timestamp": 100},
            {"int_id": "2", "cost": "0.20", "sms_cnt": 1, "phone": "7", "message": "Code 123", "send_timestamp": 150},
            {"int_id": "2", "cost": "0.20", "sms_cnt": 1, "phone": "7", "message": "Code 123", "send_timestamp": 150},
            {
                "int_id": "3",
                "cost": "0.40",
                "sms_cnt": 1,
                "phone": "7",
                "message": "Code 789",
                "send_timestamp": 160,
                "currency": "USD",
            },
            {"int_id": "4", "cost": "NaN", "sms_cnt": None},
        ]
        result = self.analysis.analyze_messages(rows, currency="RUB", timezone="unknown", complete=False)
        self.assertEqual(result["totals_by_currency"]["RUB"]["known_cost"], "0.30")
        self.assertEqual(result["totals_by_currency"]["USD"]["known_cost"], "0.40")
        self.assertEqual(result["totals_by_currency"]["RUB"]["known_parts"], 3)
        self.assertEqual(result["totals_by_currency"]["RUB"]["missing_cost"], 1)
        self.assertEqual(result["totals_by_currency"]["RUB"]["missing_parts"], 1)
        self.assertEqual(result["potential_repeats_within_5_minutes"], 1)
        self.assertEqual(result["multipart_messages"], 1)
        self.assertEqual(result["duplicate_export_rows_removed"], 1)
        self.assertFalse(result["complete"])
        self.assertFalse(result["template_candidates"])
        self.assertNotIn("Code 123", json.dumps(result))

    def test_binary_documents_and_exports_validate_type_and_error(self):
        pdf = b"%PDF-1.7\nsynthetic"
        result = self.smsc._decode(pdf, "application/pdf", "pdf")
        self.assertEqual(base64.b64decode(result["data_base64"]), pdf)
        for body in (b"<html>login</html>", b"ERROR = 2", b"not a pdf"):
            with self.assertRaises(self.smsc.SmscError):
                self.smsc._decode(body, "text/html", "pdf")
        with self.assertRaises(self.smsc.SmscError):
            self.smsc._decode(b'{"error":"secret","error_code":2}', "application/json", "pdf")
        with self.assertRaises(PermissionError), patch.object(self.smsc, "_post") as post:
            self.client().execute("invoice", {"user": "child", "id": 42})
        post.assert_not_called()
        self.assertEqual(self.smsc._decode(b"a,b\n1,2", "text/csv", "csv")["content_type"], "text/csv")
        with self.assertRaises(self.smsc.SmscError):
            self.smsc._decode(b"<html>", "application/octet-stream", "csv")
        multipart = (
            b'--abc\r\nContent-Type: application/json\r\n\r\n{"id":4,"sum":"100"}\r\n'
            b"--abc\r\nContent-Type: application/pdf\r\n\r\n%PDF-1.7\nsynthetic\r\n--abc--\r\n"
        )
        result = self.smsc._decode(multipart, "multipart/mixed; boundary=abc", "multipart")
        self.assertEqual(result["metadata"]["id"], 4)
        self.assertTrue(base64.b64decode(result["data_base64"]).startswith(b"%PDF-"))
