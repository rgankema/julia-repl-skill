---
name: julia-repl
description: Run Julia code in a persistent Julia session, maintaining compilation state and variables between executions.
---

# Julia REPL

A persistent Julia REPL that maintains compilation state and variables between executions. Sessions are automatically managed per-project directory.

## Usage

**Always use this tool instead of direct `julia` commands for any Julia code execution.**

```bash
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "your_julia_code_here"
```

## Why Use This Tool?

- **Persistent state**: Variables and loaded packages persist between commands
- **Cached compilation**: Dramatically faster subsequent runs (seconds vs minutes)
- **Auto Revise**: Automatically reloads code changes with Revise.jl
- **Multi-project support**: Each directory gets its own isolated session

## Key Features

### Automatic Session Management
Sessions are automatically created based on your current directory - no manual setup required!

```bash
cd /workspace/branches/feature-A
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "using MyPackage"  # Creates session for feature-A

# Later commands automatically reuse the same session (fast!):
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "test_function()"

# Different branch gets its own session:
cd /workspace/branches/feature-B
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "using MyPackage"  # Separate isolated session
```

### Testing Workflow

**CRITICAL**: Always use julia_repl_tool for running tests, not direct `julia` commands.

Why? Tests often fail and require iteration (fix → re-run → repeat). The persistent session maintains compilation cache between runs, making subsequent test runs dramatically faster.

```bash
# ✅ CORRECT: Fast iterative testing
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "using ReTestItems; runtests(\"test/Component\"; tags=[:unit, :ring1])"

# After code changes, re-run (much faster due to cached compilation):
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "using ReTestItems; runtests(\"test/Component\"; tags=[:unit])"

# ❌ INCORRECT: Direct julia call (slow compilation every time)
julia --project=. -e 'using ReTestItems; runtests("test/path")'
```

## Session Operations

### Reset Session
Clear all variables and packages (useful after major code changes):
```bash
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "# any code" --reset
```

### Shutdown Session
When done with a project:
```bash
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "" --shutdown
```

### List Active Sessions
```bash
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py --list
```

### Clean Up Stale Sessions
```bash
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py --cleanup
```

## Advanced Usage

### Explicit Session Control
For advanced use cases:

```bash
# Use a named session (useful for experiments):
JULIA_SESSION=experiment python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "risky_code()"

# Use the global session (for quick one-off commands):
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "println(42)" --global

# Explicit session name:
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "code" --session=my-experiment
```

## Revise Error Handling

Revise shows errors when it can't reload code changes. Some are fixable without resetting:

### Examples of Fixable Errors (No Reset Needed)
```
┌ Error: Failed to revise ...
│   exception = ParseError:
```
**Fix**: Correct the syntax error in your code. Revise will automatically reload once fixed.

### Examples That Require Session Reset
```
┌ Error: Failed to revise ...
│   exception = invalid redefinition of constant ...
```
**Fix**: Reset the session, then re-run:
```bash
python3 $CLAUDE_SKILL_DIR/scripts/julia_repl_tool.py "" --reset
```

**Note**: These are common examples. Other errors may also require a reset - use your judgment. If fixing the code doesn't resolve the Revise error, try resetting the session.

**Tip**: Revise may suggest `Revise.retry()` for evaluation order issues - try that before a full reset.

## Timeouts and Self-Healing

Commands are bounded by a wall-clock timeout (`--timeout=N`, default 3600s) that is
enforced **even if Julia goes completely silent** (long precompile, infinite loop,
crash, or code waiting on stdin such as `readline()` or a debugger prompt).

When a command exceeds its timeout the session recovers automatically — you do **not**
need to run `--reset` manually:

1. The tool sends an interrupt (Ctrl-C) to Julia. If the REPL recovers, your session
   **state is preserved** and you get a timeout error result.
2. If the interrupt doesn't help (e.g. a tight `while true end` loop or a consumed
   stdin), Julia is killed and restarted. The session is usable again immediately, but
   **variables/compilation state are lost** (like a fresh REPL).

While a command is running, the tool prints a progress line to stderr
(`[julia-repl] running… Ns`) so a long-but-alive precompile isn't mistaken for a hang.
A timed-out or failed command exits non-zero and prints the reason to stderr.

If you send a command while another is still running in the same session, it fails fast
with a "Session busy" message instead of blocking. Use `--reset` to recover a session
that is genuinely stuck.

## Available Options

- `--session=NAME`: Use explicit session ID
- `--global`: Use global session (for one-off commands)
- `--reset`: Reset the Julia session before execution (recovers a stuck session)
- `--shutdown`: Shutdown the session
- `--timeout=N`: Wall-clock timeout in seconds (default: 3600). Honored even when Julia
  is silent; on expiry the session interrupts, then restarts if needed, and self-heals.
- `--log=FILE`: Log output to file
- `--list`: List all active sessions (shows `idle`, `busy (Ns)`, or `stale`)
- `--cleanup`: Clean up stale sessions

## Best Practices

1. **Always use julia_repl_tool** for Julia code execution to maintain compilation benefits
2. **Especially for tests** - iterative development benefits enormously from cached compilation
3. **Let automatic session management work** - sessions are created per-directory automatically
4. **Reset session** if Revise errors persist after fixing code
5. **Shutdown session** when completely done with a project to free resources

## How It Works

The tool:
1. Automatically detects your current directory
2. Creates/reuses a persistent Julia REPL session for that directory
3. Runs `Revise.revise()` before each execution to pick up code changes
4. Maintains all compilation state and variables between calls
5. Returns all printed output from Julia

This makes iterative development dramatically faster - especially for testing workflows.