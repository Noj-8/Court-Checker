#!/usr/bin/env python3
"""
Crystal Sports Tennis Court Monitor — Cloud edition.

Runs N check passes (default 4) with M-second sleeps between them (default 60),
then exits. Designed for GitHub Actions cron at */5 minutes — the inner loop
provides effective 60-second polling within each scheduled run.

  Reads config from:    config.json
  Reads secrets from:   PHPSESSID, EMAIL_USER, EMAIL_APP_PASSWORD env vars
  Reads loop tuning:    LOOP_ITERATIONS, LOOP_INTERVAL_SECONDS env vars (optional)
  Reads/writes state:   state.json
"""

import json
import os
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"

API_URL = (
    "https://crystalsports-booking.kegroup.co.th"
    "/api_helper.php?action=getAvailableStadiums"
)
MEMBER_INFO_URL = (
    "https://crystalsports-booking.kegroup.co.th"
    "/api_helper.php?action=getMemberInfo"
)
BOOKING_URL = "https://crystalsports-booking.kegroup.co.th/booking.php"
SAFE_WALLET_BALANCE = "0.00"
BOOKED_STATUS = "1"
TZ = ZoneInfo("Asia/Bangkok")

LOCATIONS = {
    "LOC001": "Crystal Sports",
    "LOC002": "Crystal Sports G",
}

HEADERS = {
    "accept": "*/*",
    "content-type": "application/json; charset=UTF-8",
    "origin": "https://crystalsports-booking.kegroup.co.th",
    "referer": BOOKING_URL,
    "x-requested-with": "XMLHttpRequest",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
}


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def normalize_times(times):
    """Accept either a legacy plain time string or a {time, autoBook} object
    per entry, and always return the latter. Lets old and new config.json
    shapes coexist indefinitely — targets aren't rewritten until edited
    through the dashboard, and plain strings just mean autoBook=False."""
    normalized = []
    for t in times:
        if isinstance(t, str):
            normalized.append({"time": t, "autoBook": False})
        else:
            normalized.append({"time": t["time"], "autoBook": bool(t.get("autoBook", False))})
    return normalized


def load_config():
    if not CONFIG_FILE.exists():
        log(f"ERROR: {CONFIG_FILE.name} not found")
        sys.exit(1)
    config = json.loads(CONFIG_FILE.read_text())
    for target in config.get("targets", []):
        target["times"] = normalize_times(target["times"])
    return config


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_slots(date, loc_id, phpsessid):
    try:
        resp = requests.post(
            API_URL,
            headers=HEADERS,
            cookies={"PHPSESSID": phpsessid},
            json={"date": date, "locId": loc_id},
            timeout=20,
        )
    except requests.RequestException as e:
        return None, f"network: {e}"
    if resp.status_code != 200:
        return None, f"http {resp.status_code}"
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return None, "session_expired"
    if not isinstance(data, list):
        return None, "session_expired"
    return data, None


def get_wallet_balance(member_mobile, phpsessid):
    """Fetch the member's current wallet balance via getMemberInfo.

    Mirrors fetch_slots()'s shape exactly: same request pattern, same
    (value, err) return contract. err is None only when `value` is a
    validated, trustworthy balance string — every other outcome (network
    failure, bad status, unparseable body, wrong shape, missing/null/blank
    field) returns (None, reason). Never raises: any unanticipated
    exception is itself treated as a failed read, not a crash, so a caller
    that only checks "was err set" can never be fooled into treating a
    freak error as a clean result.

    See WALLET_GUARD_SAFETY.md for the full failure-mode test matrix this
    contract was verified against, and why every branch below fails closed
    on purpose — do not "simplify" this without reading that first.
    """
    if not member_mobile:
        return None, "missing member_mobile"
    try:
        resp = requests.post(
            MEMBER_INFO_URL,
            headers=HEADERS,
            cookies={"PHPSESSID": phpsessid},
            json={"memberMobile": member_mobile},
            timeout=20,
        )
        if resp.status_code != 200:
            return None, f"http {resp.status_code}"
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None, "session_expired"
        if not isinstance(data, dict):
            return None, "session_expired"
        balance = data.get("walletBalance")
        if not isinstance(balance, str) or not balance.strip():
            return None, f"missing/invalid walletBalance in response: {balance!r}"
        return balance, None
    except requests.RequestException as e:
        return None, f"network: {e}"
    except Exception as e:
        return None, f"unexpected: {e}"


def wallet_is_safe_to_book(member_mobile, phpsessid):
    """The Section 6 safety guard. Must be called fresh, immediately before
    every bookingTransactions attempt — never cached, never skipped. Returns
    True only when the balance was read successfully AND is exactly
    "0.00". Any failure or ambiguity from get_wallet_balance(), or any
    balance value other than an exact "0.00" match, returns False —
    silence or uncertainty is never interpreted as safe to proceed.

    See WALLET_GUARD_SAFETY.md for the full test matrix.
    """
    balance, err = get_wallet_balance(member_mobile, phpsessid)
    if err:
        log(f"      ⚠ wallet balance check failed ({err}) — treating as unsafe, will not auto-book")
        return False
    if balance != SAFE_WALLET_BALANCE:
        log(f"      ⚠ wallet balance is {balance!r}, not exactly \"{SAFE_WALLET_BALANCE}\" — treating as unsafe, will not auto-book")
        return False
    return True


def find_open_slots(slots, target, loc_id):
    times = {t["time"] for t in target["times"]}
    return [
        s for s in slots
        if s.get("locId") == loc_id
        and s.get("timeName") in times
        and str(s.get("reservestatus")) != BOOKED_STATUS
    ]


