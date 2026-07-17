"""
Tests for the Julia REPL tool's client/server wire protocol.

The framing tests use a fake socket, so they need no Julia and run in
milliseconds. The protocol is newline-delimited JSON: the server sends one
`{"type": "output", "data": ...}` message per output line, optional
`{"type": "heartbeat", ...}` messages during silence, and a terminal
`{"type": "result", ...}` message. Because every message is a self-describing
JSON object on its own line, program output can never be mistaken for the
result — which is the class of stdout-corruption bug these tests guard against:

  * a stray JSON result leaking onto stdout,
  * large output being duplicated as an escaped JSON blob,
  * a Julia line that itself looks like JSON truncating the stream,
  * a message split across recv() boundaries,
  * heartbeats interleaved with output,
  * multibyte UTF-8 content surviving arbitrary chunking,
  * a connection closing before the result, and
  * the error status still being available to programmatic callers.

An end-to-end test drives a real server + Julia process and is skipped
automatically when the `julia` binary is not on PATH.

Run: python3 -m unittest discover -s tests
"""

import io
import json
import os
import shutil
import socket
import sys
import time
import unittest
import importlib.util
from contextlib import redirect_stdout, redirect_stderr

# Load the module by path (it lives in ../scripts and is not an installed package).
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_HERE, "..", "scripts", "julia_repl_tool.py")
_spec = importlib.util.spec_from_file_location("julia_repl_tool", _MODULE_PATH)
jrt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jrt)


class FakeSocket:
    """Delivers a fixed byte stream in `chunk`-sized recv() calls.

    This mimics TCP: send() boundaries are not preserved, so a single JSON
    message may arrive coalesced with others or split across several recv()
    calls. settimeout() is a no-op (the stream is never silent — it either has
    bytes or is closed).
    """

    def __init__(self, data: bytes, chunk: int):
        self.data = data
        self.chunk = chunk
        self.pos = 0

    def settimeout(self, _t):
        pass

    def recv(self, _bufsize):
        end = min(self.pos + self.chunk, len(self.data))
        out = self.data[self.pos:end]
        self.pos = end
        return out


def _msg(obj) -> bytes:
    """Encode one newline-delimited JSON message exactly as the server does."""
    return (json.dumps(obj) + "\n").encode("utf-8")


def _wire(output: str, result: dict, heartbeats=()) -> bytes:
    """Build the on-the-wire byte stream: optional heartbeats, one output
    message per line, then the terminal result message."""
    parts = [_msg({"type": "heartbeat", "elapsed": e}) for e in heartbeats]
    for line in output.splitlines(keepends=True):
        parts.append(_msg({"type": "output", "data": line}))
    parts.append(_msg({"type": "result", **result}))
    return b"".join(parts)


def _collect(wire: bytes, chunk: int, echo: bool = True):
    """Run read_execute_stream against a FakeSocket, capturing stdout."""
    sock = FakeSocket(wire, chunk)
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        result = jrt.read_execute_stream(sock, heartbeat_interval=1, echo=echo)
    return result, buf.getvalue()


def _assert_result(test, res, expected):
    """The result carries an extra "type" key; compare the payload fields."""
    test.assertIsNotNone(res)
    for k, v in expected.items():
        test.assertEqual(res.get(k), v, f"field {k!r}")


# recv chunk sizes exercised for every scenario: 1 byte (worst-case
# fragmentation) up to larger-than-the-whole-stream (full coalescing).
CHUNKS = (1, 2, 7, 64, 512, 1024, 1_000_000)


