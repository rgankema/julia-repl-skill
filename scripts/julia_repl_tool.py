"""
Persistent Julia REPL tool for Claude.
Maintains a Julia session across multiple code executions using a background server.
"""

import json
import subprocess
import time
import sys
import os
import socket
import signal
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional


class SessionRegistry:
    """Manages persistent Julia REPL sessions and their port assignments."""

    def __init__(self, sessions_dir: str = None):
        if sessions_dir is None:
            self.sessions_dir = Path('/tmp/claude/julia-repl-skill')
        else:
            self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.sessions_dir / 'registry.json'

    def load_registry(self) -> Dict[str, Dict[str, Any]]:
        """Load the session registry from disk."""
        if not self.registry_file.exists():
            return {}
        try:
            with open(self.registry_file, 'r') as f:
                return json.load(f)
        except:
            return {}

    def save_registry(self, registry: Dict[str, Dict[str, Any]]):
        """Save the session registry to disk."""
        with open(self.registry_file, 'w') as f:
            json.dump(registry, f, indent=2)

    def is_port_available(self, port: int) -> bool:
        """Check if a port is available for use."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.bind(('localhost', port))
            sock.close()
            return True
        except:
            return False

    def ping_session(self, session_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Ping a session's server. Returns the ping response dict, or None if
        the server is unreachable. The response includes busy/busy_seconds so
        callers can distinguish an idle session from one running (or stuck on) a
        long evaluation."""
        port = session_info.get('port')
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(('localhost', port))
            sock.send(json.dumps({"command": "ping"}).encode())
            response = sock.recv(1024).decode()
            sock.close()
            result = json.loads(response)
            if result.get("status") == "alive":
                return result
            return None
        except:
            return None

    def is_session_alive(self, session_info: Dict[str, Any]) -> bool:
        """Check if a session's server is responsive."""
        return self.ping_session(session_info) is not None

    def allocate_port(self, registry: Dict[str, Dict[str, Any]]) -> int:
        """Allocate an available port in the range 10000-20000."""
        used_ports = {info['port'] for info in registry.values()}

        # Try random ports in range
        import random
        for _ in range(100):
            port = random.randint(10000, 19999)
            if port not in used_ports and self.is_port_available(port):
                return port

        # Fallback: sequential search
        for port in range(10000, 20000):
            if port not in used_ports and self.is_port_available(port):
                return port

        raise Exception("No available ports found in range 10000-19999")

    def get_or_create_session(self, session_id: str, cwd: str = None) -> Dict[str, Any]:
        """Get an existing session or create a new one."""
        registry = self.load_registry()

        # Clean up stale sessions while we're here
        self._cleanup_stale_sessions(registry)

        # Check if session exists and is alive
        if session_id in registry:
            session_info = registry[session_id]
            if self.is_session_alive(session_info):
                # Update last_used timestamp
                session_info['last_used'] = time.time()
                self.save_registry(registry)
                return session_info
            else:
                # Session is stale, remove it
                del registry[session_id]

        # Create new session
        port = self.allocate_port(registry)
        session_info = {
            'port': port,
            'pid': None,  # Will be set when server starts
            'cwd': cwd or os.getcwd(),
            'created': time.time(),
            'last_used': time.time(),
            'session_id': session_id
        }

        registry[session_id] = session_info
        self.save_registry(registry)
        return session_info

    def update_session_pid(self, session_id: str, pid: int):
        """Update the PID for a session."""
        registry = self.load_registry()
        if session_id in registry:
            registry[session_id]['pid'] = pid
            registry[session_id]['last_used'] = time.time()
            self.save_registry(registry)

    def remove_session(self, session_id: str):
        """Remove a session from the registry."""
        registry = self.load_registry()
        if session_id in registry:
            del registry[session_id]
            self.save_registry(registry)

    def list_sessions(self) -> Dict[str, Dict[str, Any]]:
        """List all sessions."""
        registry = self.load_registry()
        self._cleanup_stale_sessions(registry)
        return registry

    def _cleanup_stale_sessions(self, registry: Dict[str, Dict[str, Any]]):
        """Remove stale sessions from registry."""
        to_remove = []
        for session_id, session_info in registry.items():
            if not self.is_session_alive(session_info):
                to_remove.append(session_id)

        for session_id in to_remove:
            del registry[session_id]

        if to_remove:
            self.save_registry(registry)

    def cleanup_all(self):
        """Clean up all stale sessions."""
        registry = self.load_registry()
        self._cleanup_stale_sessions(registry)
        return len(registry)


