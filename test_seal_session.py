#!/usr/bin/env python3
"""Tests for seal_session.py (the Mac -> GitHub Actions handoff). Run with:
python3 -m unittest test_seal_session -v
Uses a throwaway temp dir and a fake clipboard; never the real jar."""
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

import requests
from cryptography.fernet import Fernet

import probe_new_site as probe
import seal_session

COOKIES = {"cs.did": "DID_value_for_seal_1111", "cs.member": "MEMBER_value_for_seal_2222"}


class TestSeal(unittest.TestCase):
    def setUp(self):
        probe._secrets.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.jar = self.dir / ".bot_session.json"
        self.enc = self.dir / "bot_session.enc"
        s = requests.Session()
        for name, value in COOKIES.items():
            s.cookies.set_cookie(probe.create_cookie(name, value, domain=probe.HOST, path="/", secure=True))
        probe.save_jar(self.jar, s)
        self.clipboard = []

    def tearDown(self):
        self._tmp.cleanup()

    def seal(self, clipboard=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = seal_session.seal(self.jar, self.enc, clipboard or self.clipboard.append)
        return code, out.getvalue()

    def test_seals_hands_off_and_prints_only_fingerprints(self):
        plaintext = self.jar.read_bytes()
        code, out = self.seal()
        self.assertEqual(code, 0)
        [key] = self.clipboard
        s = requests.Session()
        probe.load_jar(self.enc, s, Fernet(key))
        self.assertEqual({c.name: c.value for c in s.cookies}, COOKIES)
        self.assertFalse(self.jar.exists())
        [handed] = probe.handed_off_files(self.jar)
        self.assertEqual(handed.read_bytes(), plaintext)
        for secret in list(COOKIES.values()) + [key.decode()]:
            self.assertNotIn(secret, out)
        self.assertIn(probe.fingerprint(key.decode()), out)
        self.assertEqual(sorted(p.name for p in self.dir.glob(".bot_session.*.tmp")), [])

    def test_refuses_when_enc_already_exists(self):
        self.enc.write_bytes(b"live actions session")
        code, out = self.seal()
        self.assertEqual(code, 1)
        self.assertEqual(self.enc.read_bytes(), b"live actions session")
        self.assertTrue(self.jar.exists())
        self.assertEqual(self.clipboard, [])

    def test_clipboard_failure_undoes_everything(self):
        def broken(_):
            raise subprocess.CalledProcessError(1, ["pbcopy"])
        code, out = self.seal(clipboard=broken)
        self.assertEqual(code, 1)
        self.assertFalse(self.enc.exists())
        self.assertTrue(self.jar.exists())
        self.assertEqual(probe.handed_off_files(self.jar), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
