#!/usr/bin/env python3
"""Tests for monitor.py's auto-book path (starred slots): the wallet-guard
removal, the paid/parked/failed classification, and the email routing that
depends on it. Run with: python3 -m unittest test_monitor -v

This file exists because an /ultrareview pass on the wallet-guard-removal
commit found that WALLET_GUARD_SAFETY.md claimed "test coverage" for that
change when no test file actually existed in the repo — every prior round
of testing in this project's history was run from throwaway scripts outside
the repo and never committed. This file is the fix: a real, persisted,
rerunnable test suite for the auto-book logic, not a documentation promise.
It is not exhaustive coverage of every stage ever built, only of the
starred-slot auto-book path as it stands today.
"""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("monitor", REPO / "monitor.py")
monitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitor)


class FakeResp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


def make_config(date, starred_time, other_times=()):
    times = [{"time": starred_time, "autoBook": True}]
    for t in other_times:
        times.append({"time": t, "autoBook": False})
    return {"targets": [{"name": "Test", "date": date, "locations": ["LOC001"], "times": times}]}


def make_slot(time_name, price="500.0000", court="North-3"):
    return {
        "locId": "LOC001", "stadiumtimeId": 1, "stadiumName": court,
        "timeName": time_name, "stadiumtimePrice": price, "reservestatus": "0",
    }


def run_check_pass(config, slots, booking_response_fn):
    """booking_response_fn(url, kwargs) -> FakeResp, or raises."""
    call_log = []

    def router(url, **kwargs):
        call_log.append(url)
        if url == monitor.API_URL:
            return FakeResp(200, slots)
        return booking_response_fn(url, kwargs)

    sent = []

    def fake_send_email(subject, body, *_args):
        sent.append({"subject": subject, "body": body})

    state = {}
    with mock.patch.object(monitor.requests, "post", side_effect=router), \
         mock.patch.object(monitor, "send_email", side_effect=fake_send_email):
        monitor.run_check_pass(state, config, "fakesession", "0812345678", "u@e.com", "pw", "u@e.com")

    return state, sent, call_log


class TestClassifyPaidOrParked(unittest.TestCase):
    """The heuristic itself, in isolation."""

    def test_confirmed_insufficient_fund_pattern_is_parked(self):
        self.assertEqual(
            monitor.classify_paid_or_parked("Insufficient fund | 500.00 THB", ""),
            "parked",
        )

    def test_real_looking_payment_ref_with_no_insufficient_text_is_paid(self):
        self.assertEqual(
            monitor.classify_paid_or_parked("Payment successful", "PAYREF-ABC123"),
            "paid",
        )

    def test_ambiguous_no_insufficient_text_no_real_ref_defaults_to_parked(self):
        self.assertEqual(
            monitor.classify_paid_or_parked("Transaction processed", ""),
            "parked",
        )

    def test_empty_message_and_empty_ref_defaults_to_parked(self):
        self.assertEqual(monitor.classify_paid_or_parked("", ""), "parked")
        self.assertEqual(monitor.classify_paid_or_parked(None, None), "parked")

    def test_placeholder_payment_refs_do_not_count_as_real(self):
        # Regression: an /ultrareview finding caught that "any non-empty
        # string" was too weak a check — placeholders must be rejected.
        for placeholder in ["0", "-", "N/A", "n/a", "None", "null", "", "   "]:
            with self.subTest(placeholder=placeholder):
                self.assertEqual(
                    monitor.classify_paid_or_parked("some other text", placeholder),
                    "parked",
                    f"placeholder paymentRef {placeholder!r} must not be treated as real",
                )

    def test_non_string_message_does_not_crash(self):
        # Regression: an /ultrareview finding caught that a non-string
        # truthy message (e.g. an int) crashed .lower(). This runs AFTER
        # book_slot() has already fired the real call, so a crash here
        # would skip the autobook_attempts write entirely -- the exact
        # double-booking risk the original wallet guard existed to
        # prevent, reintroduced through a different door.
        try:
            result = monitor.classify_paid_or_parked(12345, "PAYREF-ABC123")
        except AttributeError:
            self.fail("classify_paid_or_parked() must never raise — it runs after the real booking call")
        self.assertIn(result, ("paid", "parked"))

        try:
            monitor.classify_paid_or_parked(["unexpected", "shape"], "")
        except AttributeError:
            self.fail("classify_paid_or_parked() must never raise on a non-string message")