class WireProtocolTests(unittest.TestCase):
    def test_small_output_no_json_leak(self):
        """The result JSON must never print; only program output reaches stdout."""
        output = "Running tests...\nDone.\n"
        result = {"success": True, "output": output.rstrip(), "error": None}
        wire = _wire(output, result)
        for chunk in CHUNKS:
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)
                self.assertNotIn("success", printed)
                _assert_result(self, res, result)

    def test_large_output_printed_exactly_once(self):
        """>1 KB output must be streamed once, never duplicated as a JSON blob."""
        output = "".join(f"line {i}: {'x' * 40}\n" for i in range(200))
        self.assertGreater(len(output), 1024)
        result = {"success": True, "output": output.rstrip("\n"), "error": None}
        wire = _wire(output, result)
        for chunk in (13, 512, 1024):
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)
                self.assertEqual(printed.count("line 1:"), 1)
                _assert_result(self, res, result)

    def test_json_looking_output_line_not_truncated(self):
        """A Julia line that looks like JSON must not be taken as the terminator."""
        output = '{"k": 1}\nafter the json line\nfinal\n'
        result = {"success": True, "output": output.rstrip(), "error": None}
        wire = _wire(output, result)
        for chunk in CHUNKS:
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)
                _assert_result(self, res, result)

    def test_message_split_across_recv_boundaries(self):
        """1-byte-at-a-time delivery splits every message; framing must hold."""
        output = "x\n"
        result = {"success": True, "output": "x", "error": None}
        res, printed = _collect(_wire(output, result), chunk=1)
        self.assertEqual(printed, output)
        _assert_result(self, res, result)

    def test_heartbeats_interleaved_do_not_reach_stdout(self):
        """Heartbeats keep the client alive but must not corrupt output."""
        output = "working\ndone\n"
        result = {"success": True, "output": output.rstrip(), "error": None}
        wire = _wire(output, result, heartbeats=(1, 2, 3))
        for chunk in (1, 7, 512):
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)
                self.assertNotIn("heartbeat", printed)
                _assert_result(self, res, result)

    def test_echo_false_suppresses_output_but_returns_result(self):
        output = "should not be printed\n"
        result = {"success": True, "output": output.rstrip(), "error": None}
        res, printed = _collect(_wire(output, result), chunk=8, echo=False)
        self.assertEqual(printed, "")
        _assert_result(self, res, result)

    def test_large_single_message_is_correct(self):
        """A single output line far larger than a recv chunk must round-trip."""
        output = "y" * 100_000  # one long run, no interior newline
        result = {"success": True, "output": output, "error": None}
        wire = _wire(output + "\n", result)
        res, printed = _collect(wire, chunk=4096)
        self.assertEqual(printed, output + "\n")
        _assert_result(self, res, result)

    def test_multibyte_utf8_survives_chunking(self):
        """Multibyte UTF-8 content must not raise or corrupt at any chunk size."""
        output = "café ☕ 世界 🚀\n"
        result = {"success": True, "output": output.rstrip(), "error": None}
        wire = _wire(output, result)
        for chunk in (1, 2, 3, 7):
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)
                _assert_result(self, res, result)

    def test_connection_closed_before_result(self):
        """Closing after some output but before the result -> failure result."""
        wire = _msg({"type": "output", "data": "partial output\n"})
        res, printed = _collect(wire, chunk=5)
        self.assertEqual(printed, "partial output\n")
        self.assertFalse(res["success"])
        self.assertIn("closed", res["error"].lower())

    def test_connection_closed_before_result_completes(self):
        """A truncated result message -> failure result, output still streamed."""
        wire = _msg({"type": "output", "data": "out\n"}) + b'{"type": "result", "succ'
        res, printed = _collect(wire, chunk=4)
        self.assertEqual(printed, "out\n")
        self.assertFalse(res["success"])

    def test_error_status_available(self):
        """Programmatic callers still get the failing status + message."""
        output = "some streamed output\n"
        result = {"success": False, "output": "", "error": "boom"}
        res, printed = _collect(_wire(output, result), chunk=8)
        self.assertEqual(printed, output)
        _assert_result(self, res, result)


@unittest.skipUnless(shutil.which("julia"), "julia binary not on PATH")
class EndToEndFramingTests(unittest.TestCase):
    """Drive a real server + Julia process and confirm output framing holds.

    Slow (Julia startup + Revise) and skipped automatically without Julia.
    """

    PORT = 18293  # dedicated port; will not collide with auto-detected sessions

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("JULIA_NUM_THREADS", "8,2")
        cls.client = jrt.JuliaREPLClient(port=cls.PORT, auto_detect=False)
        if not cls.client.is_server_running():
            cls.client.start_server()

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.client.is_server_running():
                cls.client.send_command("shutdown")
                time.sleep(1)
        finally:
            try:
                os.remove(f"/tmp/claude/julia-repl-skill/{cls.PORT}.pid")
            except OSError:
                pass

    def _run(self, code):
        """Send code to the live server and capture streamed stdout + result."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("localhost", self.PORT))
        sock.send(json.dumps(
            {"command": "execute", "code": code, "timeout": 120}).encode())
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            result = jrt.read_execute_stream(sock, echo=True)
        sock.close()
        return result, buf.getvalue()

    def test_small_output_no_trailing_json(self):
        res, printed = self._run('println("Running..."); println("Done.")')
        self.assertIn("Done.", printed)
        self.assertNotIn("success", printed)
        self.assertTrue(res["success"])

    def test_large_output_not_duplicated(self):
        res, printed = self._run(
            'for i in 1:60; println("line $i: ", repeat("x", 30)); end')
        self.assertGreater(len(printed), 1024)
        self.assertEqual(printed.count("line 1:"), 1)
        self.assertNotIn('"success"', printed)
        self.assertTrue(res["success"])

    def test_json_looking_line_not_truncated(self):
        res, printed = self._run(
            'println("{\\"k\\": 1}"); println("after json"); println("final")')
        self.assertIn("after json", printed)
        self.assertIn("final", printed)
        self.assertTrue(res["success"])


if __name__ == "__main__":
    unittest.main()
