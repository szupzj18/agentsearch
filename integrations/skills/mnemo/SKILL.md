---
name: mnemo
description: Search past conversations across all local coding agents (Claude Code, Codex, Pi) and registered remote devboxes. Use when the user wants to recall, find, or check what was previously discussed, decided, tried, or written in any agent session on any device — including sessions that ran in a different agent or on a devbox rather than this machine. 跨 agent、跨设备搜索历史会话：当用户想找以前（在本机或 devbox 上）在 claude / codex / pi（任意一家）里讨论过的内容、做过的决定、试过的方案或写过的代码时使用。
---

# mnemo

Full-text search over the session histories of Claude Code, Codex, and Pi, across this machine and registered remote devboxes.
All agents on every device are indexed in one view; the current agent can read the other agents' and other machines' sessions.

## When to use

- "上次/之前是怎么做 X 的"、"找找以前讨论过 X 没有"、"codex/pi 那边有没有搞过 X"、"devbox 上的会话里有没有 X"
- Recall a prior decision, error, command, or piece of code without knowing which agent, device, or directory it lived in
- Resume context from a session in another agent or on a devbox

Do not use for the current conversation (that is already in context).

## How to search

Run the CLI with `--json` and parse the result:

```bash
mnemo search "关键词" --json
```

- Multiple keywords are AND-ed; English matches word prefixes, Chinese matches substrings (bigrams), so `订阅` matches `订阅支出` and `refact` matches `refactoring`.
- By default every reachable device is searched in parallel (local + devboxes), and results are merged by rank.
- Filters (all optional):
  - `--source claude,codex,pi` — restrict to agents
  - `--host local,devbox-109` — restrict to devices
  - `--cwd <substring>` — restrict to working directory, e.g. `--cwd toutiao-dev`
  - `--since YYYY-MM-DD`
  - `--limit N` (default 20)
  - `--kind text,summary,tool_call,tool_result,reasoning` (default excludes reasoning; pass `--all-kinds` to include it)

Each hit contains: `host`, `source`, `cwd`, `ts`, `role`, `kind`, `snippet`, `path`, `lineno`.
A warning on stderr lists devices that were unreachable; results from the others are still complete.

## Reading a hit in context

Snippets are short. Before quoting details or reusing code from a hit, fetch the surrounding messages. Pass back the hit's `host` so a remote hit is read on the device that holds it:

```bash
mnemo context <path> <lineno> --host <hit-host> --json [--before 4] [--after 8]
```

Returns the N indexed messages before/after the hit (normalized role/kind/text) read straight from the index. Read the relevant hits rather than dumping whole sessions into context.

To read the entire session a hit belongs to (the hit's `path` is one JSONL session file), ordered by time:

```bash
mnemo session <path> --host <hit-host> --json [--head N] [--tail N]
```

MCP/Pi expose the same as `get_session` / `get_full_session`. A session can be long; prefer `context` for one detail and use `--head`/`--tail` to skim a long session before pulling all of it.

Indexed message bodies are capped at 20k characters each (long tool outputs are clipped with a `…[truncated]` marker). To read the original full content straight from the session JSONL, add `--raw` to `context`/`session` (MCP/Pi: `raw: true`). Raw reads execute on the device that holds the file, so remote content still does not leave it except in the query response.

## Keeping the index fresh

The local index updates incrementally on MCP server / Pi tool startup; remote devices sync automatically right before each search (sub-second when idle). If the CLI reports nothing or the user just had a conversation that should be searchable:

```bash
mnemo index      # local incremental, usually under a second when idle
mnemo status     # counts per source, last sync, registered remotes
```

Manage devices (run from this machine; installs code over SSH and builds the remote index):

```bash
mnemo remote add <name> [ssh-host]   # ssh-host defaults to name
mnemo remote list
mnemo remote remove <name>
mnemo remote update [<name>]         # re-sync code and re-index
mnemo dashboard                      # local web panel: connectivity, per-device index stats, search diagnostics
```

Search results, snippets, and context may contain sensitive content from private sessions — summarize for the user rather than forwarding raw content elsewhere.
