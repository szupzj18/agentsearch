# agentsearch

Full-text search over the local conversation histories of multiple coding agents — Claude Code, Codex, and Pi — from one place. A session that ran in Codex or on a devbox is searchable from inside Claude, and vice versa.

Zero dependencies: Python 3.7+ and SQLite FTS5 only.

## Features

- One SQLite FTS5 index over all three agents' JSONL session logs; incremental sync by file mtime/size.
- English prefix matching and Chinese substring matching (unigram + bigram column), BM25 ranking.
- Normalized message schema (text / summary / reasoning / tool_call / tool_result) with noise filtered.
- CLI, an MCP server (Claude Code, Codex), and a native Pi extension.
- Multi-device federated search: queries fan out over SSH to remote devboxes in parallel and merge by reciprocal rank fusion; `context` is routed to the device that holds the hit. No session content is copied off the remote device.
- Local-only web dashboard for connectivity checks, per-device index stats, and search latency diagnostics.

## Install

```bash
git clone https://github.com/szupzj18/agentsearch.git ~/agentsearch
ln -s ~/agentsearch/bin/agentsearch ~/.local/bin/agentsearch
agentsearch index
```

The launcher resolves the package through its own symlink, so a symlink on `PATH` is enough.

### MCP server (Claude Code / Codex)

Register the stdio command `agentsearch mcp` with your MCP client. Example for Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.agentsearch]
command = "/Users/you/.local/bin/agentsearch"
args = ["mcp"]
startup_timeout_sec = 120
```

### Pi extension

Symlink the extension into Pi's extensions directory:

```bash
ln -s ~/agentsearch/integrations/pi/agentsearch.ts ~/.pi/agent/extensions/agentsearch.ts
```

### Skill (optional)

`integrations/skills/agentsearch/SKILL.md` can be symlinked into any agent's skills directory.

## Usage

```bash
agentsearch search "关键词" [--source claude,codex,pi] [--cwd SUBSTR] [--since YYYY-MM-DD] [--limit N] [--json]
agentsearch context <path> <lineno> [--before 4] [--after 8] [--host HOST] [--json]
agentsearch session <path> [--host HOST] [--head N] [--tail N] [--raw] [--json]   # whole session file
# --raw reads full untruncated bodies from the original JSONL (index caps each message at 20k)
agentsearch index            # incremental local reindex
agentsearch status           # counts and last sync
agentsearch mcp              # stdio MCP server
agentsearch dashboard        # local web admin panel (127.0.0.1, token-gated)
```

Multiple keywords are AND-ed. Each search hit carries `host`, `source`, `cwd`, `ts`, `role`, `kind`, `snippet`, `path`, `lineno`; use `context` with the hit's `host` to read surrounding messages.

### Remote devices

```bash
agentsearch remote add <name> [ssh-host]   # rsync-installs code and builds the remote index
agentsearch remote list
agentsearch remote update [<name>]         # re-sync code and re-index
agentsearch remote remove <name>
```

Requirements on the remote: passwordless SSH, Python 3.7+ with SQLite FTS5. Connections use SSH `ControlMaster` multiplexing via a socket in `~/.agentsearch/`; unreachable devices are skipped with a warning.

Remotes are configured per device, so devices can form a mesh: run `remote add` on each device pointing at the others. A remote always searches only its own index (the forwarded command is pinned to `--host local`), so meshed devices do not chain or duplicate queries. On hosts whose Kerberos config lacks the corporate realm (e.g. a stock MIT `krb5.conf`), a user-level `~/.krb5.conf` plus `KRB5_CONFIG` is enough; agentsearch points its SSH calls at `~/.krb5.conf` automatically when present.

## Layout

```
agentsearch/index.py      # SQLite FTS5 schema, incremental sync
agentsearch/search.py     # query builder (CJK grams), BM25 search, context lookup
agentsearch/remote.py     # SSH fan-out, RRF merge, remote install
agentsearch/dashboard.py  # stdlib web admin panel
agentsearch/mcp_server.py # zero-dep JSON-RPC stdio MCP server
agentsearch/sources/      # per-agent JSONL adapters
bin/agentsearch           # launcher
integrations/             # Pi extension, agent skill
```

The index lives at `~/.agentsearch/index.db`; remote configuration at `~/.agentsearch/remotes.json`. Nothing is uploaded anywhere.
