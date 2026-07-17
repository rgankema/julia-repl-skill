#!/usr/bin/env python3
"""
Background server for the persistent Julia REPL skill.

One server process owns one long-lived Julia process and answers commands over a
localhost TCP socket. The design goal is that a wedged evaluation can NEVER hang
the whole session:

  * Reads from Julia are select-based and honour a wall-clock deadline even when
    Julia goes completely silent (long precompile, infinite loop, waiting on stdin).
  * On timeout the server first sends SIGINT (Ctrl-C) to try to recover the REPL;
    if that doesn't bring the end marker back within a grace period it kills and
    restarts Julia so the session self-heals.
  * The per-session lock is acquired with a timeout, so one stuck evaluation
    reports "session busy" to later commands instead of silently blocking them.
  * `ping` reports whether the session is idle or busy (and for how long) without
    taking the lock, so liveness checks stay responsive.

Streaming protocol (execute): the server sends newline-delimited JSON messages,
one object per line:
    {"type": "output",    "data": "<text incl. trailing newline>"}
    {"type": "heartbeat", "elapsed": <seconds since eval start>[, "note": "..."]}
    {"type": "result",    "success": bool, "output": "...", "error": ..., ...}
The final "result" message always terminates a successful or failed execute.
"""

import json
import os
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

# Marker printed after Julia (and Revise) finish loading, used to resync the
# output stream before the first user command runs.
INIT_MARKER = "##JULIA_REPL_INIT##"

# How long, in seconds, to wait for the end marker after sending SIGINT before we
# give up and kill/restart Julia.
SIGINT_GRACE = 8.0

# How long to wait for the lock before telling the client the session is busy.
LOCK_ACQUIRE_TIMEOUT = 2.0

# Generous ceiling for the one-time Julia + Revise startup (cold precompile).
STARTUP_TIMEOUT = 300.0

# Default heartbeat cadence (seconds of silence between heartbeats).
DEFAULT_HEARTBEAT = 5.0


def send_json(conn, obj):
    """Send a single newline-delimited JSON message. No-op if conn is None."""
    if conn is None:
        return
    try:
        conn.sendall((json.dumps(obj) + "\n").encode())
    except OSError:
        # Client went away; nothing we can do but let the eval finish server-side.
        pass