def detect_session_id() -> str:
    """
    Detect the session ID automatically based on context.

    Priority order:
    1. JULIA_SESSION environment variable (explicit override)
    2. Hash of current working directory (automatic per-project sessions)
    3. "global" (fallback for one-off commands)
    """
    # Check for explicit session ID
    explicit_session = os.environ.get('JULIA_SESSION')
    if explicit_session:
        return explicit_session

    # Use current working directory hash
    cwd = os.getcwd()
    home = str(Path.home())

    # Don't create sessions for home directory (use global)
    if cwd == home:
        return "global"

    # Create a short hash of the directory path
    dir_hash = hashlib.md5(cwd.encode()).hexdigest()[:8]
    # Use last part of path for readability
    dir_name = Path(cwd).name
    return f"{dir_name}_{dir_hash}"


def read_execute_stream(sock: socket.socket, heartbeat_interval: float = 5,
                        echo: bool = True, idle_limit: float = None) -> Dict[str, Any]:
    """
    Read an execute response as newline-delimited JSON from `sock`.

    Message types: "output" (streamed text), "heartbeat" (progress during silence),
    and "result" (terminal). Output is echoed to stdout in real time; heartbeats
    update a single progress line on stderr. A socket timeout guards against a
    dead server: if nothing at all (not even a heartbeat) arrives for `idle_limit`
    seconds, we stop waiting and return an error instead of blocking forever.
    """
    if idle_limit is None:
        idle_limit = max(heartbeat_interval * 4, 20)
    sock.settimeout(min(heartbeat_interval + 5, idle_limit))

    buffer = b""
    result = None
    last_data = time.time()
    progress_shown = False

    def clear_progress():
        nonlocal progress_shown
        if progress_shown:
            print("", file=sys.stderr, flush=True)
            progress_shown = False

    while result is None:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            idle = time.time() - last_data
            if idle >= idle_limit:
                clear_progress()
                msg = (f"no output from server for {int(idle)}s; giving up "
                       f"(session may be stuck — try --reset or --shutdown).")
                print(f"[julia-repl] {msg}", file=sys.stderr, flush=True)
                return {"success": False, "output": "",
                        "error": f"Client gave up: {msg}"}
            print(f"\r[julia-repl] waiting… {int(idle)}s with no server response",
                  end="", file=sys.stderr, flush=True)
            progress_shown = True
            continue

        if not chunk:
            break
        last_data = time.time()
        buffer += chunk

        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw.decode(errors="replace"))
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            if mtype == "output":
                clear_progress()
                if echo:
                    print(msg.get("data", ""), end="", flush=True)
            elif mtype == "heartbeat":
                elapsed = msg.get("elapsed", 0)
                note = msg.get("note")
                label = f" — {note}" if note else ""
                print(f"\r[julia-repl] running… {elapsed}s{label}          ",
                      end="", file=sys.stderr, flush=True)
                progress_shown = True
            elif mtype == "result":
                clear_progress()
                result = msg
                break

    if result is None:
        result = {"success": False, "output": "",
                  "error": "Connection closed before a result was received."}
    return result


