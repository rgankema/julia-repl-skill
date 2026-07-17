#!/usr/bin/env python3
"""
Tests for the persistent Julia REPL skill, focused on the hang/self-heal fix.

Two layers:

  * Fast, deterministic unit tests (ServerLogicTests, ProtocolTests) that need no
    Julia. They drive the server against a controllable stub REPL (fake_julia.py)
    via the JULIA_REPL_CMD seam, covering the timeout -> interrupt -> kill/restart
    machinery, crash recovery, the busy fast-fail, the busy ping, and the
    newline-delimited streaming protocol.

  * Opt-in integration tests (IntegrationTests) that drive the real CLI against a
    real `julia`. Skipped automatically when `julia` is not on PATH.

Run:  python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
FAKE_JULIA = HERE / "fake_julia.py"
TOOL = SCRIPTS / "julia_repl_tool.py"

sys.path.insert(0, str(SCRIPTS))
import julia_repl_server as jrs  # noqa: E402
from julia_repl_tool import read_execute_stream  # noqa: E402


class FakeConn:
    """Stand-in for a client socket that just records everything the server sends."""

    def __init__(self):
        self.buf = bytearray()

    def sendall(self, data):
        self.buf.extend(data)

    def messages(self):
        out = []
        for line in bytes(self.buf).split(b"\n"):
            if line.strip():
                out.append(json.loads(line.decode()))
        return out

    def result(self):
        for m in reversed(self.messages()):
            if m.get("type") == "result":
                return m
        return None


class ServerLogicTests(unittest.TestCase):
    """Exercise the recovery machinery against the controllable stub REPL."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("JULIA_REPL_CMD", "JULIA_REPL_SIGINT_GRACE",
                        "JULIA_REPL_LOCK_TIMEOUT")}
        os.environ["JULIA_REPL_CMD"] = f"{sys.executable} {FAKE_JULIA}"
        os.environ["JULIA_REPL_SIGINT_GRACE"] = "0.6"
        os.environ["JULIA_REPL_LOCK_TIMEOUT"] = "0.5"
        self.server = jrs.JuliaREPLServer(0)

    def tearDown(self):
        try:
            self.server.stop_julia()
        except Exception:
            pass
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def run_code(self, code, timeout=5, heartbeat=0.2):
        conn = FakeConn()
        self.server.execute_code_stream(
            conn, {"code": code, "timeout": timeout, "heartbeat": heartbeat})
        return conn.result(), conn

    def test_normal_output(self):
        res, _ = self.run_code('println("hello-world")')
        self.assertTrue(res["success"], res)
        self.assertIn("hello-world", res["output"])

    def test_sigint_recovery_preserves_process(self):
        self.run_code('println("warmup")')
        pid_before = self.server.process.pid
        res, _ = self.run_code("__HANG_UNTIL_SIGINT__", timeout=0.5)
        self.assertTrue(res["timed_out"])
        self.assertTrue(res["recovered_via_interrupt"])
        self.assertFalse(res["session_restarted"])
        # Same process survived the interrupt (state would be preserved).
        self.assertEqual(self.server.process.pid, pid_before)

    def test_kill_restart_on_uninterruptible_hang(self):
        self.run_code('println("warmup")')
        pid_before = self.server.process.pid
        res, _ = self.run_code("__HANG_IGNORE_SIGINT__", timeout=0.5)
        self.assertTrue(res["timed_out"])
        self.assertFalse(res["recovered_via_interrupt"])
        self.assertTrue(res["session_restarted"])
        # A fresh process replaced the wedged one.
        self.assertNotEqual(self.server.process.pid, pid_before)

    def test_next_command_works_after_kill(self):
        self.run_code("__HANG_IGNORE_SIGINT__", timeout=0.5)
        res, _ = self.run_code('println("healed")')
        self.assertTrue(res["success"], res)
        self.assertIn("healed", res["output"])

    def test_crash_restarts_session(self):
        self.run_code('println("warmup")')
        res, _ = self.run_code("__CRASH__")
        self.assertFalse(res["success"])
        self.assertTrue(res.get("session_restarted"))
        # And the session is usable again immediately.
        res2, _ = self.run_code('println("after-crash")')
        self.assertTrue(res2["success"], res2)
        self.assertIn("after-crash", res2["output"])

    def test_busy_command_fast_fails(self):
        # Hold the lock as if an evaluation were in flight.
        self.server.busy = True
        self.server.busy_since = time.time()
        self.assertTrue(self.server.lock.acquire())
        try:
            conn = FakeConn()
            t0 = time.time()
            self.server.execute_code_stream(
                conn, {"code": 'println("x")', "timeout": 5})
            dt = time.time() - t0
            res = conn.result()
            self.assertTrue(res.get("session_busy"))
            self.assertFalse(res["success"])
            # Should fail fast (roughly within the lock timeout), not block.
            self.assertLess(dt, self.server.lock_timeout + 1.0)
        finally:
            self.server.lock.release()
            self.server.busy = False
            self.server.busy_since = None

    def test_ping_reports_busy_without_lock(self):
        a, b = socket.socketpair()
        try:
            self.server.busy = True
            self.server.busy_since = time.time() - 3
            b.sendall(json.dumps({"command": "ping"}).encode())
            # handle_client reads the request from `a` and replies on `a`.
            self.server.handle_client(a)
            resp = json.loads(b.recv(4096).decode())
            self.assertEqual(resp["status"], "alive")
            self.assertTrue(resp["busy"])
            self.assertGreaterEqual(resp["busy_seconds"], 3)
        finally:
            b.close()
            self.server.busy = False
            self.server.busy_since = None