class TestAttemptAutobookNoWalletGuard(unittest.TestCase):
    """attempt_autobook() must go straight from 'starred slot open' to
    book_slot() — no call to wallet_is_safe_to_book()/get_wallet_balance()
    anywhere in this path."""

    def test_guard_functions_are_never_called_paid_path(self):
        self._assert_guard_never_called(
            lambda url, kwargs: FakeResp(200, {
                "statusCode": "10", "message": "Payment successful",
                "bookingRef": "SPORTS-PAID", "paymentRef": "PAYREF-1",
            }),
            expected_outcome="paid",
        )

    def test_guard_functions_are_never_called_parked_path(self):
        self._assert_guard_never_called(
            lambda url, kwargs: FakeResp(200, {
                "statusCode": "10", "message": "Insufficient fund | 500.00 THB",
                "bookingRef": "SPORTS-PARK", "paymentRef": "",
            }),
            expected_outcome="parked",
        )

    def test_guard_functions_are_never_called_failed_path(self):
        def raise_timeout(url, kwargs):
            raise monitor.requests.exceptions.Timeout("timed out")
        self._assert_guard_never_called(raise_timeout, expected_outcome="failed")

    def _assert_guard_never_called(self, booking_response_fn, expected_outcome):
        config = make_config("2026-10-20", "20:00")
        k = "2026-10-20|LOC001|North-3|20:00"
        with mock.patch.object(monitor, "wallet_is_safe_to_book",
                                side_effect=AssertionError("wallet_is_safe_to_book was called")), \
             mock.patch.object(monitor, "get_wallet_balance",
                                side_effect=AssertionError("get_wallet_balance was called")):
            state, sent, calls = run_check_pass(config, [make_slot("20:00")], booking_response_fn)
        self.assertEqual(state["autobook_attempts"][k]["outcome"], expected_outcome)
        self.assertNotIn(monitor.MEMBER_INFO_URL, calls)

    def test_member_info_url_never_hit_across_any_outcome(self):
        for booking_response_fn in [
            lambda url, kwargs: FakeResp(200, {"statusCode": "10", "message": "Payment successful", "bookingRef": "R", "paymentRef": "REF1"}),
            lambda url, kwargs: FakeResp(200, {"statusCode": "10", "message": "Insufficient fund", "bookingRef": "R", "paymentRef": ""}),
            lambda url, kwargs: FakeResp(500, None),
        ]:
            config = make_config("2026-10-20", "20:00")
            _, _, calls = run_check_pass(config, [make_slot("20:00")], booking_response_fn)
            self.assertNotIn(monitor.MEMBER_INFO_URL, calls)


class TestEmailRouting(unittest.TestCase):
    """The three-way outcome split and what each produces in the inbox."""

    def test_paid_produces_distinct_email_with_raw_message_no_generic_email(self):
        config = make_config("2026-10-20", "20:00")
        booking_fn = lambda url, kwargs: FakeResp(200, {
            "statusCode": "10", "message": "Payment successful",
            "bookingRef": "SPORTS-PAID-1", "paymentRef": "PAYREF-1",
        })
        state, sent, _ = run_check_pass(config, [make_slot("20:00")], booking_fn)

        self.assertEqual(len(sent), 1)
        self.assertIn("PAID", sent[0]["subject"])
        self.assertIn("SPORTS-PAID-1", sent[0]["body"])
        self.assertIn("Payment successful", sent[0]["body"])  # raw message included

    def test_confirmed_parked_email_unchanged_no_extra_line(self):
        config = make_config("2026-10-20", "20:00")
        booking_fn = lambda url, kwargs: FakeResp(200, {
            "statusCode": "10", "message": "Insufficient fund | 500.00 THB",
            "bookingRef": "SPORTS-PARK-1", "paymentRef": "",
        })
        _, sent, _ = run_check_pass(config, [make_slot("20:00")], booking_fn)

        self.assertEqual(len(sent), 1)
        self.assertIn("already in payment pending", sent[0]["subject"])
        self.assertNotIn("booking system said", sent[0]["body"])

    def test_ambiguous_parked_shows_raw_message_not_silently_paid(self):
        config = make_config("2026-10-20", "20:00")
        booking_fn = lambda url, kwargs: FakeResp(200, {
            "statusCode": "10", "message": "Transaction processed",
            "bookingRef": "SPORTS-AMBIG-1", "paymentRef": "",
        })
        state, sent, _ = run_check_pass(config, [make_slot("20:00")], booking_fn)

        self.assertEqual(len(sent), 1)
        self.assertIn("already in payment pending", sent[0]["subject"])
        self.assertIn("Transaction processed", sent[0]["body"])
        self.assertIn("didn't match the known insufficient-fund pattern", sent[0]["body"])

    def test_ambiguous_empty_message_shows_warning_regression(self):
        # Regression for the /ultrareview finding: the warning used to be
        # gated on a truthy message, so it never fired for the empty-
        # message case — exactly the case it exists to flag.
        config = make_config("2026-10-20", "20:00")
        booking_fn = lambda url, kwargs: FakeResp(200, {
            "statusCode": "10", "message": "", "bookingRef": "R", "paymentRef": "",
        })
        _, sent, _ = run_check_pass(config, [make_slot("20:00")], booking_fn)
        self.assertEqual(len(sent), 1)
        self.assertIn("no message was returned", sent[0]["body"])

    def test_failed_shows_real_reason_not_wallet_wording(self):
        config = make_config("2026-10-20", "20:00")

        def raise_timeout(url, kwargs):
            raise monitor.requests.exceptions.Timeout("timed out talking to booking endpoint")

        _, sent, _ = run_check_pass(config, [make_slot("20:00")], raise_timeout)
        self.assertEqual(len(sent), 1)
        self.assertIn("failed", sent[0]["subject"])
        self.assertIn("timed out talking to booking endpoint", sent[0]["body"])
        self.assertNotIn("wallet balance", sent[0]["body"].lower())

    def test_failed_bad_status_code_names_the_code(self):
        config = make_config("2026-10-20", "20:00")
        booking_fn = lambda url, kwargs: FakeResp(200, {
            "statusCode": "77", "message": "some other failure", "bookingRef": None, "paymentRef": "",
        })
        _, sent, _ = run_check_pass(config, [make_slot("20:00")], booking_fn)
        self.assertEqual(len(sent), 1)
        self.assertIn("'77'", sent[0]["body"])
        self.assertIn("some other failure", sent[0]["body"])

    def test_missing_member_mobile_fails_closed_without_network_call(self):
        config = make_config("2026-10-20", "20:00")

        def should_never_be_called(url, kwargs):
            raise AssertionError(f"must not reach {url} with no member_mobile")

        state = {}
        sent = []

        def fake_send_email(subject, body, *_args):
            sent.append({"subject": subject, "body": body})

        def router(url, **kwargs):
            if url == monitor.API_URL:
                return FakeResp(200, [make_slot("20:00")])
            return should_never_be_called(url, kwargs)

        with mock.patch.object(monitor.requests, "post", side_effect=router), \
             mock.patch.object(monitor, "send_email", side_effect=fake_send_email):
            monitor.run_check_pass(state, config, "fakesession", "", "u@e.com", "pw", "u@e.com")

        k = "2026-10-20|LOC001|North-3|20:00"
        self.assertEqual(state["autobook_attempts"][k]["outcome"], "failed")
        self.assertIn("MEMBER_MOBILE", state["autobook_attempts"][k]["message"])