def send_email(subject, body, smtp_user, smtp_pass, to_addr):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.set_content(body)
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def slot_key(date, loc_id, court, time_name):
    return f"{date}|{loc_id}|{court}|{time_name}"


def run_check_pass(state, config, phpsessid, email_user, email_pass, email_to):
    """Performs one full check. Mutates `state` in place."""
    today_str = datetime.now(TZ).date().isoformat()

    targets = config.get("targets", [])
    active = [t for t in targets if t["date"] >= today_str]
    if not active:
        log("  no active (future) targets — nothing to check")
        return

    fetch_keys = set()
    for t in active:
        for loc in t["locations"]:
            fetch_keys.add((t["date"], loc))

    cache = {}
    session_expired = False
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_slots, date, loc, phpsessid): (date, loc)
            for date, loc in sorted(fetch_keys)
        }
        for future in as_completed(futures):
            date, loc = futures[future]
            data, err = future.result()
            log(f"  → {LOCATIONS.get(loc, loc)} / {date}")
            if err == "session_expired":
                session_expired = True
                log("      ⚠ session expired")
            elif err:
                log(f"      ⚠ {err}")
            cache[(date, loc)] = data

    if session_expired:
        last = state.get("last_session_alert", 0)
        if (time.time() - last) > 6 * 3600:
            try:
                send_email(
                    "🎾 Tennis monitor: session expired",
                    "Your PHPSESSID has expired and the cloud monitor can't read "
                    "court availability anymore.\n\n"
                    "To fix:\n"
                    f"  1. Log in to {BOOKING_URL}\n"
                    "  2. DevTools → Application → Cookies → copy PHPSESSID\n"
                    "  3. Update the PHPSESSID secret in your GitHub repo\n"
                    "     (Settings → Secrets and variables → Actions → PHPSESSID → Update)\n"
                    "  4. Next scheduled run will use the new value.\n",
                    email_user, email_pass, email_to,
                )
                state["last_session_alert"] = time.time()
                log("✉ session-expired email sent")
            except Exception as e:
                log(f"⚠ session-expired email failed: {e}")
        return

    known_open = {
        k: v for k, v in state.get("known_open", {}).items()
        if k.split("|", 1)[0] >= today_str
    }

    new_alerts = []
    currently_open = set()

    for target in active:
        for loc in target["locations"]:
            slots = cache.get((target["date"], loc))
            if not slots:
                continue
            for s in find_open_slots(slots, target, loc):
                k = slot_key(target["date"], loc, s["stadiumName"], s["timeName"])
                currently_open.add(k)
                if k not in known_open:
                    known_open[k] = True
                    new_alerts.append({
                        "target": target["name"],
                        "date": target["date"],
                        "loc": LOCATIONS.get(loc, loc),
                        "court": s["stadiumName"],
                        "time": s["timeName"],
                        "price": s.get("stadiumtimePrice", ""),
                    })

    for k in list(known_open.keys()):
        d, l = k.split("|", 2)[:2]
        cached = cache.get((d, l))
        if (d, l) in fetch_keys and cached is not None and k not in currently_open:
            del known_open[k]

    state["known_open"] = known_open
    state["last_check"] = datetime.now(TZ).isoformat()
    state["currently_open_count"] = len(currently_open)

    log(f"  currently open across watchlist: {len(currently_open)}")

    if new_alerts:
        log(f"🎾 {len(new_alerts)} NEW opening(s) detected")
        lines = [f"🎾 {len(new_alerts)} tennis slot(s) just opened up:\n"]
        by_target = {}
        for a in new_alerts:
            by_target.setdefault(a["target"], []).append(a)
        for tname, items in by_target.items():
            lines.append(f"• {tname}")
            for a in items:
                price = a["price"].rstrip("0").rstrip(".") if a["price"] else "?"
                lines.append(
                    f"    {a['date']} {a['time']} — {a['loc']} / {a['court']} (฿{price})"
                )
            lines.append("")
        lines.append(f"Book here: {BOOKING_URL}")
        body = "\n".join(lines)
        try:
            send_email(
                f"🎾 {len(new_alerts)} tennis slot(s) open",
                body,
                email_user, email_pass, email_to,
            )
            log("✉ alert email sent")
        except Exception as e:
            log(f"⚠ alert email failed: {e}")
    else:
        log("  no new openings this iteration")


def main():
    phpsessid = os.environ.get("PHPSESSID", "").strip()
    email_user = os.environ.get("EMAIL_USER", "").strip()
    email_pass = os.environ.get("EMAIL_APP_PASSWORD", "").strip()

    missing = [k for k, v in {
        "PHPSESSID": phpsessid,
        "EMAIL_USER": email_user,
        "EMAIL_APP_PASSWORD": email_pass,
    }.items() if not v]
    if missing:
        log(f"ERROR: missing env vars: {', '.join(missing)}")
        sys.exit(1)

    config = load_config()
    email_to = config.get("email_to") or email_user

    iterations = int(os.environ.get("LOOP_ITERATIONS", "4"))
    interval = int(os.environ.get("LOOP_INTERVAL_SECONDS", "60"))
    log(f"Running {iterations} check pass(es), {interval}s apart")

    state = load_state()

    for i in range(iterations):
        log(f"--- iteration {i + 1}/{iterations} ---")
        try:
            run_check_pass(state, config, phpsessid, email_user, email_pass, email_to)
            save_state(state)
        except Exception as e:
            log(f"⚠ iteration {i + 1} failed: {e}")

        if i < iterations - 1:
            log(f"  sleeping {interval}s before next iteration...")
            time.sleep(interval)

    log("Done.")


if __name__ == "__main__":
    main()
