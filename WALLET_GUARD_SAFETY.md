# Wallet balance guard — safety design & verification

`get_wallet_balance()` and `wallet_is_safe_to_book()` in `monitor.py` exist to
answer one question, correctly, every single time: is it safe to fire the
`bookingTransactions` call for a starred slot right now?

That call uses `paymentType: WALLET`. If the wallet balance is exactly
`"0.00"` at the moment it fires, the call fails on purpose ("Insufficient
fund") and the slot gets parked in pending payment for ~15–20 min — the whole
point of this feature. If the balance is ever non-zero, the *same call*
likely pays for real, automatically, with no confirmation step. See the
auto-book spec, Section 6, for the full rationale — this file only covers the
implementation and how it was verified.

## Design principle: fail closed, always

`wallet_is_safe_to_book()` has exactly one path to `True`: the balance read
succeeded *and* the value is an exact string match to `"0.00"`. Every other
outcome — a network error, a bad HTTP status, an unparseable body, a
response with the wrong shape, a missing/null/blank `walletBalance` field, a
near-miss value like `"0.0"`, or a completely unanticipated exception — falls
through to `False`. There is no default branch that assumes safety; ambiguity
is always treated as "do not proceed."

`get_wallet_balance()` backs this up structurally: it cannot raise past its
own boundary. A final `except Exception` catches anything unanticipated and
converts it into the same `(None, reason)` shape as every other failure, so
`wallet_is_safe_to_book()` never has to guess whether an exception means
"unsafe" — every failure, known or not, arrives through the same channel.

This is a deliberate deviation from `fetch_slots()`'s narrower
`except requests.RequestException` / `except json.JSONDecodeError` shape
elsewhere in this file. That's fine there — a failed poll just costs one
missed cycle. It's not fine here, where an uncaught exception propagating
past this function could leave a caller in an ambiguous state right before a
money-moving call.

## Full failure-mode test matrix

Verified by actually mocking `requests.post` and exercising each case, not
by inspection alone (see "a bug in verifying this," below, for why that
distinction mattered).

| Scenario | What happens inside `get_wallet_balance()` | Result |
|---|---|---|
| Clean `"0.00"` | Passes all checks | `wallet_is_safe_to_book() == True` — the only green light |
| Non-zero balance (e.g. `"50.00"`) | Read succeeds, but `balance != "0.00"` | False |
| Request timeout | `requests.exceptions.Timeout` → `except requests.RequestException` → `"network: ..."` | False |
| Connection refused / DNS failure | Same `RequestException` branch | False |
| Non-200 HTTP status (500, 403, ...) | `resp.status_code != 200` check | False |
| Malformed/non-JSON body (e.g. an HTML login page after session expiry) | `resp.json()` raises `JSONDecodeError` → `"session_expired"` | False |
| Valid JSON but wrong shape (a list, a bool, not a dict) | `isinstance(data, dict)` check → `"session_expired"` | False |
| `walletBalance` key missing | `.get()` returns `None` → fails the `isinstance(balance, str)` check | False |
| `walletBalance` explicitly `null` | Same as missing | False |
| `walletBalance` blank/whitespace string | `.strip()` is falsy | False |
| `walletBalance` wrong type (a number, not a string) | Fails `isinstance(balance, str)` | False |
| Near-miss values (`"0.0"`, `"0"`, `" 0.00 "`) | Read succeeds, but exact string compare fails | False — deliberately strict, no numeric coercion |
| Missing `member_mobile` (caller misconfiguration) | Short-circuits before any network call | False |
| Any unanticipated exception (a bug, not a `requests` error) | Caught by the final `except Exception` | False |

## A bug in verifying this, worth remembering

The first test pass for the timeout/connection-error cases used a harness
that branched on `callable(mock_post_effect)` to decide whether to pass an
exception as `side_effect` or `return_value` to `mock.patch.object`.
Exception *instances* (e.g. `requests.exceptions.Timeout("timed out")`) are
not callable, so that check routed them into `return_value` instead —
meaning `requests.post` returned the exception object itself instead of
raising it. `resp.status_code` was then accessed on a `Timeout` instance,
which raised `AttributeError`, which was in turn caught by the generic
`except Exception` fallback. The test still reported the correct final
result (unsafe), but for the wrong reason — it never actually exercised the
`except requests.RequestException` branch it claimed to test.

Lesson for anyone re-verifying this function later: when mocking
`requests.post` to simulate a network failure, always pass the exception via
`side_effect`, never `return_value` — `unittest.mock`'s `side_effect`
handles both exception instances/classes (raises them) and callables (calls
them) correctly on its own; a manual `callable()` branch is redundant and,
as above, actively wrong for exception instances. A passing test with the
right final answer is not the same as a test that exercised the code path it
claims to.

## Known follow-up: `MEMBER_MOBILE` (Stage 4 dependency)

`get_wallet_balance()` and the future `bookingTransactions` call both need
the member's mobile number, which today lives in neither `config.json` nor
an env var. Stage 4 will need to add a `MEMBER_MOBILE` secret, following the
same pattern as `PHPSESSID`/`EMAIL_USER`/`EMAIL_APP_PASSWORD` in
`monitor.yml` and `main()`'s env var loading. Not solved here — this stage
is standalone and unwired by design — just tracked so it isn't rediscovered
from scratch.