class JuliaREPLServer:
    def __init__(self, port):
        self.port = port
        self.process = None
        self.lock = threading.Lock()
        self.running = True

        # Leftover bytes read from Julia's stdout that didn't end in a newline yet.
        # Kept across phases/commands so we never lose or re-split partial output.
        self._buf = b""

        # Busy-state tracking (read by the lock-free ping handler).
        self.busy = False
        self.busy_since = None
        self._eval_start = None

        # Tunables (overridable via env, mainly for tests).
        # JULIA_REPL_CMD lets tests substitute a controllable stub REPL for the
        # real `julia` binary; it is shlex-split into the process argv.
        self.julia_cmd = os.environ.get("JULIA_REPL_CMD")
        self.sigint_grace = float(os.environ.get("JULIA_REPL_SIGINT_GRACE", SIGINT_GRACE))
        self.lock_timeout = float(os.environ.get("JULIA_REPL_LOCK_TIMEOUT",
                                                 LOCK_ACQUIRE_TIMEOUT))

    # -- Julia process lifecycle ------------------------------------------------

    def start_julia(self, conn=None):
        """(Re)start the Julia process and resync the output stream."""
        if self.process:
            self.stop_julia()

        env = os.environ.copy()
        argv = (shlex.split(self.julia_cmd) if self.julia_cmd
                else ['julia', '--banner=no', '--color=no', '--startup-file=no'])
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,  # unbuffered binary pipes; we do our own line buffering
            env=env,
        )
        self._buf = b""

        # Load Revise, then print a marker so we can drain any startup/precompile
        # output and start the first user command against a clean stream.
        init = 'try; using Revise; catch; end\nprintln("%s")\n' % INIT_MARKER
        try:
            self.process.stdin.write(init.encode())
            self.process.stdin.flush()
        except OSError:
            return
        # Heartbeats keep a waiting client alive through a cold precompile.
        self._drain_to_marker(conn, INIT_MARKER, STARTUP_TIMEOUT,
                              DEFAULT_HEARTBEAT, None, stream=False)

    def stop_julia(self):
        """Best-effort graceful stop, escalating to kill."""
        if not self.process:
            return
        try:
            self.process.stdin.write(b"exit()\n")
            self.process.stdin.flush()
            self.process.wait(timeout=3)
        except Exception:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
                try:
                    self.process.wait(timeout=3)
                except Exception:
                    pass
        finally:
            # Close the pipes so we don't leak file descriptors across restarts.
            for stream in (self.process.stdin, self.process.stdout,
                           self.process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            self.process = None
            self._buf = b""

    def _julia_alive(self):
        return self.process is not None and self.process.poll() is None

    # -- Core read loop ---------------------------------------------------------

    def _drain_to_marker(self, conn, marker, timeout, heartbeat_interval,
                         log_handle, stream):
        """
        Read Julia stdout until `marker` appears on its own line, or the wall-clock
        `timeout` elapses. This is select-based, so the deadline is honoured even
        when Julia produces no output at all.

        Returns (status, lines):
          "ok"      -> marker seen; `lines` are the output lines before it
          "timeout" -> deadline hit; `lines` are whatever arrived so far
          "eof"     -> Julia's stdout closed (process died)
        Lines are streamed to `conn` as they arrive when `stream` is True, and a
        heartbeat is emitted whenever `heartbeat_interval` seconds pass with no
        output.
        """
        if not self._julia_alive():
            return ("eof", [])

        fd = self.process.stdout.fileno()
        deadline = time.time() + timeout
        lines = []

        while True:
            now = time.time()
            remaining = deadline - now
            if remaining <= 0:
                return ("timeout", lines)

            # First, flush any complete lines already sitting in the buffer.
            status = self._consume_buffer(conn, marker, lines, log_handle, stream)
            if status is not None:
                return (status, lines)

            wait = min(heartbeat_interval, remaining)
            try:
                rlist, _, _ = select.select([fd], [], [], wait)
            except (OSError, ValueError):
                return ("eof", lines)

            if not rlist:
                # Silence: reassure the client we're still alive.
                elapsed = int(now - self._eval_start) if self._eval_start else 0
                send_json(conn, {"type": "heartbeat", "elapsed": elapsed})
                continue

            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return ("eof", lines)
            if not chunk:
                return ("eof", lines)
            self._buf += chunk

            status = self._consume_buffer(conn, marker, lines, log_handle, stream)
            if status is not None:
                return (status, lines)

    def _consume_buffer(self, conn, marker, lines, log_handle, stream):
        """Split complete lines out of self._buf. Returns "ok" if marker seen, else None."""
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.decode(errors="replace").rstrip("\r")
            if line == marker:
                return "ok"
            lines.append(line)
            if stream:
                send_json(conn, {"type": "output", "data": line + "\n"})
            if log_handle:
                log_handle.write(line + "\n")
                log_handle.flush()
        return None

    def _recover_after_timeout(self, conn, marker, heartbeat_interval, log_handle):
        """
        Attempt to recover a timed-out evaluation. First SIGINT (like Ctrl-C in a
        real REPL); if the end marker comes back the REPL is healthy again and the
        session (and its state) is preserved. Otherwise kill and restart Julia so
        the session self-heals with a clean process.

        Returns dict: {lines, restarted, interrupted}.
        """
        send_json(conn, {"type": "heartbeat", "elapsed": 0,
                         "note": "timeout reached; sending interrupt (Ctrl-C)"})
        try:
            self.process.send_signal(signal.SIGINT)
        except Exception:
            pass

        status, lines = self._drain_to_marker(
            conn, marker, self.sigint_grace, heartbeat_interval, log_handle, stream=True)
        if status == "ok":
            return {"lines": lines, "restarted": False, "interrupted": True}

        # SIGINT didn't bring us back (tight native loop, consumed marker via
        # readline, or a crash) -> hard reset for a clean, usable session.
        send_json(conn, {"type": "heartbeat", "elapsed": 0,
                         "note": "interrupt did not recover; restarting Julia"})
        self.stop_julia()
        self.start_julia(conn)
        return {"lines": lines, "restarted": True, "interrupted": False}

    # -- Command handling -------------------------------------------------------

    def execute_code_stream(self, conn, request):
        """Execute Julia code, streaming output, with a hard deadline and self-heal."""
        acquired = self.lock.acquire(timeout=self.lock_timeout)
        if not acquired:
            busy_for = int(time.time() - self.busy_since) if self.busy_since else 0
            send_json(conn, {
                "type": "result",
                "success": False,
                "output": "",
                "error": (f"Session busy: another evaluation has been running for "
                          f"~{busy_for}s. If it appears stuck, reset it with --reset."),
                "session_busy": True,
            })
            return

        log_handle = None
        try:
            code = request["code"]
            timeout = float(request.get("timeout", 3600))
            heartbeat_interval = float(request.get("heartbeat", DEFAULT_HEARTBEAT))
            log_file = request.get("log_file")

            self.busy = True
            self.busy_since = time.time()
            self._eval_start = self.busy_since

            if not self._julia_alive():
                self.start_julia(conn)

            if log_file:
                log_handle = open(log_file, 'a')
                log_handle.write(f"\n=== Julia Command Started at "
                                 f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                log_handle.write(f"Executing: {code}\n")
                log_handle.flush()

            # Phase 1: Revise.revise() so code edits are picked up. Any Revise
            # errors/warnings are streamed to the user (they're actionable). This
            # is bounded by its own deadline so a Revise hang can't wedge us.
            revise_marker = "##REVISE_CHECK_%d##" % int(self._eval_start * 1e6)
            revise_code = ('if isdefined(Main, :Revise); try; Revise.revise(); '
                           'catch e; @warn "Revise.revise() failed" '
                           'exception=(e, catch_backtrace()); end; end\n'
                           'println("%s")\n' % revise_marker)
            try:
                self.process.stdin.write(revise_code.encode())
                self.process.stdin.flush()
            except OSError:
                pass
            rstatus, rlines = self._drain_to_marker(
                conn, revise_marker, 60.0, heartbeat_interval, log_handle, stream=True)
            revise_note = [l for l in rlines if l.strip()]

            if rstatus == "timeout":
                self._recover_after_timeout(conn, revise_marker, heartbeat_interval, log_handle)
                send_json(conn, {
                    "type": "result", "success": False,
                    "output": "\n".join(revise_note),
                    "error": "Timed out during Revise.revise(); session was reset.",
                    "timed_out": True, "session_restarted": True,
                })
                return
            if rstatus == "eof":
                self.start_julia(conn)
                send_json(conn, {
                    "type": "result", "success": False, "output": "\n".join(revise_note),
                    "error": "Julia exited during Revise check; session restarted.",
                    "session_restarted": True,
                })
                return

            # Phase 2: run the user's code, bounded by the real deadline.
            marker = "##END_MARKER_%d##" % int(time.time() * 1e6)
            full_code = code + '\nprintln("%s")\n' % marker
            try:
                self.process.stdin.write(full_code.encode())
                self.process.stdin.flush()
            except OSError:
                self.start_julia(conn)
                send_json(conn, {
                    "type": "result", "success": False, "output": "",
                    "error": "Julia process was not writable; session restarted.",
                    "session_restarted": True,
                })
                return

            status, lines = self._drain_to_marker(
                conn, marker, timeout, heartbeat_interval, log_handle, stream=True)

            if status == "ok":
                result = {"type": "result", "success": True,
                          "output": "\n".join(lines), "error": None}
            elif status == "eof":
                self.start_julia(conn)
                result = {"type": "result", "success": False,
                          "output": "\n".join(lines),
                          "error": "Julia process exited unexpectedly during "
                                   "evaluation. Session was restarted.",
                          "session_restarted": True}
            else:  # timeout
                recovery = self._recover_after_timeout(
                    conn, marker, heartbeat_interval, log_handle)
                lines += recovery["lines"]
                if recovery["interrupted"]:
                    detail = ("The REPL was recovered via interrupt (Ctrl-C); "
                              "session state is preserved.")
                else:
                    detail = ("The REPL did not respond to interrupt and Julia was "
                              "restarted; session state was lost.")
                result = {"type": "result", "success": False,
                          "output": "\n".join(lines),
                          "error": f"Evaluation timed out after {int(timeout)}s. {detail}",
                          "timed_out": True,
                          "recovered_via_interrupt": recovery["interrupted"],
                          "session_restarted": recovery["restarted"]}

            if log_handle:
                log_handle.write(f"=== Command Completed at "
                                 f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")

            send_json(conn, result)

        except Exception as e:
            send_json(conn, {"type": "result", "success": False, "output": "",
                             "error": "Server error: %s" % e})
        finally:
            if log_handle:
                try:
                    log_handle.close()
                except Exception:
                    pass
            self.busy = False
            self.busy_since = None
            self._eval_start = None
            self.lock.release()

    def handle_client(self, conn):
        """Handle one client connection."""
        try:
            data = conn.recv(65536).decode()
            request = json.loads(data)
            command = request.get("command")

            if command == "ping":
                busy_seconds = (int(time.time() - self.busy_since)
                                if (self.busy and self.busy_since) else 0)
                send_json(conn, {"status": "alive", "busy": self.busy,
                                 "busy_seconds": busy_seconds})
            elif command == "execute":
                self.execute_code_stream(conn, request)
            elif command == "reset":
                # Acquire the lock if we can so we don't fight an in-flight eval,
                # but reset even if we can't (that's the whole point of a reset).
                got = self.lock.acquire(timeout=self.lock_timeout)
                try:
                    self.stop_julia()
                finally:
                    if got:
                        self.lock.release()
                send_json(conn, {"status": "reset"})
            elif command == "shutdown":
                self.running = False
                send_json(conn, {"status": "shutting_down"})
            else:
                send_json(conn, {"error": "unknown_command"})
        except Exception as e:
            send_json(conn, {"error": str(e)})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('localhost', self.port))
        sock.listen(5)
        sock.settimeout(1.0)

        print(f"Julia REPL server listening on port {self.port}")

        while self.running:
            try:
                conn, _ = sock.accept()
                threading.Thread(target=self.handle_client, args=(conn,),
                                 daemon=True).start()
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Server error: {e}")
                break

        sock.close()
        self.stop_julia()


def signal_handler(signum, frame):
    print("Shutting down server...")
    sys.exit(0)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9998
    pid_file = sys.argv[2] if len(sys.argv) > 2 else f'/tmp/julia_repl_server_{port}.pid'

    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    JuliaREPLServer(port).run()