class JuliaREPLClient:
    # Default cadence (seconds) between server heartbeats during silent periods.
    HEARTBEAT_INTERVAL = 5

    def __init__(self, port: Optional[int] = None, session_id: Optional[str] = None, auto_detect: bool = True):
        """
        Initialize Julia REPL client.

        Args:
            port: Explicit port number (for backwards compatibility)
            session_id: Explicit session ID (takes precedence over auto-detection)
            auto_detect: Whether to auto-detect session (default True)
        """
        self.registry = SessionRegistry()
        self.session_id = None

        # Mode 1: Explicit port (backwards compatibility)
        if port is not None:
            self.port = port
            self.session_id = None
        # Mode 2: Session-based (new auto-detection)
        elif auto_detect:
            # Detect or use provided session ID
            self.session_id = session_id or detect_session_id()
            session_info = self.registry.get_or_create_session(
                self.session_id,
                cwd=os.getcwd()
            )
            self.port = session_info['port']
        # Mode 3: Default port
        else:
            self.port = 9998
            self.session_id = None

        # PID file is port-specific; the server script is shipped alongside this
        # file (see scripts/julia_repl_server.py) rather than generated per-port.
        sessions_dir = Path('/tmp/claude/julia-repl-skill')
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file = str(sessions_dir / f'{self.port}.pid')
        self.server_script = str(Path(__file__).resolve().parent / 'julia_repl_server.py')

    def is_server_running(self) -> bool:
        """Check if server is running by pinging it."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(('localhost', self.port))
            
            request = {"command": "ping"}
            sock.send(json.dumps(request).encode())
            
            response = sock.recv(1024).decode()
            result = json.loads(response)
            sock.close()
            
            return result.get("status") == "alive"
        except:
            return False
    
    def start_server(self):
        """Start the background server."""
        # Start server in background, passing port and PID file path
        process = subprocess.Popen(
            [sys.executable, self.server_script, str(self.port), self.pid_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        # Update registry with PID if we're in session mode
        if self.session_id:
            # Wait for PID file to be written
            time.sleep(1)
            try:
                with open(self.pid_file, 'r') as f:
                    pid = int(f.read().strip())
                self.registry.update_session_pid(self.session_id, pid)
            except:
                pass  # PID update is optional

        # Wait a bit for server to start
        time.sleep(2)

        # Verify it started
        if not self.is_server_running():
            raise Exception("Failed to start Julia REPL server")
    
    def send_command(self, command: str, **kwargs) -> Dict[str, Any]:
        """Send command to server."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(('localhost', self.port))

            request = {"command": command, **kwargs}
            sock.send(json.dumps(request).encode())

            # Execute streams newline-delimited JSON messages; use the shared
            # reader so a dead/stuck server can't hang us (it sets a socket timeout).
            if command == "execute":
                heartbeat = kwargs.get("heartbeat", self.HEARTBEAT_INTERVAL)
                result = read_execute_stream(sock, heartbeat_interval=heartbeat)
                sock.close()
                return result
            else:
                # Non-execute commands get simple response
                response = sock.recv(8192).decode()
                result = json.loads(response)
                sock.close()
                return result
        except Exception as e:
            return {"error": f"Communication error: {str(e)}"}
    
    def execute_code(self, code: str, reset_session: bool = False, timeout: int = 3600, shutdown_server: bool = False, log_file: str = None) -> Dict[str, Any]:
        """Execute Julia code via the server. Output is always streamed."""
        # Shutdown server if requested
        if shutdown_server:
            if self.is_server_running():
                self.send_command("shutdown")
                time.sleep(1)  # Give time for shutdown
            return {"success": True, "output": "Server shutdown completed", "error": None}

        # Ensure server is running
        if not self.is_server_running():
            self.start_server()

        # Reset session if requested
        if reset_session:
            self.send_command("reset")

        # Execute code (always streams by default)
        return self.send_command("execute", code=code, timeout=timeout, log_file=log_file)


def julia_repl_tool(code: str, port: int = 9998, reset_session: bool = False, timeout: int = 3600, shutdown_server: bool = False, log_file: str = None, stream_output: bool = False) -> Dict[str, Any]:
    """
    Execute Julia code in a persistent REPL session.

    Args:
        code: Julia code to execute
        port: Port for the Julia REPL server
        reset_session: Whether to reset the session before execution
        timeout: Timeout in seconds for code execution
        shutdown_server: Whether to shutdown the background server
        log_file: Optional path to log file for real-time output
        stream_output: DEPRECATED - streaming is now always enabled

    Returns:
        Dictionary with execution results
    """
    if stream_output:
        print("Warning: --stream flag is deprecated and now the default behavior", file=sys.stderr)

    client = JuliaREPLClient(port)
    return client.execute_code(code, reset_session, timeout, shutdown_server, log_file)


