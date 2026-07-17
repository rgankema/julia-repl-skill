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
import codecs
from pathlib import Path
from typing import Dict, Any, Optional


# Unique sentinel emitted by the server on its own, immediately before the final
# JSON result. It lets the client separate streamed program output from the
# result unambiguously, instead of guessing by brace-matching (which breaks
# because TCP does not preserve send() boundaries and Julia can print braces).
# This literal MUST stay in sync with the copy embedded in the server script
# inside JuliaREPLClient.create_server_script().
RESULT_SENTINEL = "##JULIA_REPL_RESULT_JSON##"


def _stream_and_collect(sock, echo: bool = True) -> Optional[Dict[str, Any]]:
    """Read streamed output followed by the sentinel-delimited JSON result.

    Everything received before RESULT_SENTINEL is streamed program output; when
    ``echo`` is True it is printed to stdout as it arrives. Everything after the
    sentinel is accumulated until it parses as JSON, which is returned. Returns
    None if the connection closes before a complete result is received.
    """
    # Incremental decoder so a multibyte UTF-8 character split across TCP
    # segments is buffered until complete instead of raising UnicodeDecodeError.
    decoder = codecs.getincrementaldecoder('utf-8')()
    buffer = ""
    holdback = len(RESULT_SENTINEL) - 1

    def recv_text():
        """Return (decoded_text, closed). closed is True at end of stream."""
        raw = sock.recv(4096)
        if not raw:
            return decoder.decode(b'', final=True), True
        return decoder.decode(raw), False

    # Phase 1: stream output until the sentinel appears.
    while True:
        idx = buffer.find(RESULT_SENTINEL)
        if idx != -1:
            if echo and idx > 0:
                print(buffer[:idx], end='', flush=True)
            buffer = buffer[idx + len(RESULT_SENTINEL):]
            break

        # No complete sentinel yet. Trim everything except a possible partial
        # sentinel at the tail (holdback chars) so the buffer can't grow
        # unbounded; print the trimmed prefix only when echoing.
        if len(buffer) > holdback:
            safe = len(buffer) - holdback
            if echo:
                print(buffer[:safe], end='', flush=True)
            buffer = buffer[safe:]

        text, closed = recv_text()
        if closed:
            # Connection closed without a sentinel; flush whatever remains.
            if echo and buffer:
                print(buffer, end='', flush=True)
            return None
        buffer += text

    # Phase 2: accumulate the JSON result until it parses.
    while True:
        try:
            return json.loads(buffer.strip())
        except json.JSONDecodeError:
            text, closed = recv_text()
            if closed:
                return None
            buffer += text


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

    def is_session_alive(self, session_info: Dict[str, Any]) -> bool:
        """Check if a session is still alive by checking its PID and server."""
        port = session_info.get('port')
        pid = session_info.get('pid')

        # Check if server is responsive
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(('localhost', port))
            request = {"command": "ping"}
            sock.send(json.dumps(request).encode())
            response = sock.recv(1024).decode()
            result = json.loads(response)
            sock.close()
            return result.get("status") == "alive"
        except:
            return False

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


