import argparse
import datetime
import json
import os
import sys

from . import __version__
from .index import DEFAULT_DB_PATH, Index
from . import remote as remote_mod
from .remote import LOCAL, RemoteError, fan_out_search, remote_context, remote_session
from .search import DEFAULT_KINDS, get_context, get_session
from .sources import SOURCES

BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _hl(text, use_color):
    if use_color:
        return text.replace("[[", YELLOW + BOLD).replace("]]", RESET)
    return text.replace("[[", "").replace("]]", "")


def _since(value):
    if not value:
        return None
    if len(value) == 10:
        value += "T00:00:00Z"
    elif "T" not in value:
        value += "T00:00:00Z"
    return value


def cmd_index(args):
    idx = Index(args.db)
    names = args.source.split(",") if args.source else None
    stats = idx.sync(names, logger=(lambda m: print(m, file=sys.stderr)) if args.verbose else None)
    print(
        "indexed: +{files_new} new, {files_updated} updated, {files_removed} removed,"
        " {messages} messages".format(**stats)
    )
    return 0


def cmd_search(args):
    idx = Index(args.db)
    if args.all_kinds:
        kinds = []
    elif args.kind:
        kinds = args.kind.split(",")
    else:
        kinds = DEFAULT_KINDS
    hosts = [h.strip() for h in args.host.split(",")] if args.host else None
    try:
        hits, warnings = fan_out_search(
            idx,
            " ".join(args.query),
            sources=args.source.split(",") if args.source else None,
            kinds=kinds,
            cwd=args.cwd,
            since=_since(args.since),
            limit=args.limit,
            hosts=hosts,
        )
    except RemoteError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    for w in warnings:
        print("warning: %s" % w, file=sys.stderr)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("no matches (try `agentsearch index` first)")
        return 1
    color = sys.stdout.isatty()
    for i, h in enumerate(hits, 1):
        print(
            "%s%d.%s %s[%s/%s]%s %s/%s  %s%s%s  %s%s%s"
            % (
                BOLD, i, RESET,
                BOLD, h.get("host", LOCAL), h["source"], RESET,
                h["role"], h["kind"],
                DIM, h["ts"][:16].replace("T", " "), RESET,
                DIM, h["cwd"], RESET,
            )
        )
        print("   " + _hl(h["snippet"], color))
        print("   %s→ %s:%d%s" % (DIM, h["path"], h["lineno"], RESET))
    return 0


def _slice(messages, head, tail):
    if head:
        messages = messages[:head]
    elif tail:
        messages = messages[-tail:]
    return messages


def _print_messages(path, rows):
    for r in rows:
        print("%s%s:%d%s  %s%s/%s%s" % (BOLD, path, r["lineno"], RESET, DIM, r["role"], r["kind"], RESET))
        text = r["text"]
        if len(text) > 2000:
            text = text[:2000] + " …[truncated]"
        for ln in text.splitlines():
            print("    " + ln)
        print()


def cmd_session(args):
    host = args.host or LOCAL
    try:
        if host == LOCAL:
            idx = Index(args.db)
            sess = get_session(idx, args.path)
        else:
            sess = remote_session(
                remote_mod.get_remote(host), args.path,
                head=args.head, tail=args.tail,
            )
    except RemoteError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if sess is None:
        print("not in index; run `agentsearch index`", file=sys.stderr)
        return 1
    if host == LOCAL:
        sess["messages"] = _slice(sess["messages"], args.head, args.tail)
    if args.json:
        print(json.dumps(sess, ensure_ascii=False, indent=2))
        return 0
    print("%s%s%s  source=%s  cwd=%s" % (
        BOLD, args.path, RESET, sess["source"], sess["cwd"]))
    print("%s%s → %s  %d messages (showing %d)%s\n" % (
        DIM, (sess["started_at"] or "")[:16].replace("T", " "),
        (sess["ended_at"] or "")[:16].replace("T", " "),
        sess["count"], len(sess["messages"]), RESET))
    _print_messages(args.path, sess["messages"])
    return 0


