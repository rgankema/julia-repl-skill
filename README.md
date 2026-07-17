# julia-repl skill for Claude Code

A Claude Code skill that runs Julia code in a **persistent REPL session**, preserving compilation state and variables between executions. Iterative test runs go from minutes to seconds.

## Prerequisites

- [Claude Code](https://claude.ai/code) installed
- Python 3
- Julia with `Revise.jl` installed (`] add Revise` in the Julia REPL)

## Installation

```bash
git clone https://github.com/rgankema/julia-repl-skill ~/.claude/skills/julia-repl
```

That's it. Claude Code auto-discovers skills under `~/.claude/skills/`.

## Configuration

To make Claude always use the skill instead of raw `julia` commands, add this to `~/.claude/CLAUDE.md`:

```markdown
## Julia Execution

**CRITICAL: Never use direct `julia` commands. Always use the julia-repl skill.**
```

## Usage

Once installed, invoke the skill in any Claude Code session:

```
/julia-repl
```

Or just ask Claude to run Julia code — if you've added the CLAUDE.md instruction above, it will use the persistent REPL automatically.

See [SKILL.md](SKILL.md) for full documentation on session management, testing workflows, and advanced options.

## Development

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```

The fast tests drive the server against a controllable stub REPL (no Julia needed)
and cover the timeout → interrupt → kill/restart self-healing, crash recovery, the
busy fast-fail, and the streaming protocol. The end-to-end integration test runs the
real CLI against `julia` and is skipped automatically when Julia isn't on `PATH`.