class RequestFramingTests(unittest.TestCase):
    """The server must reassemble a request that spans multiple packets / exceeds
    a single recv (e.g. an `execute` carrying a large `code` payload)."""

    def test_large_request_reassembled_across_packets(self):
        a, b = socket.socketpair()
        big_code = "x = " + "1 + " * 50_000 + "1"  # comfortably exceeds 64 KiB
        req = json.dumps(
            {"command": "execute", "code": big_code, "timeout": 5}).encode()
        self.assertGreater(len(req), 65536)

        def writer():
            for i in range(0, len(req), 4096):
                b.sendall(req[i:i + 4096])

        t = threading.Thread(target=writer)
        t.start()
        try:
            got = jrs.JuliaREPLServer._recv_request(a, timeout=10)
        finally:
            t.join()
            a.close()
            b.close()
        self.assertEqual(got["command"], "execute")
        self.assertEqual(got["code"], big_code)


class ClientTimeoutTests(unittest.TestCase):
    """The client reader's socket-timeout / give-up behavior, which needs a real
    socket (the deterministic framing matrix lives in test_julia_repl_tool.py)."""

    def setUp(self):
        self.client, self.server = socket.socketpair()

    def tearDown(self):
        for s in (self.client, self.server):
            try:
                s.close()
            except Exception:
                pass

    def test_gives_up_on_total_silence(self):
        # No data at all (server open but silent): the reader must return an
        # error, not block forever.
        t0 = time.time()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            res = read_execute_stream(self.client, heartbeat_interval=0.3,
                                      idle_limit=0.8)
        dt = time.time() - t0
        self.assertFalse(res["success"])
        self.assertIn("gave up", res["error"].lower())
        self.assertLess(dt, 3.0)

    def test_heartbeats_prevent_giveup(self):
        # A slow-but-alive server that only sends heartbeats must NOT be given
        # up on; once the result arrives the reader returns it.
        def writer():
            for i in range(3):
                self.server.sendall(
                    (json.dumps({"type": "heartbeat", "elapsed": i}) + "\n").encode())
                time.sleep(0.3)
            self.server.sendall(
                (json.dumps({"type": "result", "success": True,
                             "output": "ok", "error": None}) + "\n").encode())

        t = threading.Thread(target=writer)
        t.start()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            res = read_execute_stream(self.client, heartbeat_interval=0.2,
                                      idle_limit=0.5)
        t.join()
        self.assertTrue(res["success"])
        self.assertEqual(res["output"], "ok")


@unittest.skipUnless(shutil.which("julia"), "julia not on PATH")
class IntegrationTests(unittest.TestCase):
    """End-to-end against a real Julia. Slow; skipped when Julia is absent."""

    SESSION = f"jrepl_pytest_{os.getpid()}"

    def _cli(self, code, *extra, timeout=180):
        env = dict(os.environ)
        env["JULIA_SESSION"] = self.SESSION
        env.setdefault("JULIA_NUM_THREADS", "8,2")
        return subprocess.run(
            [sys.executable, str(TOOL), code, *extra],
            capture_output=True, text=True, timeout=timeout, env=env)

    @classmethod
    def tearDownClass(cls):
        env = dict(os.environ)
        env["JULIA_SESSION"] = cls.SESSION
        # Bound the shutdown: this class exercises hang scenarios, so a regressed
        # shutdown must not hang the whole test run.
        try:
            subprocess.run([sys.executable, str(TOOL), "", "--shutdown"],
                           capture_output=True, text=True, env=env, timeout=60)
        except subprocess.TimeoutExpired:
            pass

    def test_end_to_end_hang_and_self_heal(self):
        # 1. Basic execution.
        r = self._cli("println(6 * 7)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("42", r.stdout)

        # 2. An infinite loop must time out (not hang the CLI) and exit non-zero.
        r = self._cli("while true end", "--timeout=3")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("timed out", (r.stdout + r.stderr).lower())

        # 3. The very next command must work with no manual --reset (self-healed).
        r = self._cli('println("healed-", 5 * 5)')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("healed-25", r.stdout)


if __name__ == "__main__":
    unittest.main()
