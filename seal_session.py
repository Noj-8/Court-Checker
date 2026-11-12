#!/usr/bin/env python3
"""
One-time handoff of the bot's Crystal Sports session from this Mac to
GitHub Actions. Run it once, after a good Mac probe run:

    python3 seal_session.py

  1. Generates a NEW Fernet key and puts it on the clipboard (pbcopy). It is
     never printed. Paste it into the GitHub secret BOT_JAR_KEY.
  2. Encrypts .bot_session.json -> bot_session.enc (atomic write, ciphertext
     only) and checks that it decrypts back to exactly the same cookies.
  3. Renames .bot_session.json -> .bot_session.json.handed-off-<timestamp>
     (gitignored), so the Mac probe refuses to run on the handed-off session.

Prints only fingerprints (length + first 8 hex chars of SHA-256). Refuses
to touch anything if bot_session.enc already exists. Needs the cryptography
package (pip3 install cryptography).
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests
from cryptography.fernet import Fernet

import probe_new_site as probe


def pbcopy(data):
    subprocess.run(["pbcopy"], input=data, check=True)


def seal(jar_path=probe.JAR_FILE, enc_path=probe.ENC_FILE, clipboard=pbcopy):
    jar_path, enc_path = Path(jar_path), Path(enc_path)
    say = probe.say

    if enc_path.exists():
        say(f"ERROR: {enc_path.name} already exists — a session may already be handed off to Actions.")
        say("Nothing changed. To replace it on purpose, delete it yourself first (git rm), then rerun.")
        return 1
    if not jar_path.exists():
        handed = probe.handed_off_files(jar_path)
        say(f"ERROR: no {jar_path.name} to seal"
            + (f" — already handed off ({handed[-1].name})." if handed else "."))
        return 1

    session = requests.Session()
    try:
        probe.load_jar(jar_path, session)
    except probe.JarError as e:
        say(f"ERROR: couldn't read {jar_path.name} ({e}). Nothing changed.")
        return 1
    values, dupes = probe.cookie_values(session)
    missing = [n for n in probe.SEED_ENV if n not in probe.OPTIONAL_SEED and n not in values]
    if missing or dupes:
        say(f"ERROR: {jar_path.name} isn't a usable session (missing: {', '.join(missing) or 'none'}; "
            f"duplicated: {', '.join(sorted(dupes)) or 'none'}). Nothing changed.")
        return 1

    key = Fernet.generate_key()
    probe.remember_secret(key.decode("ascii"))
    fernet = Fernet(key)
    try:
        probe.save_jar(enc_path, session, fernet)
        check = requests.Session()
        probe.load_jar(enc_path, check, fernet)
        if probe.jar_records(check) != probe.jar_records(session):
            raise probe.JarError("decrypted copy doesn't match the plaintext jar")
        clipboard(key)
    except (OSError, probe.JarError, subprocess.CalledProcessError) as e:
        enc_path.unlink(missing_ok=True)  # useless without the key we failed to hand over
        say(f"ERROR: {type(e).__name__}: {e}. Removed {enc_path.name}; {jar_path.name} left as it was.")
        return 1

    handed = jar_path.with_name(
        f"{jar_path.name}.handed-off-{datetime.now(probe.TZ).strftime('%Y%m%d-%H%M%S')}")
    os.replace(jar_path, handed)

    say(f"Sealed {len(values)} cookie(s) into {enc_path.name} (values never printed):")
    for name in sorted(values):
        say(f"  {name} {probe.fingerprint(values[name])}")
    say(f"  {probe.JAR_KEY_ENV} {probe.fingerprint(key.decode('ascii'))} — on the clipboard, not shown.")
    say("  The Actions log prints the same fingerprint, so you can check the secret was pasted intact.")
    say(f"  {enc_path.name}: {enc_path.stat().st_size} bytes of ciphertext.")
    say(f"Renamed {jar_path.name} -> {handed.name}; the Mac probe now refuses to run on it.")
    return 0


if __name__ == "__main__":
    sys.exit(seal())
