"""
Tests for the Julia REPL tool's client/server wire protocol.

The framing tests use a fake socket, so they need no Julia and run in
milliseconds. They cover the bugs fixed in the sentinel-framing change and its
review follow-ups:

  * a stray JSON result leaking onto stdout,
  * large output being duplicated as an escaped JSON blob,
  * a Julia line that itself looks like JSON truncating the stream,
  * the RESULT_SENTINEL split across recv() boundaries,
  * unbounded buffering when echo=False,
  * multibyte UTF-8 characters split across TCP segments,
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
from contextlib import redirect_stdout

# Load the module by path (it lives in ../scripts and is not an installed package).
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_HERE, "..", "scripts", "julia_repl_tool.py")
_spec = importlib.util.spec_from_file_location("julia_repl_tool", _MODULE_PATH)
jrt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jrt)

SENTINEL = jrt.RESULT_SENTINEL


class FakeSocket:
    """Delivers a fixed byte stream in `chunk`-sized recv() calls.

    This mimics TCP: send() boundaries are not preserved, so the result JSON may
    arrive coalesced with output or split across several recv() calls.
    """

    def __init__(self, data: bytes, chunk: int):
        self.data = data
        self.chunk = chunk
        self.pos = 0

    def recv(self, _bufsize):
        end = min(self.pos + self.chunk, len(self.data))
        out = self.data[self.pos:end]
        self.pos = end
        return out


def _wire(output: str, result: dict) -> bytes:
    """Build the on-the-wire byte stream: output, then sentinel, then JSON."""
    return output.encode("utf-8") + SENTINEL.encode("utf-8") + \
        json.dumps(result, ensure_ascii=False).encode("utf-8")


def _collect(wire: bytes, chunk: int, echo: bool = True):
    """Run _stream_and_collect against a FakeSocket, capturing stdout."""
    sock = FakeSocket(wire, chunk)
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = jrt._stream_and_collect(sock, echo=echo)
    return result, buf.getvalue()


# recv chunk sizes exercised for every scenario: 1 byte (worst-case
# fragmentation) up to larger-than-the-whole-stream (full coalescing).
CHUNKS = (1, 2, 7, 64, 512, 1024, 1_000_000)


class StreamAndCollectTests(unittest.TestCase):
    def test_small_output_no_json_leak(self):
        """Stray-blob repro: last line + JSON coalesce; JSON must not print."""
        output = "Running tests...\nDone.\n"
        result = {"success": True, "output": output.rstrip(), "error": None}
        wire = _wire(output, result)
        for chunk in CHUNKS:
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)
                self.assertNotIn("{", printed)
                self.assertNotIn("success", printed)
                self.assertEqual(res, result)

    def test_large_output_printed_exactly_once(self):
        """>1 KB output whose result echoes it must not be duplicated."""
        output = "".join(f"line {i}: {'x' * 40}\n" for i in range(200))
        self.assertGreater(len(output), 1024)
        result = {"success": True, "output": output.rstrip("\n"), "error": None}
        wire = _wire(output, result)
        for chunk in (13, 512, 1024):
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)  # exactly once, no JSON blob
                self.assertEqual(res, result)

    def test_json_looking_output_line_not_truncated(self):
        """A Julia line that looks like JSON must not be taken as the terminator."""
        output = '{"k": 1}\nafter the json line\nfinal\n'
        result = {"success": True, "output": output.rstrip(), "error": None}
        wire = _wire(output, result)
        for chunk in CHUNKS:
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)
                self.assertEqual(res, result)

    def test_sentinel_split_across_recv_boundaries(self):
        """1-byte-at-a-time delivery splits the sentinel; framing must hold."""
        output = "x\n"
        result = {"success": True, "output": "x", "error": None}
        wire = _wire(output, result)
        res, printed = _collect(wire, chunk=1)
        self.assertEqual(printed, output)
        self.assertEqual(res, result)

    def test_echo_false_suppresses_output_but_returns_result(self):
        output = "should not be printed\n"
        result = {"success": True, "output": output.rstrip(), "error": None}
        wire = _wire(output, result)
        res, printed = _collect(wire, chunk=8, echo=False)
        self.assertEqual(printed, "")
        self.assertEqual(res, result)

    def test_echo_false_large_output_is_correct(self):
        """echo=False must still parse correctly with far-larger-than-buffer output.

        This exercises the trim-regardless-of-echo path that prevents unbounded
        buffering; correctness here guarantees the trim did not drop data.
        """
        output = "y" * 100_000  # no newlines: a single long run
        result = {"success": True, "output": output, "error": None}
        wire = _wire(output, result)
        res, printed = _collect(wire, chunk=4096, echo=False)
        self.assertEqual(printed, "")
        self.assertEqual(res["output"], output)

    def test_multibyte_utf8_split_across_segments(self):
        """Multibyte UTF-8 chars split across recv() must not raise or corrupt."""
        output = "café ☕ 世界 🚀\n"
        result = {"success": True, "output": output.rstrip(), "error": None}
        wire = _wire(output, result)
        for chunk in (1, 2, 3, 7):
            with self.subTest(chunk=chunk):
                res, printed = _collect(wire, chunk)
                self.assertEqual(printed, output)
                self.assertEqual(res["output"], output.rstrip())

    def test_connection_closed_before_sentinel(self):
        """Closing mid-output returns None and flushes what was received."""
        wire = b"partial output, no sentinel"
        res, printed = _collect(wire, chunk=5)
        self.assertIsNone(res)
        self.assertEqual(printed, "partial output, no sentinel")

    def test_connection_closed_before_json_completes(self):
        """Sentinel seen but JSON truncated -> None."""
        wire = "out\n".encode() + SENTINEL.encode() + b'{"success": tr'
        res, printed = _collect(wire, chunk=4)
        self.assertIsNone(res)
        self.assertEqual(printed, "out\n")

    def test_error_status_available(self):
        """Programmatic callers still get the failing status + message."""
        output = "some streamed output\n"
        result = {"success": False, "output": "", "error": "boom"}
        wire = _wire(output, result)
        res, printed = _collect(wire, chunk=8)
        self.assertEqual(printed, output)
        self.assertEqual(res, result)


@unittest.skipUnless(shutil.which("julia"), "julia binary not on PATH")
class EndToEndTests(unittest.TestCase):
    """Drive a real server + Julia process on a dedicated port.

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
            for suffix in (".pid", ".py"):
                path = f"/tmp/claude/julia-repl-skill/{cls.PORT}{suffix}"
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _run(self, code):
        """Send code to the live server and capture streamed stdout + result."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("localhost", self.PORT))
        sock.send(json.dumps(
            {"command": "execute", "code": code, "timeout": 120}).encode())
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = jrt._stream_and_collect(sock, echo=True)
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
