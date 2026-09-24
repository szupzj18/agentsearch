import json
import sys

from . import __version__
from .index import Index
from . import remote as remote_mod
from .remote import LOCAL, fan_out_search, remote_context, remote_session
from .search import DEFAULT_KINDS, get_context, get_session


def build_tools():
    names = [LOCAL] + [r["name"] for r in remote_mod.load_remotes()]
    host_desc = "comma-separated devices to search; available: %s (default: all)" % ",".join(names)
    return [
        {
            "name": "search_sessions",
            "description": (
                "Full-text search across coding-agent sessions (claude, codex, pi) on this machine"
                " and registered remote devices. Matches user prompts, assistant replies, summaries,"
                " tool calls and tool results. Each hit gives host, source, cwd, timestamp, a snippet,"
                " and file:line for get_context (pass the hit's host to get_context); pass the hit's path alone to get_session for the whole session file."
                " Supports English (prefix) and Chinese (substring via bigrams)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "keywords, whitespace-separated (all must match)"},
                    "source": {"type": "string", "description": "comma-separated subset of: claude,codex,pi"},
                    "kinds": {
                        "type": "string",
                        "description": "comma-separated subset of: text,summary,tool_call,tool_result,reasoning (default: text,summary,tool_call,tool_result)",
                    },
                    "cwd": {"type": "string", "description": "only sessions whose working directory contains this substring"},
                    "since": {"type": "string", "description": "YYYY-MM-DD"},
                    "host": {"type": "string", "description": host_desc},
                    "limit": {"type": "integer", "description": "max hits (default 20)"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_context",
            "description": (
                "Fetch surrounding messages of a search hit (same normalized format) so a hit can be read in context."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "host": {"type": "string", "description": "device holding the hit, from the search result (default: local)"},
                    "before": {"type": "integer", "description": "messages before the hit (default 4)"},
                    "after": {"type": "integer", "description": "messages after the hit (default 8)"},
                },
                "required": ["path", "line"],
            },
        },
        {
            "name": "get_session",
            "description": (
                "Fetch every indexed message of the whole session file that a search hit belongs to"
                " (same normalized format), ordered by time. Use after get_context when you need the"
                " full conversation rather than a window around one line. Each message body is capped"
                " at 20k characters in the index. head/tail select only the first/last N messages."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "host": {"type": "string", "description": "device holding the session, from the search result (default: local)"},
                    "head": {"type": "integer", "description": "only return the first N messages"},
                    "tail": {"type": "integer", "description": "only return the last N messages"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "reindex",
            "description": "Incrementally scan local session files for new/changed sessions. Cheap when nothing changed.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "comma-separated subset of: claude,codex,pi"},
                },
            },
        },
    ]


def _text_result(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, indent=2)}]}


def _since(value):
    if not value:
        return None
    if len(value) == 10:
        return value + "T00:00:00Z"
    return value


def handle_call(name, args, index):
    args = args or {}
    if name == "search_sessions":
        query = args.get("query", "").strip()
        if not query:
            raise ValueError("query is required")
        sources = args.get("source")
        kinds = args.get("kinds")
        host = args.get("host")
        hosts = [h.strip() for h in host.split(",")] if host else None
        hits, warnings = fan_out_search(
            index,
            query,
            sources=sources.split(",") if sources else None,
            kinds=kinds.split(",") if kinds else list(DEFAULT_KINDS),
            cwd=args.get("cwd"),
            since=_since(args.get("since")),
            limit=min(int(args.get("limit", 20)), 100),
            hosts=hosts,
        )
        text = json.dumps(hits, ensure_ascii=False, indent=2)
        if warnings:
            text += "\n\nunreachable devices (excluded from results):\n" + "\n".join(
                "- " + w for w in warnings
            )
        return {"content": [{"type": "text", "text": text}]}
    if name == "get_context":
        host = args.get("host") or LOCAL
        if host == LOCAL:
            rows = get_context(
                index,
                args["path"],
                int(args["line"]),
                before=int(args.get("before", 4)),
                after=int(args.get("after", 8)),
            )
        else:
            rows = remote_context(
                remote_mod.get_remote(host),
                args["path"],
                int(args["line"]),
                int(args.get("before", 4)),
                int(args.get("after", 8)),
            )
        if rows is None:
            raise ValueError("path not in index; run reindex")
        return _text_result(rows)
    if name == "get_session":
        host = args.get("host") or LOCAL
        head = args.get("head")
        tail = args.get("tail")
        if host == LOCAL:
            sess = get_session(index, args["path"])
        else:
            sess = remote_session(
                remote_mod.get_remote(host), args["path"],
                head=head, tail=tail,
            )
        if sess is None:
            raise ValueError("path not in index; run reindex")
        if host == LOCAL and (head or tail):
            sess["messages"] = sess["messages"][:head] if head else sess["messages"][-tail:]
        return _text_result(sess)
    if name == "reindex":
        sources = args.get("source")
        stats = index.sync(sources.split(",") if sources else None)
        return _text_result(stats)
    raise ValueError("unknown tool: %s" % name)


def run():
    sys.stderr.write("agentsearch mcp: indexing sessions...\n")
    index = Index()
    stats = index.sync(logger=lambda m: sys.stderr.write(m + "\n"))
    sys.stderr.write("agentsearch mcp: ready (%s)\n" % json.dumps(stats))
    tools = build_tools()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        method = req.get("method")
        rid = req.get("id")

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": (req.get("params") or {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agentsearch", "version": __version__},
                },
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}
        elif method == "tools/call":
            params = req.get("params") or {}
            try:
                result = handle_call(params.get("name"), params.get("arguments"), index)
            except Exception as exc:
                result = {"content": [{"type": "text", "text": "error: %s" % exc}], "isError": True}
            resp = {"jsonrpc": "2.0", "id": rid, "result": result}
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}}
        else:
            if rid is None:
                continue
            resp = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": "method not found: %s" % method},
            }
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    run()