class JuliaREPLClient:
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

        # Make PID and server script files port-specific
        sessions_dir = Path('/tmp/claude/julia-repl-skill')
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file = str(sessions_dir / f'{self.port}.pid')
        self.server_script = str(sessions_dir / f'{self.port}.py')
    
    def create_server_script(self):
        """Create the background server script."""
        server_code = '''#!/usr/bin/env python3
import socket
import subprocess
import threading
import time
import json
import sys
import signal
import os

class JuliaREPLServer:
    def __init__(self, port):
        self.port = port
        self.process = None
        self.lock = threading.Lock()
        self.running = True
        
    def start_julia(self):
        """Start Julia REPL process."""
        if self.process:
            self.stop_julia()
            
        self.process = subprocess.Popen(
            ['julia', '--banner=no', '--color=no', '--startup-file=no'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=0,
            env=os.environ.copy()
        )
        time.sleep(1)  # Wait for Julia to start
        
        # Initialize with Revise
        try:
            self.process.stdin.write("using Revise\\n")
            self.process.stdin.flush()
            time.sleep(2)  # Give Revise time to load
        except:
            pass  # Continue even if Revise fails to load
        
    def stop_julia(self):
        """Stop Julia REPL process."""
        if self.process:
            try:
                self.process.stdin.write("exit()\\n")
                self.process.stdin.flush()
                self.process.wait(timeout=5)
            except:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except:
                    self.process.kill()
            finally:
                self.process = None
                
    def execute_code_stream(self, conn, request):
        """Execute Julia code and stream output line by line to client."""
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self.start_julia()

            # Sentinel emitted right before the JSON result so the client can
            # separate streamed output from the result. MUST match
            # RESULT_SENTINEL in the client module.
            result_sentinel = "##JULIA_REPL_RESULT_JSON##"
            log_handle = None
            try:
                code = request["code"]
                timeout = request.get("timeout", 3600)
                log_file = request.get("log_file")

                # Open log file if specified
                if log_file:
                    log_handle = open(log_file, 'a')
                    log_handle.write(f"\\n=== Julia Command Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\\n")
                    log_handle.flush()
                
                # First run Revise.revise() then check Revise.errors() before executing user code
                revise_marker = f"##REVISE_CHECK_{int(time.time() * 1000000)}##"
                revise_check = f'Revise.revise()\\nRevise.errors()\\nprintln("{revise_marker}")\\n'
                
                self.process.stdin.write(revise_check)
                self.process.stdin.flush()
                
                # Collect Revise check output
                revise_output = []
                start_time = time.time()
                
                while time.time() - start_time < 10:  # 10 second timeout for Revise check
                    line = self.process.stdout.readline()
                    if not line:
                        break
                        
                    line = line.rstrip()
                    if line == revise_marker:
                        break
                        
                    revise_output.append(line)
                
                # Now execute the user's code
                marker = f"##END_MARKER_{int(time.time() * 1000000)}##"
                full_code = f'{code}\\nprintln("{marker}")\\n'
                
                self.process.stdin.write(full_code)
                self.process.stdin.flush()
                
                if log_handle:
                    log_handle.write(f"Executing: {code}\\n")
                    log_handle.flush()
                
                output_lines = []
                start_time = time.time()
                
                while time.time() - start_time < timeout:
                    line = self.process.stdout.readline()
                    if not line:
                        break
                        
                    line = line.rstrip()
                    if line == marker:
                        break
                        
                    output_lines.append(line)
                    
                    # Send line immediately to client for streaming.
                    # sendall so a long line is transmitted in full.
                    line_data = line + "\\n"
                    conn.sendall(line_data.encode())
                    
                    if log_handle:
                        log_handle.write(line + "\\n")
                        log_handle.flush()
                
                if log_handle:
                    log_handle.write(f"=== Command Completed at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\\n\\n")
                    log_handle.close()
                
                # Send final JSON result, preceded by the sentinel line so the
                # client can unambiguously find the result boundary.
                result = {
                    "success": True,
                    "output": "\\n".join(output_lines),
                    "error": None
                }

                # sendall so partial sends can't corrupt the result framing.
                conn.sendall(result_sentinel.encode())
                conn.sendall(json.dumps(result).encode())

            except Exception as e:
                if log_handle:
                    log_handle.write(f"ERROR: {str(e)}\\n")
                    log_handle.close()
                error_result = {
                    "success": False,
                    "output": "",
                    "error": str(e)
                }
                conn.sendall(result_sentinel.encode())
                conn.sendall(json.dumps(error_result).encode())

    def handle_client(self, conn):
        """Handle client connection."""
        try:
            data = conn.recv(4096).decode()
            request = json.loads(data)

            if request["command"] == "ping":
                response = {"status": "alive"}
                conn.send(json.dumps(response).encode())
            elif request["command"] == "execute":
                # Always stream output - connection NOT closed in finally
                self.execute_code_stream(conn, request)
                conn.close()  # Close here after streaming is done
                return
            elif request["command"] == "reset":
                self.stop_julia()
                response = {"status": "reset"}
                conn.send(json.dumps(response).encode())
            elif request["command"] == "shutdown":
                self.running = False
                response = {"status": "shutting_down"}
                conn.send(json.dumps(response).encode())
            else:
                response = {"error": "unknown_command"}
                conn.send(json.dumps(response).encode())

        except Exception as e:
            error_response = {"error": str(e)}
            conn.send(json.dumps(error_response).encode())
        finally:
            conn.close()
    
    def run(self):
        """Run the server."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('localhost', self.port))
        sock.listen(5)
        sock.settimeout(1.0)  # Non-blocking accept
        
        print(f"Julia REPL server listening on port {self.port}")
        
        while self.running:
            try:
                conn, addr = sock.accept()
                threading.Thread(target=self.handle_client, args=(conn,)).start()
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

    # Write PID file
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    server = JuliaREPLServer(port)
    server.run()
'''
        
        with open(self.server_script, 'w') as f:
            f.write(server_code)
        os.chmod(self.server_script, 0o755)
    
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
        self.create_server_script()

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

            # All execute commands now stream by default
            if command == "execute":
                # Print output in real-time, then collect the JSON result that
                # follows the sentinel.
                result = _stream_and_collect(sock, echo=True)
                sock.close()
                if result is None:
                    return {
                        "success": False,
                        "output": "",
                        "error": "Connection closed before result was received",
                    }
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
        print("-" * 90)
        for session_id, info in sessions.items():
            status = "alive" if registry.is_session_alive(info) else "stale"
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

        request = {"command": "execute", "code": code, "timeout": timeout, "log_file": log_file}
        sock.send(json.dumps(request).encode())

        # Stream output in real-time and collect the final JSON result, which is
        # delimited by RESULT_SENTINEL so it never leaks into stdout.
        result = _stream_and_collect(sock, echo=True)
        sock.close()

        # Surface a failing status (output has already been streamed above).
        if result is None:
            # Connection closed before the sentinel/JSON result arrived.
            print("Error: connection closed before result was received",
                  file=sys.stderr)
            sys.exit(1)
        if not result.get("success", True):
            error = result.get("error")
            if error:
                print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()