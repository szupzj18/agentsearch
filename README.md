# Mnemo

Full-text search over the local conversation histories of multiple coding agents — Claude Code, Codex, and Pi — from one place. A session that ran in Codex or on a devbox is searchable from inside Claude, and vice versa.

Zero dependencies: Python 3.7+ and SQLite FTS5 only.

## Messages, not shared memory

Agents already write their complete working memory to local JSONL logs. Mnemo shares that data by **communication rather than shared state**:

- There is no handoff file, no shared memory folder, no markdown that every agent must keep updated. Agents never change their behavior — Mnemo reads the logs they already produce.
- Every machine keeps its own SQLite index over its own logs. A search is a message: the query fans out over SSH to each device, the devices search locally, and ranked results merge back.
- Session content is read on the device that holds it (`context`, full-session view, raw reads are all forwarded). Nothing is copied into a central store; a laptop gains a devbox's memory without the devbox's data ever leaving it.

The result is a mesh of independent memories: add a device, it speaks the same query protocol, and every machine can recall every other machine's sessions.

## Features

- One SQLite FTS5 index over all three agents' JSONL session logs; incremental sync by file mtime/size.
- English prefix matching and Chinese substring matching (unigram + bigram column), BM25 ranking.
- Normalized message schema (text / summary / reasoning / tool_call / tool_result) with noise filtered.
- Click through from any search hit to the whole session it belongs to: the dashboard renders the full transcript as a chat thread — user bubbles, assistant narration, collapsible reasoning/tool-call/tool-result blocks, term highlighting, and prev/next navigation between matches. A raw mode reads the untruncated bodies straight from the JSONL.
- CLI, an MCP server (Claude Code, Codex), and a native Pi extension.
- Multi-device federated search: queries fan out over SSH to remote devboxes in parallel and merge by reciprocal rank fusion; `context` and the session view are routed to the device that holds the hit, so session bodies never leave the device they were created on.
- Local-only web dashboard for browser search and session reading, plus connectivity checks, per-device index stats, and search latency diagnostics.

## Install

```bash
git clone https://github.com/szupzj18/mnemo.git ~/mnemo
ln -s ~/mnemo/bin/mnemo ~/.local/bin/mnemo
mnemo index
```

The launcher resolves the package through its own symlink, so a symlink on `PATH` is enough.

### MCP server (Claude Code / Codex)

Register the stdio command `mnemo mcp` with your MCP client. Example for Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.mnemo]
command = "/Users/you/.local/bin/mnemo"
args = ["mcp"]
startup_timeout_sec = 120
```

### Pi extension

Symlink the extension into Pi's extensions directory:

```bash
ln -s ~/mnemo/integrations/pi/mnemo.ts ~/.pi/agent/extensions/mnemo.ts
```

### Skill (optional)

`integrations/skills/mnemo/SKILL.md` can be symlinked into any agent's skills directory.

## Usage

```bash
mnemo search "关键词" [--source claude,codex,pi] [--cwd SUBSTR] [--since YYYY-MM-DD] [--limit N] [--json]
mnemo context <path> <lineno> [--before 4] [--after 8] [--host HOST] [--json]
mnemo session <path> [--host HOST] [--head N] [--tail N] [--raw] [--json]   # whole session file
# --raw reads full untruncated bodies from the original JSONL (index caps each message at 20k)
mnemo index            # incremental local reindex
mnemo status           # counts and last sync
mnemo mcp              # stdio MCP server
mnemo dashboard        # local web panel (127.0.0.1, token-gated)
```

Multiple keywords are AND-ed. Each hit carries `host`, `source`, `cwd`, `ts`, `role`, `kind`, `snippet`, `path`, `lineno`; use `context` with the hit's `host` to read surrounding messages, or open the session in the dashboard to read the full transcript with highlights.

### Remote devices

```bash
mnemo remote add <name> [ssh-host]   # rsync-installs code and builds the remote index
mnemo remote list
mnemo remote update [<name>]         # re-sync code and re-index
mnemo remote remove <name>
```

Requirements on the remote: passwordless SSH, Python 3.7+ with SQLite FTS5. Connections use SSH `ControlMaster` multiplexing via a socket in `~/.mnemo/`; unreachable devices are skipped with a warning.

Remotes are configured per device, so devices can form a mesh: run `remote add` on each device pointing at the others. A remote always searches only its own index (the forwarded command is pinned to `--host local`), so meshed devices do not chain or duplicate queries. On hosts whose Kerberos config lacks the corporate realm (e.g. a stock MIT `krb5.conf`), a user-level `~/.krb5.conf` plus `KRB5_CONFIG` is enough; mnemo points its SSH calls at `~/.krb5.conf` automatically when present.

## Layout

```
mnemo/index.py      # SQLite FTS5 schema, incremental sync
mnemo/search.py     # query builder (CJK grams), BM25 search, context/session lookup
mnemo/remote.py     # SSH fan-out, RRF merge, remote install
mnemo/dashboard.py  # stdlib web panel: search UI, session transcript, admin
mnemo/mcp_server.py # zero-dep JSON-RPC stdio MCP server
mnemo/sources/      # per-agent JSONL adapters
bin/mnemo           # launcher
integrations/             # Pi extension, agent skill
```

The index lives at `~/.mnemo/index.db`; remote configuration at `~/.mnemo/remotes.json`. Session data stays on the device that produced it.