def cmd_context(args):
    host = args.host or LOCAL
    if host == LOCAL:
        idx = Index(args.db)
        rows = get_context(idx, args.path, args.line, args.before, args.after)
    else:
        try:
            rows = remote_context(
                remote_mod.get_remote(host), args.path, args.line, args.before, args.after
            )
        except RemoteError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
    if rows is None:
        print("not in index; run `agentsearch index`", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    color = sys.stdout.isatty()
    for r in rows:
        mark = ">" if r["hit"] else " "
        line = "%s %s %s:%s %s %s" % (
            mark,
            r["ts"][11:16],
            r["role"],
            r["kind"],
            r["path"] if False else "",
            "",
        )
        print("%s%s:%d%s  %s%s/%s%s" % (BOLD if r["hit"] else DIM, args.path, r["lineno"], RESET, DIM, r["role"], r["kind"], RESET))
        text = r["text"]
        if len(text) > 2000:
            text = text[:2000] + " …[truncated]"
        for ln in text.splitlines():
            print("    " + ln)
        print()
    return 0


def cmd_status(args):
    idx = Index(args.db)
    counts = idx.counts()
    last = idx.last_sync()
    remotes = remote_mod.load_remotes()
    if args.json:
        print(json.dumps({
            "db": args.db,
            "last_sync": last,
            "sources": {
                name: counts.get(name, {"files": 0, "messages": 0}) for name in sorted(SOURCES)
            },
            "remotes": remotes,
        }, ensure_ascii=False, indent=2))
        return 0
    for name in sorted(SOURCES):
        c = counts.get(name, {"files": 0, "messages": 0})
        print("%-8s %5d sessions  %8d messages" % (name, c["files"], c["messages"]))
    if last:
        print("last sync:", datetime.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S"))
    print("db:       %s" % args.db)
    for r in remotes:
        print("remote:   %s → %s (%s)" % (r["name"], r["host"], r["bin"]))
    return 0


def cmd_remote_add(args):
    remote = {"name": args.name, "host": args.ssh_host or args.name,
              "bin": args.bin or "~/agentsearch/bin/agentsearch"}
    try:
        if any(r["name"] == remote["name"] for r in remote_mod.load_remotes()):
            raise RemoteError("remote %r already registered; remove it first" % remote["name"])
        print("connecting to %s ..." % remote["host"], file=sys.stderr)
        remote_mod.ping(remote)
        print("installing code and building index ...", file=sys.stderr)
        remote_mod.install(remote, logger=lambda m: print(m, file=sys.stderr))
        remote_mod.add_remote(remote["name"], remote["host"], remote["bin"])
    except RemoteError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("added remote %s (%s)" % (remote["name"], remote["host"]))
    return 0


def cmd_remote_list(args):
    remotes = remote_mod.load_remotes()
    if not remotes:
        print("no remotes; add one with: agentsearch remote add <name> [ssh-host]")
        return 0
    for r in remotes:
        print("%-16s %-32s %s" % (r["name"], r["host"], r["bin"]))
    return 0


def cmd_remote_remove(args):
    try:
        remote_mod.remove_remote(args.name)
    except RemoteError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("removed remote %s" % args.name)
    return 0


def cmd_dashboard(args):
    from .dashboard import DEFAULT_PORT, serve
    serve(port=args.port or DEFAULT_PORT, open_browser=not args.no_open)
    return 0


def cmd_remote_update(args):
    try:
        remotes = remote_mod.load_remotes()
        if args.name:
            remotes = [r for r in remotes if r["name"] == args.name]
            if not remotes:
                raise RemoteError("no remote named %r" % args.name)
        for r in remotes:
            print("updating %s ..." % r["name"], file=sys.stderr)
            remote_mod.install(r, logger=lambda m: print("  %s" % m, file=sys.stderr))
            print("updated %s" % r["name"])
    except RemoteError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="agentsearch", description="search across agent sessions on this machine and remote devices")
    p.add_argument("--db", default=DEFAULT_DB_PATH, help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version="agentsearch " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("index", help="incrementally index local sessions")
    sp.add_argument("--source", help="comma-separated: %s" % ",".join(SOURCES))
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("search", aliases=["query"], help="full-text search")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--source", help="comma-separated source filter")
    sp.add_argument("--kind", help="comma-separated kinds: text,summary,tool_call,tool_result,reasoning")
    sp.add_argument("--all-kinds", action="store_true")
    sp.add_argument("--cwd", help="substring match on working directory")
    sp.add_argument("--since", help="YYYY-MM-DD")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--host", help="comma-separated devices (default: local + all remotes)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("context", help="show messages around a hit")
    sp.add_argument("path")
    sp.add_argument("line", type=int)
    sp.add_argument("--before", type=int, default=4)
    sp.add_argument("--after", type=int, default=8)
    sp.add_argument("--host", help="device holding the hit (default: local)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_context)

    sp = sub.add_parser("session", help="dump all indexed messages of one session file")
    sp.add_argument("path")
    sp.add_argument("--host", help="device holding the session (default: local)")
    sp.add_argument("--head", type=int, default=0, help="only first N messages")
    sp.add_argument("--tail", type=int, default=0, help="only last N messages")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_session)

    sp = sub.add_parser("status", help="index stats")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("remote", help="manage remote devices")
    rsub = sp.add_subparsers(dest="remote_cmd", required=True)

    rsp = rsub.add_parser("add", help="install agentsearch on a device over SSH and register it")
    rsp.add_argument("name", help="local name for the device, e.g. devbox-109")
    rsp.add_argument("ssh_host", nargs="?", help="SSH host alias (defaults to name)")
    rsp.add_argument("--bin", help="remote agentsearch launcher path (default: ~/agentsearch/bin/agentsearch)")
    rsp.set_defaults(func=cmd_remote_add)

    rsp = rsub.add_parser("list", help="list registered devices")
    rsp.set_defaults(func=cmd_remote_list)

    rsp = rsub.add_parser("remove", help="unregister a device (does not touch files on it)")
    rsp.add_argument("name")
    rsp.set_defaults(func=cmd_remote_remove)

    rsp = rsub.add_parser("update", help="re-sync code and re-index one device or all remotes")
    rsp.add_argument("name", nargs="?", help="device name (default: all)")
    rsp.set_defaults(func=cmd_remote_update)

    sp = sub.add_parser("mcp", help="run MCP stdio server")
    sp.set_defaults(func=lambda a: __import__("agentsearch.mcp_server", fromlist=["run"]).run())

    sp = sub.add_parser("dashboard", help="open the local web admin panel")
    sp.add_argument("--port", type=int, default=7787)
    sp.add_argument("--no-open", action="store_true", help="do not open a browser")
    sp.set_defaults(func=cmd_dashboard)

    args = p.parse_args(argv)
    return args.func(args)