def main():
    """Command-line interface for the Julia REPL tool."""
    # Handle special commands first
    if '--list' in sys.argv:
        registry = SessionRegistry()
        sessions = registry.list_sessions()
        if not sessions:
            print("No active Julia REPL sessions")
            sys.exit(0)

        print("Active Julia REPL sessions:")
        print(f"{'Session ID':<30} {'Port':<8} {'Directory':<40} {'Status'}")
        print("-" * 95)
        for session_id, info in sessions.items():
            ping = registry.ping_session(info)
            if ping is None:
                status = "stale"
            elif ping.get("busy"):
                status = f"busy ({ping.get('busy_seconds', 0)}s)"
            else:
                status = "idle"
            cwd_short = info.get('cwd', 'unknown')
            if len(cwd_short) > 40:
                cwd_short = "..." + cwd_short[-37:]
            print(f"{session_id:<30} {info['port']:<8} {cwd_short:<40} {status}")
        sys.exit(0)

    if '--cleanup' in sys.argv:
        registry = SessionRegistry()
        remaining = registry.cleanup_all()
        print(f"Cleaned up stale sessions. {remaining} active session(s) remaining.")
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1].startswith('--'):
        print("Usage: julia_repl_tool '<julia_code>' [options]")
        print("\nOptions:")
        print("  --session=NAME        Use explicit session ID")
        print("  --port=N              Use explicit port (disables auto-detection)")
        print("  --global              Use global session (for one-off commands)")
        print("  --reset               Reset Julia session before execution")
        print("  --shutdown            Shutdown the session")
        print("  --timeout=N           Timeout in seconds (default: 3600)")
        print("  --log=FILE            Log output to file")
        print("\nSession management:")
        print("  --list                List all active sessions")
        print("  --cleanup             Clean up stale sessions")
        print("\nBy default, sessions are created per-directory automatically.")
        print("Set JULIA_SESSION environment variable to override session detection.")
        sys.exit(1)

    code = sys.argv[1]
    reset_session = '--reset' in sys.argv
    shutdown_server = '--shutdown' in sys.argv
    stream_output = '--stream' in sys.argv
    use_global = '--global' in sys.argv
    timeout = 3600
    port = None
    session_id = None
    log_file = None

    for arg in sys.argv:
        if arg.startswith('--timeout='):
            timeout = int(arg.split('=')[1])
        elif arg.startswith('--log='):
            log_file = arg.split('=')[1]
        elif arg.startswith('--port='):
            port = int(arg.split('=')[1])
        elif arg.startswith('--session='):
            session_id = arg.split('=', 1)[1]

    # Print deprecation warning if --stream is used
    if stream_output:
        print("Warning: --stream flag is deprecated and now the default behavior", file=sys.stderr)

    # Create client with auto-detection or explicit settings
    if port is not None:
        # Explicit port mode (backwards compatibility)
        client = JuliaREPLClient(port=port, auto_detect=False)
    elif use_global:
        # Global session mode
        client = JuliaREPLClient(session_id="global")
    elif session_id:
        # Explicit session ID
        client = JuliaREPLClient(session_id=session_id)
    else:
        # Auto-detect (default)
        client = JuliaREPLClient()

    # Ensure server is running
    if not client.is_server_running():
        client.start_server()

    # Reset session if requested
    if reset_session:
        client.send_command("reset")

    # Handle shutdown if requested
    if shutdown_server:
        if client.is_server_running():
            client.send_command("shutdown")
            time.sleep(1)
            # Remove from registry if in session mode
            if client.session_id:
                client.registry.remove_session(client.session_id)
        print("Server shutdown completed")
        sys.exit(0)

    # Connect and stream output
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(('localhost', client.port))

        request = {"command": "execute", "code": code, "timeout": timeout,
                   "log_file": log_file, "heartbeat": client.HEARTBEAT_INTERVAL}
        sock.send(json.dumps(request).encode())

        result = read_execute_stream(sock, heartbeat_interval=client.HEARTBEAT_INTERVAL)
        sock.close()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Surface a clear error and a non-zero exit code on timeout/failure so callers
    # can tell a wedged/failed run from a clean one.
    if not result.get("success", False):
        err = result.get("error")
        if err:
            print(f"\n[julia-repl] {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()