class TestUnstarredUnaffected(unittest.TestCase):
    """Unstarred slots must never touch any auto-book machinery at all."""

    def test_unstarred_slot_only_generic_email_no_booking_calls(self):
        config = {"targets": [{"name": "Unstarred", "date": "2026-10-21", "locations": ["LOC001"],
                                "times": [{"time": "19:00", "autoBook": False}]}]}

        def should_never_be_called(url, kwargs):
            raise AssertionError(f"unstarred slot must never touch {url}")

        state, sent, calls = run_check_pass(config, [make_slot("19:00")], should_never_be_called)

        self.assertEqual(len(sent), 1)
        self.assertIn("tennis slot(s) open", sent[0]["subject"])
        self.assertEqual(state.get("autobook_attempts", {}), {})
        self.assertEqual(calls, [monitor.API_URL])


class TestNoReattempt(unittest.TestCase):
    """A slot with an already-recorded outcome must never be re-attempted,
    across a real save_state()/load_state() file round trip."""

    def test_already_attempted_slot_blocks_reattempt_across_process_boundary(self):
        import tempfile
        config = make_config("2026-10-22", "18:00")
        k = "2026-10-22|LOC001|North-3|18:00"

        orig_state_file = monitor.STATE_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.STATE_FILE = Path(tmpdir) / "state.json"

            booking_fn = lambda url, kwargs: FakeResp(200, {
                "statusCode": "77", "message": "boom", "bookingRef": None, "paymentRef": "",
            })
            state1, _, _ = run_check_pass(config, [make_slot("18:00")], booking_fn)
            monitor.save_state(state1)

            state2 = monitor.load_state()
            state2["known_open"] = {}  # simulate the slot closing then reopening

            def should_never_be_called(url, kwargs):
                raise AssertionError(f"already-attempted slot must never touch {url}")

            call_log = []

            def router(url, **kwargs):
                call_log.append(url)
                if url == monitor.API_URL:
                    return FakeResp(200, [make_slot("18:00")])
                return should_never_be_called(url, kwargs)

            sent2 = []
            with mock.patch.object(monitor.requests, "post", side_effect=router), \
                 mock.patch.object(monitor, "send_email", side_effect=lambda s, b, *a: sent2.append((s, b))):
                monitor.run_check_pass(state2, config, "fakesession", "0812345678", "u@e.com", "pw", "u@e.com")

        monitor.STATE_FILE = orig_state_file

        self.assertNotIn(monitor.BOOKING_TX_URL, call_log)
        self.assertEqual(state2["autobook_attempts"][k]["outcome"], "failed")
        self.assertTrue(any("tennis slot(s) open" in s for s, b in sent2))
        self.assertFalse(any("failed" in s and "starred" in b for s, b in sent2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
