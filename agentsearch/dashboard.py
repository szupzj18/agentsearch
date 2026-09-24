import concurrent.futures
import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import remote as remote_mod
from .index import DEFAULT_DB_PATH, Index
from .remote import LOCAL, RemoteError
from .search import DEFAULT_KINDS, get_session, raw_session
from .sources import SOURCES

DEFAULT_PORT = 7787


def local_status():
    idx = Index(DEFAULT_DB_PATH)
    try:
        counts = idx.counts()
        last = idx.last_sync()
    finally:
        idx.close()
    return {
        "db": DEFAULT_DB_PATH,
        "last_sync": last,
        "sources": {
            name: counts.get(name, {"files": 0, "messages": 0}) for name in sorted(SOURCES)
        },
        "remotes": remote_mod.load_remotes(),
    }


def local_sync():
    idx = Index(DEFAULT_DB_PATH)
    try:
        return idx.sync()
    finally:
        idx.close()


def local_session(path, raw=False):
    idx = Index(DEFAULT_DB_PATH)
    try:
        return raw_session(idx, path) if raw else get_session(idx, path)
    finally:
        idx.close()


def _time(fn):
    t = time.time()
    value = fn()
    return value, int((time.time() - t) * 1000)


def ping_all(remotes):
    out = []
    for r in remotes:
        try:
            _, ms = _time(lambda r=r: remote_mod.remote_exec(r, ["--version"], timeout=12))
            out.append({"name": r["name"], "ok": True, "ms": ms})
        except RemoteError as exc:
            out.append({"name": r["name"], "ok": False, "error": str(exc)})
    return out


def diagnose_search(query, hosts, limit):
    remotes = remote_mod.load_remotes()
    wanted = set(hosts) if hosts else None
    selected = [r for r in remotes if wanted is None or r["name"] in wanted]
    include_local = wanted is None or LOCAL in wanted
    per_host, warnings, payload = [], [], []

    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected) + 1) as pool:
        if include_local:
            jobs[pool.submit(_time, lambda: remote_mod._local_search(
                DEFAULT_DB_PATH, query, None, list(DEFAULT_KINDS), None, None, limit))] = LOCAL
        for r in selected:
            def run_remote(r=r):
                return remote_mod._remote_search(
                    r, query, None, list(DEFAULT_KINDS), None, None, limit, True)
            jobs[pool.submit(_time, run_remote)] = r["name"]
        for fut in concurrent.futures.as_completed(jobs):
            name = jobs[fut]
            try:
                rows, ms = fut.result()
                per_host.append({"host": name, "ok": True, "ms": ms, "hits": len(rows)})
                for h in rows:
                    h["host"] = name
                payload.append((name, rows))
            except RemoteError as exc:
                per_host.append({"host": name, "ok": False, "error": str(exc)})
                warnings.append(str(exc))

    merged = remote_mod._rrf(payload, limit)
    return {"per_host": per_host, "merged": merged, "warnings": warnings}


class Handler(BaseHTTPRequestHandler):
    token = ""
    server_version = "agentsearch-dashboard"

    def log_message(self, fmt, *args):
        return

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _authorized(self):
        host = self.headers.get("Host", "")
        if host.split(":")[0] not in ("127.0.0.1", "localhost", "[::1]"):
            self._json({"error": "bad host"}, 403)
            return False
        if self.path.startswith("/api/") and self.headers.get("X-Dashboard-Token") != self.token:
            self._json({"error": "unauthorized"}, 403)
            return False
        return True

    def do_GET(self):
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = PAGE.replace("__TOKEN__", self.token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/status":
            self._json(local_status())
            return
        if parsed.path == "/api/remote-status":
            qs = parse_qs(parsed.query)
            try:
                r = remote_mod.get_remote(qs.get("name", [""])[0])
                value, ms = _time(lambda: remote_mod.remote_status(r))
                value["name"] = r["name"]
                value["ms"] = ms
                self._json({"ok": True, "status": value})
            except RemoteError as exc:
                self._json({"ok": False, "error": str(exc)})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return
        data = self._body()
        path = urlparse(self.path).path
        try:
            if path == "/api/ping":
                remotes = remote_mod.load_remotes()
                if data.get("name"):
                    remotes = [remote_mod.get_remote(data["name"])]
                self._json({"results": ping_all(remotes)})
            elif path == "/api/sync":
                if data.get("name"):
                    r = remote_mod.get_remote(data["name"])
                    value, ms = _time(lambda: remote_mod.remote_exec(r, ["index"], timeout=600))
                    self._json({"ok": True, "name": r["name"], "ms": ms, "output": value.strip()})
                else:
                    value, ms = _time(local_sync)
                    self._json({"ok": True, "ms": ms, "stats": value})
            elif path == "/api/remotes/add":
                name = (data.get("name") or "").strip()
                host = (data.get("host") or "").strip()
                if not name or not host:
                    raise RemoteError("name and host are required")
                if any(r["name"] == name for r in remote_mod.load_remotes()):
                    raise RemoteError("remote %r already registered" % name)
                remote = {"name": name, "host": host,
                          "bin": data.get("bin") or "~/agentsearch/bin/agentsearch"}
                logs = []
                remote_mod.install(remote, logger=logs.append)
                remote_mod.add_remote(name, host, remote["bin"])
                self._json({"ok": True, "logs": logs})
            elif path == "/api/remotes/remove":
                remote_mod.remove_remote((data.get("name") or "").strip())
                self._json({"ok": True})
            elif path == "/api/remotes/update":
                remotes = remote_mod.load_remotes()
                if data.get("name"):
                    remotes = [remote_mod.get_remote(data["name"])]
                logs = []
                for r in remotes:
                    remote_mod.install(r, logger=lambda m, n=r["name"]: logs.append(n + ": " + m))
                self._json({"ok": True, "logs": logs})
            elif path == "/api/search":
                query = (data.get("query") or "").strip()
                if not query:
                    raise RemoteError("query is required")
                limit = min(int(data.get("limit", 20)), 50)
                self._json(diagnose_search(query, data.get("hosts"), limit))
            elif path == "/api/session":
                path_value = (data.get("path") or "").strip()
                if not path_value:
                    raise RemoteError("path is required")
                host = data.get("host") or LOCAL
                raw = bool(data.get("raw"))
                head, tail = data.get("head"), data.get("tail")
                if host == LOCAL:
                    value = local_session(path_value, raw)
                    if value and (head or tail):
                        if head:
                            value["messages"] = value["messages"][: int(head)]
                        else:
                            value["messages"] = value["messages"][-int(tail):]
                else:
                    value = remote_mod.remote_session(
                        remote_mod.get_remote(host), path_value,
                        head=head, tail=tail, raw=raw, timeout=180,
                    )
                if value is None:
                    self._json({"ok": False, "error": "path not in index; sync first"}, 404)
                else:
                    self._json({"ok": True, "session": value})
            else:
                self._json({"error": "not found"}, 404)
        except (RemoteError, ValueError, KeyError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)


def serve(port=DEFAULT_PORT, open_browser=True):
    Handler.token = secrets.token_urlsafe(16)
    httpd = None
    for candidate in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("no free port in %d-%d" % (port, port + 9))
    url = "http://127.0.0.1:%d/" % port
    if open_browser:
        webbrowser.open(url)
    print("agentsearch dashboard: %s (Ctrl-C to stop)" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


PAGE = r"""<!doctype html>
<html lang="zh" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="token" content="__TOKEN__">
<title>agentsearch 管理面板</title>
<style>
:root {
  --bg: #eff2f7;
  --bg-grad: linear-gradient(120deg,#f0f7ff 0%,#e7f2ff 50%,#edf7ff 100%);
  --blob-1: rgba(122,162,255,.45); --blob-2: rgba(107,197,255,.4);
  --surface: rgba(255,255,255,.92);
  --surface-2: #ffffff;
  --surface-soft: rgba(255,255,255,.62);
  --border: rgba(15,23,42,.08);
  --border-strong: rgba(15,23,42,.14);
  --text: #2c3e50; --text-2: #5f6c7b; --text-3: #8b95a6;
  --accent: #3b82f6; --accent-soft: rgba(59,130,246,.1); --accent-border: rgba(59,130,246,.3);
  --ok: #16a34a; --ok-soft: rgba(22,163,74,.12);
  --warn: #d97706; --warn-soft: rgba(245,158,11,.14);
  --err: #dc2626; --err-soft: rgba(220,38,38,.1);
  --sidebar-bg: rgba(255,255,255,.6);
  --hover: rgba(59,130,246,.08);
  --shadow: 0 8px 24px rgba(31,38,135,.08);
  --r-lg: 16px; --r-md: 10px; --r-sm: 8px;
}
[data-theme="dark"] {
  --bg: #14161b;
  --bg-grad: linear-gradient(120deg,#161a22 0%,#151925 50%,#141a1f 100%);
  --blob-1: rgba(59,130,246,.16); --blob-2: rgba(16,185,129,.08);
  --surface: rgba(30,33,42,.92);
  --surface-2: #20242d;
  --surface-soft: rgba(255,255,255,.04);
  --border: rgba(255,255,255,.08);
  --border-strong: rgba(255,255,255,.14);
  --text: #e6e8ee; --text-2: #9aa3b2; --text-3: #6f7888;
  --accent-soft: rgba(59,130,246,.18); --accent-border: rgba(59,130,246,.4);
  --ok: #4ade80; --ok-soft: rgba(74,222,128,.12);
  --warn: #fbbf24; --warn-soft: rgba(251,191,36,.12);
  --err: #f87171; --err-soft: rgba(248,113,113,.12);
  --sidebar-bg: rgba(22,24,30,.6);
  --hover: rgba(255,255,255,.06);
  --shadow: 0 8px 24px rgba(0,0,0,.35);
}
* { box-sizing: border-box; }
body { margin:0; font:13.5px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;
       color:var(--text); background:var(--bg); min-height:100vh; }
body::before { content:""; position:fixed; inset:0; background:var(--bg-grad); z-index:-2; }
body::after { content:""; position:fixed; inset:0; z-index:-1;
  background:radial-gradient(420px 320px at 12% -5%, var(--blob-1), transparent 70%),
            radial-gradient(480px 340px at 95% 0%, var(--blob-2), transparent 70%); }
code,.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }

/* layout */
.sidebar { position:fixed; inset:0 auto 0 0; width:210px; padding:18px 14px; z-index:20;
  background:var(--sidebar-bg); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
  border-right:1px solid var(--border); display:flex; flex-direction:column; gap:6px; }
.brand { display:flex; align-items:center; gap:10px; padding:4px 10px 18px; font-weight:700; font-size:15px; }
.brand .logo { width:28px; height:28px; border-radius:8px; background:linear-gradient(135deg,#3b82f6,#22d3ee);
  display:grid; place-items:center; color:#fff; flex:none; }
.nav { display:flex; flex-direction:column; gap:3px; }
.nav a { display:flex; align-items:center; gap:11px; padding:9px 12px; border-radius:var(--r-md);
  color:var(--text-2); text-decoration:none; cursor:pointer; font-weight:500; transition:background .15s; }
.nav a:hover { background:var(--hover); }
.nav a.active { background:var(--surface-2); color:var(--text); box-shadow:0 1px 4px rgba(31,38,135,.08); border:1px solid var(--border); }
.nav a.active svg { color:var(--accent); }
.nav svg { width:18px; height:18px; flex:none; }
.sidebar-foot { margin-top:auto; padding:10px 12px; color:var(--text-3); font-size:11.5px; }

.main { margin-left:210px; padding:18px 28px 40px; }
.header { display:flex; align-items:center; gap:12px; margin-bottom:18px; }
.header h1 { font-size:19px; margin:0; font-weight:650; }
.header .spacer { flex:1; }
.iconbtn { display:inline-flex; align-items:center; gap:6px; }
.iconbtn svg { width:15px; height:15px; }

.card { background:var(--surface); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
  border:1px solid var(--border); border-radius:var(--r-lg); box-shadow:var(--shadow); padding:20px 22px; margin-bottom:18px; }
.card h2 { font-size:14px; font-weight:650; margin:0 0 14px; display:flex; align-items:center; gap:8px; }
.card h2::before { content:""; width:3.5px; height:14px; border-radius:2px; background:var(--accent); }
.card h2 .spacer { flex:1; }
.grid { display:grid; gap:14px; }
.g4 { grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); }
.g3 { grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); }

/* status banner */
.banner { display:flex; align-items:center; gap:18px; flex-wrap:wrap; }
.dot { width:9px; height:9px; border-radius:50%; background:var(--ok); box-shadow:0 0 0 4px var(--ok-soft); flex:none; }
.dot.bad { background:var(--err); box-shadow:0 0 0 4px var(--err-soft); }
.banner .k { color:var(--text-3); font-size:12px; }
.banner .v { font-weight:600; }
.pill { display:inline-flex; align-items:center; gap:5px; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; }
.pill.ok { background:var(--ok-soft); color:var(--ok); } .pill.bad { background:var(--err-soft); color:var(--err); }
.pill.warn { background:var(--warn-soft); color:var(--warn); } .pill.idle { background:var(--surface-soft); color:var(--text-3); }

/* stat cards */
.stat { background:var(--surface-2); border:1px solid var(--border); border-radius:var(--r-md); padding:15px 17px; }
.stat .top { display:flex; align-items:center; gap:10px; color:var(--text-3); font-size:12.5px; }
.stat .ico { width:30px; height:30px; border-radius:8px; display:grid; place-items:center; background:var(--accent-soft); color:var(--accent); }
.stat .ico svg { width:16px; height:16px; }
.stat .num { font-size:26px; font-weight:700; margin:8px 0 2px; letter-spacing:-.02em; }
.stat .sub { color:var(--text-3); font-size:11.5px; }

/* device card */
.dev { background:var(--surface-2); border:1px solid var(--border); border-radius:var(--r-md); padding:16px 18px; display:flex; flex-direction:column; gap:10px; }
.dev .head { display:flex; align-items:center; gap:9px; }
.dev .name { font-weight:650; font-size:14.5px; }
.health { margin-left:auto; width:30px; height:30px; border-radius:50%; display:grid; place-items:center; }
.health.ok { background:var(--ok-soft); color:var(--ok); } .health.bad { background:var(--err-soft); color:var(--err); }
.health.unknown { background:var(--surface-soft); color:var(--text-3); }
.health svg { width:15px; height:15px; }
.dev .meta { color:var(--text-2); font-size:12px; display:grid; gap:3px; }
.dev .meta b { color:var(--text); font-weight:550; }
.dev .acts { display:flex; gap:7px; flex-wrap:wrap; margin-top:2px; }
.spin { display:inline-block; width:12px; height:12px; border:2px solid currentColor; border-top-color:transparent; border-radius:50%; animation:rot .7s linear infinite; vertical-align:-2px; }
@keyframes rot { to { transform:rotate(360deg); } }

button { font:inherit; font-size:12.5px; font-weight:550; border-radius:var(--r-sm); cursor:pointer;
  border:1px solid var(--border-strong); background:var(--surface-2); color:var(--text-2); padding:6px 13px; transition:all .15s; }
button:hover { border-color:var(--accent-border); color:var(--accent); background:var(--hover); }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
button.primary:hover { filter:brightness(1.07); color:#fff; background:var(--accent); }
button.danger { color:var(--err); } button.danger:hover { border-color:var(--err); background:var(--err-soft); color:var(--err); }
button:disabled { opacity:.55; cursor:wait; }
input,select { font:inherit; font-size:13px; background:var(--surface-2); border:1px solid var(--border-strong);
  color:var(--text); border-radius:var(--r-sm); padding:7px 11px; outline:none; transition:border-color .15s; }
input:focus { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
label.chk { display:inline-flex; align-items:center; gap:6px; color:var(--text-2); font-size:12.5px; cursor:pointer; user-select:none; }

table { width:100%; border-collapse:collapse; font-size:12.5px; }
th { text-align:left; color:var(--text-3); font-weight:550; padding:7px 9px; border-bottom:1px solid var(--border); white-space:nowrap; }
td { padding:8px 9px; border-bottom:1px solid var(--border); vertical-align:top; }
tr:last-child td { border-bottom:none; }
tbody tr:hover { background:var(--surface-soft); }
.hosttag { display:inline-block; padding:1.5px 9px; border-radius:999px; font-size:11.5px; font-weight:600;
  background:var(--accent-soft); color:var(--accent); white-space:nowrap; }
.hosttag.local { background:rgba(34,211,238,.14); color:#0891b2; }
[data-theme="dark"] .hosttag.local { color:#67e8f9; }
.hosttag.bad { background:var(--err-soft); color:var(--err); }
.snip { color:var(--text-2); max-width:460px; }
.path { color:var(--text-3); }
.muted { color:var(--text-3); }
.view { display:none; animation:fade .2s ease; }
.view.active { display:block; }
@keyframes fade { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }

/* search hit list */
.hitlist { display:flex; flex-direction:column; gap:9px; }
.hit { display:grid; grid-template-columns:28px 36px 1fr 18px; gap:12px; align-items:start;
  padding:13px 16px; background:var(--surface-2); border:1px solid var(--border); border-radius:12px;
  cursor:pointer; transition:border-color .15s, box-shadow .15s, transform .15s; }
.hit:hover { border-color:var(--accent-border); box-shadow:0 6px 18px rgba(31,38,135,.1); transform:translateY(-1px); }
.hit-rank { color:var(--text-3); font-size:12px; padding-top:8px; text-align:center; font-variant-numeric:tabular-nums; }
.hit-ico { width:36px; height:36px; border-radius:10px; display:grid; place-items:center; color:#fff; flex:none; }
.hit-ico.claude { background:linear-gradient(135deg,#e0865c,#d97757); }
.hit-ico.codex { background:linear-gradient(135deg,#3f3f46,#18181b); }
.hit-ico.pi { background:linear-gradient(135deg,#a78bfa,#7c3aed); }
.hit-ico svg { width:17px; height:17px; }
.hit-main { min-width:0; }
.hit-meta { display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:12px; color:var(--text-2); margin-bottom:4px; }
.hit-snip { color:var(--text-2); font-size:13px; line-height:1.6; display:-webkit-box; -webkit-line-clamp:2;
  -webkit-box-orient:vertical; overflow:hidden; word-break:break-word; }
.hit-path { color:var(--text-3); font-size:11.5px; margin-top:6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.hit-go { color:var(--text-3); padding-top:8px; transition:color .15s, transform .15s; }
.hit:hover .hit-go { color:var(--accent); transform:translateX(2px); }
.srcbadge { font-size:11px; font-weight:700; padding:1.5px 8px; border-radius:6px;
  background:var(--surface-soft); color:var(--text-2); border:1px solid var(--border); }
mark { background:rgba(250,204,21,.38); color:inherit; border-radius:3px; padding:0 1px; }
[data-theme="dark"] mark { background:rgba(250,204,21,.28); }

/* session transcript */
.sess-bar { position:sticky; top:0; z-index:15; margin:-18px -28px 20px; padding:12px 28px;
  background:var(--surface); backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px);
  border-bottom:1px solid var(--border); display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.sess-bar .st { min-width:0; }
.sess-bar .st .t1 { font-weight:650; font-size:13.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:380px; }
.sess-bar .st .t2 { font-size:11.5px; color:var(--text-3); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:460px; }
.sess-bar .mini { min-width:48px; text-align:center; font-size:11.5px; padding:6px 9px; }
#sessMeta { font-size:12px; color:var(--text-2); display:flex; align-items:center; gap:8px; flex-wrap:wrap; min-width:0; }
#sessMeta .muted { overflow:hidden; text-overflow:ellipsis; }
.trans { max-width:748px; margin:0 auto; display:flex; flex-direction:column; gap:16px; padding-bottom:60px; }
.loadbar { display:flex; justify-content:center; }
.loadbar button { border:none; border-radius:14px; padding:4px 14px; font-size:12px;
  color:var(--text-2); background:var(--surface-soft); cursor:pointer; }
.loadbar button:hover { background:var(--hover); color:var(--accent); }

/* day divider */
.daysep { display:flex; align-items:center; gap:12px; color:var(--text-3); font-size:12px; }
.daysep::before, .daysep::after { content:""; flex:1; height:1px; background:var(--border); }

/* flow rows (DeepSeekHarness-style transcript) */
.msg { min-width:0; animation:fade .2s ease; }
.msg.user { display:flex; flex-direction:column; align-items:flex-end; gap:6px; }
.bubble { max-width:72%; background:#EDF3FE; color:#0F1115; border-radius:22px;
  padding:10px 16px; font-size:14px; line-height:22px; white-space:pre-wrap; word-break:break-word; }
[data-theme="dark"] .bubble { background:#2C2C2E; color:#E6E8EE; }
.msg.user.mhit .bubble { background:#D3E2FF; }
[data-theme="dark"] .msg.user.mhit .bubble { background:#33415e; }
.msg.user .meta { display:flex; gap:7px; align-items:center; justify-content:flex-end;
  font-size:11.5px; color:var(--text-3); padding-right:6px; opacity:0; transition:opacity .15s; }
.msg.user:hover .meta { opacity:1; }

/* assistant narration: borderless full-width markdown */
.msg.assistant { font-size:14px; line-height:24px; color:var(--text); }
.msg.assistant .mbody { white-space:pre-wrap; word-break:break-word; }
.msg.assistant.mhit { background:rgba(65,118,230,.08); border-radius:10px; padding:6px 12px 4px; }
[data-theme="dark"] .msg.assistant.mhit { background:rgba(65,118,230,.18); }
.ameta { display:flex; gap:8px; align-items:center; font-size:11.5px; color:var(--text-3);
  margin-top:3px; opacity:0; transition:opacity .15s; }
.msg.assistant:hover .ameta, .msg.mhit .ameta { opacity:1; }

/* disclosure rows: thinking / tool calls & results / summary marker */
.disc { width:100%; }
.disc > summary { display:flex; align-items:center; gap:6px; min-width:0; height:26px;
  list-style:none; cursor:pointer; color:var(--text-3); font-size:13px; line-height:20px;
  user-select:none; border-radius:6px; padding:0; }
.disc > summary::-webkit-details-marker { display:none; }
.disc > summary:hover { background:var(--surface-soft); }
.disc .chev { flex:none; width:16px; height:16px; color:var(--text-3);
  transform:rotate(-90deg); transition:transform .1s ease; }
.disc[open] > summary .chev { transform:rotate(0deg); }
.disc .dt { flex:none; font-weight:400; color:var(--text-2); }
.disc .ddot { flex:none; width:2px; height:2px; border-radius:1px; background:var(--text-3); }
.disc .dsum { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.disc .dln { flex:none; font-size:11.5px; }
.disc[open] > summary { border-bottom:1px solid var(--border); border-radius:6px 6px 0 0;
  margin-bottom:8px; height:33px; padding-bottom:8px; }
.disc .dbody { padding:2px 0 4px 22px; font-size:13px; line-height:20px; color:var(--text-3);
  white-space:pre-wrap; word-break:break-word; }
.disc.tool .dbody { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
  line-height:1.6; background:var(--surface-soft); border-radius:8px; margin:0 0 4px; padding:8px 12px; }
.disc.summarydisc > summary { height:24px; }
.disc.summarydisc[open] > summary { height:33px; }
.msg.other .mbody { white-space:pre-wrap; word-break:break-word; font-size:13px; color:var(--text-2); }

/* hit marker */
.hittag { flex:none; font-size:10.5px; font-weight:600; padding:0 6px; border-radius:6px;
  background:var(--accent-soft); color:var(--accent); line-height:18px; }

.mbody code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px;
  background:var(--surface-soft); border:1px solid var(--border); border-radius:4px; padding:0 4px; }
.codeblock { background:var(--surface-soft); border:1px solid var(--border); border-radius:8px;
  padding:10px 13px; margin:6px 0; overflow:auto; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:12.5px; line-height:1.6; white-space:pre; }
.mbody.clamp, .dbody.clamp { max-height:300px; overflow:hidden; position:relative; }
.mbody.clamp::after, .dbody.clamp::after { content:""; position:absolute; inset:auto 0 0 0; height:76px;
  background:linear-gradient(transparent, var(--bg)); pointer-events:none; }
.bubble .mbody.clamp::after { background:linear-gradient(transparent, #EDF3FE); }
[data-theme="dark"] .bubble .mbody.clamp::after { background:linear-gradient(transparent, #2C2C2E); }
.disc.tool .dbody.clamp::after { background:linear-gradient(transparent, var(--surface-soft));
  border-radius:0 0 8px 8px; left:0; right:0; }
.mexpand { margin-top:7px; font-size:11.5px; padding:3px 10px; border-radius:14px; border:none;
  color:var(--text-2); background:var(--surface-soft); cursor:pointer; }
.mexpand:hover { color:var(--accent); background:var(--hover); }


/* toasts */
#toasts { position:fixed; top:18px; right:22px; z-index:60; display:flex; flex-direction:column; gap:9px; width:340px; }
.toast { display:flex; gap:10px; align-items:flex-start; background:var(--surface-2); border:1px solid var(--border);
  border-left:3px solid var(--accent); border-radius:var(--r-md); box-shadow:var(--shadow); padding:11px 14px;
  animation:slidein .22s ease; }
.toast.ok { border-left-color:var(--ok); } .toast.err { border-left-color:var(--err); }
.toast .tmsg { font-size:12.5px; word-break:break-word; }
.toast .tmsg small { color:var(--text-3); display:block; margin-top:2px; }
@keyframes slidein { from { opacity:0; transform:translateX(16px); } to { opacity:1; transform:none; } }

/* modal */
#modal { position:fixed; inset:0; z-index:50; display:none; place-items:center;
  background:rgba(15,23,42,.32); backdrop-filter:blur(2px); }
#modal.show { display:grid; }
.modal-box { background:var(--surface-2); border:1px solid var(--border); border-radius:var(--r-lg);
  box-shadow:0 20px 60px rgba(15,23,42,.25); width:420px; max-width:calc(100vw - 40px); padding:22px 24px; animation:pop .18s ease; }
@keyframes pop { from { opacity:0; transform:scale(.96); } to { opacity:1; transform:none; } }
.modal-box h3 { margin:0 0 8px; font-size:15px; } .modal-box p { margin:0 0 18px; color:var(--text-2); font-size:13px; word-break:break-word; }
.modal-acts { display:flex; justify-content:flex-end; gap:9px; }

#logbox { background:var(--surface-2); border:1px solid var(--border); border-radius:var(--r-md);
  height:calc(100vh - 230px); min-height:300px; overflow:auto; padding:12px 16px;
  font-family:ui-monospace,Menlo,monospace; font-size:12px; }
.logline { padding:2.5px 0; color:var(--text-2); white-space:pre-wrap; }
.logline .lt { color:var(--text-3); margin-right:8px; }
.logline.ok { color:var(--ok); } .logline.err { color:var(--err); }
.empty { text-align:center; color:var(--text-3); padding:44px 0; font-size:13px; }
.empty svg { width:34px; height:34px; margin-bottom:8px; opacity:.5; }
@media (max-width: 860px) {
  .sidebar { width:54px; } .brand span, .nav a span, .sidebar-foot { display:none; }
  .brand { justify-content:center; padding-left:0; padding-right:0; } .nav a { justify-content:center; }
  .main { margin-left:54px; padding:14px; }
  .sess-bar { margin:-14px -14px 16px; padding:10px 14px; }
  .sess-bar .st .t1, .sess-bar .st .t2 { max-width:52vw; }
  .sess-bar button:disabled { cursor:default; }
  .bubble { max-width:88%; }
}
</style></head>
<body>
<aside class="sidebar">
  <div class="brand"><div class="logo">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
  </div><span>agentsearch</span></div>
  <nav class="nav" id="nav">
    <a data-view="dashboard" class="active">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg><span>仪表盘</span></a>
    <a data-view="devices">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/></svg><span>设备管理</span></a>
    <a data-view="search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg><span>会话搜索</span></a>
    <a data-view="logs">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16M4 10h16M4 15h10M4 20h7"/></svg><span>日志查看</span></a>
  </nav>
  <div class="sidebar-foot">v0.1.0 · 127.0.0.1 本地服务</div>
</aside>

<div class="main">
  <div class="header">
    <h1 id="pageTitle">仪表盘</h1>
    <span class="spacer"></span>
    <button class="iconbtn" onclick="pingAll(true)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><path d="M12 20h.01"/></svg>
      测试连接</button>
    <button class="iconbtn" onclick="refresh(true)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>
      刷新</button>
    <button class="iconbtn" onclick="toggleTheme()" title="切换主题">
      <svg id="themeIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
    </button>
  </div>

  <!-- dashboard -->
  <section class="view active" id="view-dashboard">
    <div class="card">
      <div class="banner">
        <span class="dot" id="svcDot"></span>
        <div><div class="k">本地服务</div><div class="v" id="svcState">运行中</div></div>
        <div><div class="k">索引库</div><div class="v mono" id="svcDb" style="font-size:12px">—</div></div>
        <div><div class="k">上次同步</div><div class="v" id="svcSync">—</div></div>
        <span class="spacer" style="flex:1"></span>
        <button onclick="syncLocal()">增量同步</button>
      </div>
    </div>

    <div class="grid g4" id="statRow" style="margin-bottom:18px"></div>

    <div class="card">
      <h2>设备健康状态<span class="spacer"></span>
        <button onclick="go('devices')">管理设备 →</button></h2>
      <div class="grid g3" id="healthGrid"></div>
    </div>
  </section>

  <!-- devices -->
  <section class="view" id="view-devices">
    <div class="card">
      <h2>添加远程设备</h2>
      <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center">
        <input type="text" id="addName" placeholder="名称，如 devbox-109" style="min-width:170px">
        <input type="text" id="addHost" placeholder="SSH host（默认同名称）" style="min-width:170px">
        <input type="text" id="addBin" placeholder="远端启动器路径（可选）" style="min-width:240px; flex:1">
        <button class="primary" id="addBtn" onclick="addRemote()">rsync 安装并建索引</button>
      </div>
      <p class="muted" style="margin:10px 0 0; font-size:12px">要求：免密 SSH、远端 Python 3.7+ 且 SQLite 支持 FTS5。设备上的会话正文不会被复制到本机。</p>
    </div>
    <div class="card">
      <h2>已注册设备<span class="spacer"></span>
        <button onclick="pingAll(true)">全部测试</button>
        <button onclick="syncAll()">全部同步</button>
        <button onclick="updateAll()">全部更新代码</button></h2>
      <div class="grid g3" id="devGrid"></div>
    </div>
  </section>

  <!-- search -->
  <section class="view" id="view-search">
    <div class="card">
      <h2>会话搜索</h2>
      <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center">
        <input type="text" id="q" placeholder="查询词，如：实验 重开" style="min-width:260px; flex:1">
        <input type="number" id="lim" value="20" min="1" max="50" style="width:74px" title="每设备取数">
        <label class="chk"><input type="checkbox" id="allHosts" checked onchange="renderHostPickers()"> 全部设备</label>
        <span id="hostPickers" style="display:flex;gap:12px"></span>
        <button class="primary" onclick="runSearch(this)">查询</button>
      </div>
      <div id="chips" style="margin-top:14px"></div>
    </div>
    <div class="card">
      <h2>搜索结果 <span class="spacer"></span><span id="hitCount" style="font-weight:400;font-size:12px"></span></h2>
      <div id="searchOut"><div class="empty">输入查询词并查询；结果支持点击，可直接打开该条会话的完整全文。</div></div>
    </div>
  </section>

  <!-- session transcript -->
  <section class="view" id="view-session">
    <div class="sess-bar">
      <button class="iconbtn" onclick="closeSession()" title="返回搜索结果">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>
        返回</button>
      <div class="st">
        <div class="t1 mono" id="sessTitle">—</div>
        <div class="t2 mono" id="sessSub"></div>
      </div>
      <span style="flex:1"></span>
      <span id="sessMeta"></span>
      <button class="mini" id="sessPrev" onclick="gotoMatch(-1)" title="上一处匹配">↑</button>
      <button class="mini" id="sessNext" onclick="gotoMatch(1)" title="下一处匹配">↓</button>
      <button id="sessRaw" onclick="toggleRaw()">读取全文</button>
      <button onclick="copySessionPath()">复制路径</button>
    </div>
    <div id="sessBody"><div class="empty">从搜索结果点击一条记录打开会话。</div></div>
  </section>

  <!-- logs -->
  <section class="view" id="view-logs">
    <div class="card">
      <h2>操作日志<span class="spacer"></span><button onclick="clearLogs()">清空</button></h2>
      <div id="logbox"></div>
    </div>
  </section>
</div>

<div id="toasts"></div>
<div id="modal"><div class="modal-box">
  <h3 id="modalTitle"></h3><p id="modalBody"></p>
  <div class="modal-acts"><button onclick="closeModal()">取消</button><button class="danger" id="modalOk">确认</button></div>
</div></div>

<script>
const TOKEN = document.querySelector('meta[name=token]').content;
let STATUS = null;
const PING = {};   // name -> {ok, ms|error}
const RSTAT = {};  // name -> remote status payload

/* ---------- theme & nav ---------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('agentsearch-theme', t); } catch (e) {}
}
function toggleTheme() { applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'); }
try { const t = localStorage.getItem('agentsearch-theme'); if (t) applyTheme(t); } catch (e) {}
const TITLES = { dashboard:'仪表盘', devices:'设备管理', search:'会话搜索', logs:'日志查看', session:'会话全文' };
function go(view) {
  document.querySelectorAll('.nav a').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  document.getElementById('pageTitle').textContent = TITLES[view] || '';
  history.replaceState(null, '', '#' + view);
}
document.querySelectorAll('.nav a').forEach(a => a.onclick = () => go(a.dataset.view));

/* ---------- primitives ---------- */
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtTs(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '从未同步'; }
function totals(sources) { let f=0,m=0; for (const k in sources){ f+=sources[k].files; m+=sources[k].messages; }
  return { files:f, msgs:m, text:f+' sessions · '+m.toLocaleString()+' messages' }; }
function busy(btn, html) { if (!btn) return; btn.disabled = true; btn._html = btn.innerHTML; btn.innerHTML = html || '<span class="spin"></span> 执行中'; }
function idle(btn) { if (!btn) return; btn.disabled = false; btn.innerHTML = btn._html || btn.innerHTML; }
async function api(path, body) {
  const opt = body === undefined
    ? { headers:{'X-Dashboard-Token':TOKEN} }
    : { method:'POST', headers:{'X-Dashboard-Token':TOKEN,'Content-Type':'application/json'}, body:JSON.stringify(body) };
  const r = await fetch(path, opt); return r.json();
}

/* ---------- toasts & modal & logs ---------- */
function toast(msg, type, sub) {
  const el = document.createElement('div');
  el.className = 'toast ' + (type || '');
  el.innerHTML = '<div class="tmsg">' + esc(msg) + (sub ? '<small>' + esc(sub) + '</small>' : '') + '</div>';
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 4200);
  addLog(msg + (sub ? '  ' + sub : ''), type);
}
function addLog(msg, cls) {
  const box = document.getElementById('logbox');
  const line = document.createElement('div');
  line.className = 'logline ' + (cls === 'err' ? 'err' : cls === 'ok' ? 'ok' : '');
  line.innerHTML = '<span class="lt">' + new Date().toLocaleTimeString() + '</span>' + esc(msg);
  box.appendChild(line); box.scrollTop = box.scrollHeight;
}
function clearLogs() { document.getElementById('logbox').innerHTML = ''; }
function confirmDlg(title, body, onOk, danger) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').textContent = body;
  const ok = document.getElementById('modalOk');
  ok.className = danger === false ? 'primary' : 'danger';
  ok.textContent = '确认';
  ok.onclick = () => { closeModal(); onOk(); };
  document.getElementById('modal').classList.add('show');
}
function closeModal() { document.getElementById('modal').classList.remove('show'); }
document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

/* ---------- status ---------- */
async function refresh(silent) {
  STATUS = await api('/api/status');
  if (STATUS.error) { if (!silent) toast(STATUS.error, 'err'); return; }
  document.getElementById('svcDb').textContent = STATUS.db;
  document.getElementById('svcSync').textContent = fmtTs(STATUS.last_sync);
  const t = totals(STATUS.sources);
  const online = STATUS.remotes.filter(r => PING[r.name] && PING[r.name].ok).length;
  document.getElementById('statRow').innerHTML = [
    [iconDb(), '索引会话', t.files, '本机 ' + Object.keys(STATUS.sources).length + ' 个 agent'],
    [iconMsg(), '索引消息', t.msgs.toLocaleString(), '跨三源统一索引'],
    [iconDev(), '远程设备', STATUS.remotes.length, online + ' 台在线 / 共 ' + STATUS.remotes.length + ' 台'],
    [iconClock(), '上次同步', STATUS.last_sync ? new Date(STATUS.last_sync*1000).toLocaleTimeString() : '—',
       STATUS.last_sync ? new Date(STATUS.last_sync*1000).toLocaleDateString() : '请先同步'],
  ].map(([ic,label,num,sub]) =>
    '<div class="stat"><div class="top"><span class="ico">' + ic + '</span>' + label + '</div>' +
    '<div class="num">' + num + '</div><div class="sub">' + esc(sub) + '</div></div>').join('');
  renderHealth(); renderDevices(); renderHostPickers();
}
function healthIcon(state) {
  if (state === 'ok') return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  if (state === 'bad') return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 8h.01M12 12v4"/></svg>';
}
function srcLine(sources) {
  return Object.entries(sources).map(([k,v]) =>
    '<span class="pill idle" style="margin:1px 4px 1px 0">' + esc(k) + ' ' + v.files + '/' + v.messages.toLocaleString() + '</span>').join('');
}
function renderHealth() {
  const cards = ['<div class="dev"><div class="head"><span class="name">local（本机）</span>' +
    '<span class="health ok" style="margin-left:auto">' + healthIcon('ok') + '</span></div>' +
    '<div class="meta"><div>' + srcLine(STATUS.sources) + '</div><div>last sync: <b>' + fmtTs(STATUS.last_sync) + '</b></div></div>' +
    '<div class="acts"><button onclick="syncLocal()">同步</button></div></div>'];
  cards.push(...STATUS.remotes.map(r => {
    const p = PING[r.name]; const s = RSTAT[r.name];
    const state = !p ? 'unknown' : p.ok ? 'ok' : 'bad';
    return '<div class="dev"><div class="head"><span class="name">' + esc(r.name) + '</span>' +
      '<span class="health ' + state + '">' + healthIcon(state) + '</span></div>' +
      '<div class="meta"><div>' + esc(r.host) + (p ? (p.ok ? ' · <b style="color:var(--ok)">' + p.ms + ' ms</b>' : ' · <b style="color:var(--err)">不可达</b>') : ' · 未测试') + '</div>' +
      (s ? '<div>' + srcLine(s.sources) + '</div><div>last sync: <b>' + fmtTs(s.last_sync) + '</b></div>' : '') + '</div>' +
      '<div class="acts"><button onclick="pingOne(\'' + esc(r.name) + '\')">测试</button>' +
      '<button onclick="remoteStatus(\'' + esc(r.name) + '\')">索引状态</button>' +
      '<button onclick="syncOne(\'' + esc(r.name) + '\')">同步</button></div></div>';
  }));
  document.getElementById('healthGrid').innerHTML = cards.join('');
  if (!STATUS.remotes.length) document.getElementById('healthGrid').innerHTML +=
    '<div class="empty" style="grid-column:1/-1">还没有远程设备，到「设备管理」添加。</div>';
}

/* ---------- devices page ---------- */
function renderDevices() {
  if (!STATUS) return;
  const box = document.getElementById('devGrid');
  if (!STATUS.remotes.length) { box.innerHTML = '<div class="empty" style="grid-column:1/-1">尚未注册设备</div>'; return; }
  box.innerHTML = STATUS.remotes.map(r => {
    const p = PING[r.name]; const s = RSTAT[r.name];
    const state = !p ? 'unknown' : p.ok ? 'ok' : 'bad';
    return '<div class="dev"><div class="head"><span class="name">' + esc(r.name) + '</span>' +
      '<span class="health ' + state + '">' + healthIcon(state) + '</span></div>' +
      '<div class="meta"><div>host: <b>' + esc(r.host) + '</b>' +
      (p ? (p.ok ? ' · ' + p.ms + ' ms' : ' · <b style="color:var(--err)">不可达</b>') : '') + '</div>' +
      '<div>bin: <b>' + esc(r.bin) + '</b></div>' +
      (s ? '<div>' + srcLine(s.sources) + '</div><div>last sync: <b>' + fmtTs(s.last_sync) + '</b> (' + s.ms + ' ms)</div>' : '') +
      '</div><div class="acts">' +
      '<button onclick="pingOne(\'' + esc(r.name) + '\')">测试</button>' +
      '<button onclick="remoteStatus(\'' + esc(r.name) + '\')">索引状态</button>' +
      '<button onclick="syncOne(\'' + esc(r.name) + '\')">同步</button>' +
      '<button onclick="updateOne(\'' + esc(r.name) + '\')">更新代码</button>' +
      '<button class="danger" onclick="removeOne(\'' + esc(r.name) + '\')">移除</button>' +
      '</div></div>';
  }).join('');
}
async function addRemote() {
  const name = document.getElementById('addName').value.trim();
  const host = document.getElementById('addHost').value.trim() || name;
  const bin = document.getElementById('addBin').value.trim();
  if (!name) { toast('需要填写设备名称', 'err'); return; }
  const btn = document.getElementById('addBtn'); busy(btn, '<span class="spin"></span> 安装中');
  toast('正在连接 ' + host + ' 并安装，通常需要几十秒…');
  const r = await api('/api/remotes/add', { name, host, bin });
  idle(btn);
  if (r.ok) {
    toast('已添加设备 ' + name, 'ok', (r.logs || []).join(' / '));
    document.getElementById('addName').value = ''; document.getElementById('addHost').value = ''; document.getElementById('addBin').value = '';
    await refresh(true); pingOne(name);
  } else toast('添加失败：' + r.error, 'err');
}
function removeOne(name) {
  confirmDlg('移除设备 ' + name, '只删除本机的连接配置，不会删除设备上的任何文件。', async () => {
    const r = await api('/api/remotes/remove', { name });
    if (r.ok) { toast('已移除 ' + name, 'ok'); delete PING[name]; delete RSTAT[name]; refresh(true); }
    else toast(r.error, 'err');
  });
}
function updateOne(name) {
  confirmDlg('更新 ' + name + ' 的代码', '将重新 rsync 代码并在该设备上增量建索引。', () => doUpdate({ name }, name), false);
}
function updateAll() {
  if (!STATUS.remotes.length) { toast('没有已注册设备', 'err'); return; }
  confirmDlg('更新全部设备', '将向 ' + STATUS.remotes.length + ' 台设备重新 rsync 代码并增量建索引。', () => doUpdate({}, '全部设备'), false);
}
async function doUpdate(body, label) {
  toast('开始更新 ' + label + '…');
  const r = await api('/api/remotes/update', body);
  if (r.ok) toast('更新完成：' + label, 'ok', (r.logs || []).join(' / '));
  else toast('更新失败：' + r.error, 'err');
}

/* ---------- actions ---------- */
async function pingAll(silent) {
  const r = await api('/api/ping', {});
  for (const x of r.results) {
    PING[x.name] = x;
    if (x.ok) { if (!silent) toast(x.name + ' 连接正常', 'ok', x.ms + ' ms'); }
    else if (!silent) toast(x.name + ' 不可达', 'err', x.error);
  }
  addLog('连接测试：' + r.results.map(x => x.name + (x.ok ? ' ✓' : ' ✗')).join('，'),
         r.results.every(x => x.ok) ? 'ok' : 'err');
  renderHealth(); renderDevices();
}
async function pingOne(name) {
  const r = (await api('/api/ping', { name })).results[0];
  PING[name] = r;
  if (r.ok) toast(name + ' 连接正常', 'ok', r.ms + ' ms'); else toast(name + ' 不可达', 'err', r.error);
  renderHealth(); renderDevices();
}
async function remoteStatus(name) {
  const r = await api('/api/remote-status?name=' + encodeURIComponent(name));
  if (r.ok) { RSTAT[name] = r.status; toast(name + ' 索引状态', 'ok', totals(r.status.sources).text); }
  else { toast(name + ' 状态获取失败', 'err', r.error); PING[name] = { ok:false, error:r.error }; }
  renderHealth(); renderDevices();
}
async function syncLocal() {
  toast('本机增量同步中…');
  const t0 = Date.now();
  const r = await api('/api/sync', {});
  if (r.ok) { toast('本机同步完成', 'ok', '+' + r.stats.files_new + ' 新 / ' + r.stats.messages + ' 消息 / ' + r.ms + ' ms'); refresh(true); }
  else toast(r.error, 'err');
}
async function syncOne(name) {
  toast(name + ' 增量同步中…');
  const r = await api('/api/sync', { name });
  if (r.ok) { toast(name + ' 同步完成', 'ok', r.output + ' / ' + r.ms + ' ms'); remoteStatus(name); }
  else toast(name + ' 同步失败', 'err', r.error);
}
function syncAll() {
  if (!STATUS.remotes.length) { syncLocal(); return; }
  syncLocal(); STATUS.remotes.forEach(r => syncOne(r.name));
}

/* ---------- search diagnose ---------- */
function renderHostPickers() {
  if (!STATUS) return;
  const all = document.getElementById('allHosts').checked;
  const names = ['local'].concat(STATUS.remotes.map(r => r.name));
  document.getElementById('hostPickers').innerHTML = all ? '' :
    names.map(n => '<label class="chk"><input type="checkbox" class="hpick" value="' + esc(n) + '" checked> ' + esc(n) + '</label>').join('');
}
function pickedHosts() {
  if (document.getElementById('allHosts').checked) return null;
  return [...document.querySelectorAll('.hpick:checked')].map(e => e.value);
}
const ROLE_LABEL = { user:'用户', assistant:'助手', tool:'工具', system:'系统' };
const KIND_LABEL = { text:'对话', summary:'摘要', tool_call:'工具调用', tool_result:'工具结果', reasoning:'思考' };
let LAST_TERMS = [];
let LAST_HITS = [];

async function runSearch(btn) {
  const query = document.getElementById('q').value.trim();
  if (!query) return;
  const limit = parseInt(document.getElementById('lim').value, 10) || 20;
  if (btn) busy(btn);
  const r = await api('/api/search', { query, limit, hosts: pickedHosts() });
  if (btn) idle(btn);
  if (r.error) { toast(r.error, 'err'); return; }
  document.getElementById('chips').innerHTML = r.per_host.map(h =>
    '<span class="pill ' + (h.ok ? 'ok' : 'bad') + '" style="margin:2px 6px 2px 0">' +
    esc(h.host) + ' · ' + (h.ok ? h.ms + ' ms · ' + h.hits + ' 命中' : '不可达') + '</span>').join('') +
    (r.warnings.length ? '<div class="muted" style="margin-top:6px; font-size:12px">' + r.warnings.map(esc).join('<br>') + '</div>' : '');
  LAST_TERMS = query.split(/\s+/).filter(Boolean);
  LAST_HITS = r.merged;
  document.getElementById('hitCount').textContent = r.merged.length ? r.merged.length + ' 条 · 点击查看会话全文' : '';
  if (!r.merged.length) { document.getElementById('searchOut').innerHTML = '<div class="empty">没有匹配结果。</div>'; return; }
  document.getElementById('searchOut').innerHTML =
    '<div class="hitlist">' + r.merged.map((h, i) => {
      const snip = esc((h.snippet || '').replace(/\s+/g, ' '))
        .replace(/\[\[([\s\S]*?)\]\]/g, '<mark>$1</mark>');
      return '<div class="hit" data-i="' + i + '" role="button" tabindex="0">' +
        '<div class="hit-rank">' + (i + 1) + '</div>' +
        '<div class="hit-ico ' + esc(h.source) + '">' + srcIcon(h.source) + '</div>' +
        '<div class="hit-main"><div class="hit-meta">' +
          '<span class="hosttag ' + (h.host === 'local' ? 'local' : '') + '">' + esc(h.host) + '</span>' +
          '<span class="srcbadge">' + esc(h.source) + '</span>' +
          '<span>' + esc(ROLE_LABEL[h.role] || h.role || '') + ' · ' + esc(KIND_LABEL[h.kind] || h.kind || '') + '</span>' +
          '<span class="muted">' + esc((h.ts || '').slice(0, 16).replace('T', ' ')) + '</span>' +
        '</div>' +
        '<div class="hit-snip">' + snip + '</div>' +
        '<div class="hit-path mono">' + esc(h.path.split('/').pop()) + ' : ' + h.lineno + '</div></div>' +
        '<div class="hit-go">' + iconChevron() + '</div></div>';
    }).join('') + '</div>';
  addLog('搜索「' + query + '」：' + r.per_host.map(h => h.host + ' ' + (h.ok ? h.ms + 'ms' : '✗')).join('，'));
}
document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
document.getElementById('searchOut').addEventListener('click', e => {
  const el = e.target.closest('.hit');
  if (el && LAST_HITS[+el.dataset.i]) {
    const h = LAST_HITS[+el.dataset.i];
    openSession(h.path, h.host, h.lineno, LAST_TERMS);
  }
});
document.getElementById('searchOut').addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const el = e.target.closest('.hit');
  if (el && LAST_HITS[+el.dataset.i]) {
    e.preventDefault();
    const h = LAST_HITS[+el.dataset.i];
    openSession(h.path, h.host, h.lineno, LAST_TERMS);
  }
});

/* ---------- session transcript ---------- */
let SESS = null;
const EXPANDED = new Set();
const DISC_OPEN = new Set();

function discToggle(sum) {
  setTimeout(() => {
    const d = sum.closest('details.disc');
    const i = +d.closest('.msg').dataset.i;
    if (d.open) DISC_OPEN.add(i); else DISC_OPEN.delete(i);
  }, 0);
}

function showSession() {
  document.querySelectorAll('.nav a').forEach(a => a.classList.remove('active'));
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-session').classList.add('active');
  document.getElementById('pageTitle').textContent = TITLES.session;
}
function closeSession() {
  SESS = null;
  history.pushState(null, '', '#search');
  go('search');
}
window.addEventListener('popstate', () => {
  if (location.hash === '#search') { SESS = null; go('search'); }
  else if (location.hash.startsWith('#session')) openSessionFromHash(true);
});
function openSessionFromHash(replace) {
  const p = new URLSearchParams(location.hash.slice(9));
  const path = p.get('path');
  if (!path) { go('search'); return; }
  const terms = p.get('q') ? p.get('q').split(/\s+/).filter(Boolean) : [];
  openSession(path, p.get('host') || 'local', parseInt(p.get('line'), 10) || 0, terms, replace);
}
async function openSession(path, host, lineno, terms, replace) {
  host = host || 'local';
  terms = terms || [];
  showSession();
  EXPANDED.clear();
  DISC_OPEN.clear();
  SESS = null;
  document.getElementById('sessTitle').textContent = path.split('/').pop();
  document.getElementById('sessSub').textContent = host + ' · ' + path;
  document.getElementById('sessMeta').textContent = '加载中…';
  document.getElementById('sessBody').innerHTML =
    '<div class="empty"><span class="spin" style="display:inline-block;width:18px;height:18px;border-width:2.5px"></span><br>正在读取会话…</div>';
  const q = new URLSearchParams({ path, host, line: String(lineno || '') });
  if (terms.length) q.set('q', terms.join(' '));
  if (replace) history.replaceState(null, '', '#session?' + q.toString());
  else history.pushState(null, '', '#session?' + q.toString());
  const r = await api('/api/session', { path, host, raw: false });
  if (!r.ok) {
    document.getElementById('sessBody').innerHTML = '<div class="empty">加载失败：' + esc(r.error) + '</div>';
    toast('会话加载失败：' + r.error, 'err');
    return;
  }
  initSession(r.session, host, lineno, terms, false);
}
function initSession(data, host, anchor, terms, raw) {
  EXPANDED.clear();
  DISC_OPEN.clear();
  SESS = { data, host, anchor, terms, raw, lo: 0, hi: data.count, matched: [], cur: -1 };
  const idx = Math.max(0, data.messages.findIndex(m => m.lineno === anchor));
  if (data.count > 240) {
    SESS.lo = Math.max(0, idx - 90);
    SESS.hi = Math.min(data.count, idx + 91);
  }
  const tt = terms.map(t => t.toLowerCase());
  if (tt.length) data.messages.forEach((m, i) => {
    const txt = (m.text || '').toLowerCase();
    if (tt.every(t => txt.includes(t))) SESS.matched.push(i);
  });
  SESS.cur = SESS.matched.find(i => i >= idx);
  if (SESS.cur === undefined) SESS.cur = SESS.matched[0] === undefined ? -1 : SESS.matched[0];
  renderSession('anchor');
}
function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function renderBody(text, terms) {
  let h = esc(text == null ? '' : text);
  (terms || []).forEach(t => {
    if (!t) return;
    h = h.replace(new RegExp('(' + escRe(esc(t)) + ')', 'gi'), '<mark>$1</mark>');
  });
  h = h.replace(/```[^\n]*\n?([\s\S]*?)(?:```|$)/g,
    (m, code) => '<pre class="codeblock">' + code.replace(/\n$/, '') + '</pre>');
  h = h.replace(/(^|[\s(\[])`([^`\n]{1,200})`(?=$|[\s).,:;!?\]])/g, '$1<code>$2</code>');
  return h;
}
function fmtMsgTs(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return isNaN(d) ? String(ts).slice(11, 19) : d.toLocaleTimeString();
}
function chevIcon() {
  return '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
}
function oneLine(text, n) {
  const line = String(text || '').split('\n').map(s => s.trim()).find(Boolean) || '';
  return line.length > n ? line.slice(0, n) + '…' : line;
}
function metaHtml(m, hit) {
  return '<span class="meta">' + fmtMsgTs(m.ts) + ' · L' + m.lineno +
    (hit ? '<span class="hittag">命中</span>' : '') + '</span>';
}
function renderMsg(m, i, hit, terms) {
  const text = m.text || '';
  const long = text.length > 1200 || text.split('\n').length > 24;
  const clamp = long && !EXPANDED.has(i);
  const expandBtn = clamp
    ? '<button class="mexpand" onclick="expandMsg(' + i + ')">展开全部 ↓</button>' : '';
  if (m.kind === 'text' && m.role === 'user') {
    return '<div class="msg user' + (hit ? ' mhit' : '') + '" data-i="' + i + '">' +
      '<div class="bubble"><div class="mbody' + (clamp ? ' clamp' : '') + '">' +
      renderBody(text, terms) + '</div></div>' + expandBtn + metaHtml(m, hit) + '</div>';
  }
  if ((m.kind === 'text' || m.kind == null) && (m.role === 'assistant' || m.role == null)) {
    return '<div class="msg assistant' + (hit ? ' mhit' : '') + '" data-i="' + i + '">' +
      '<div class="mbody' + (clamp ? ' clamp' : '') + '">' + renderBody(text, terms) + '</div>' +
      expandBtn +
      '<div class="ameta">' + fmtMsgTs(m.ts) + ' · L' + m.lineno +
      (hit ? '<span class="hittag">命中</span>' : '') + '</div></div>';
  }
  let discClass = 'disc', title = KIND_LABEL[m.kind] || m.kind || '内容';
  if (m.kind === 'reasoning') discClass += ' think';
  else if (m.kind === 'tool_call' || m.kind === 'tool_result') discClass += ' tool';
  else if (m.kind === 'summary') discClass += ' summarydisc';
  else return '<div class="msg other' + (hit ? ' mhit' : '') + '" data-i="' + i + '">' +
      '<div class="ameta">' + esc(ROLE_LABEL[m.role] || m.role || '') + ' · ' +
      esc(KIND_LABEL[m.kind] || m.kind || '') + ' · ' + fmtMsgTs(m.ts) + ' · L' + m.lineno +
      (hit ? '<span class="hittag">命中</span>' : '') + '</div>' +
      '<div class="mbody">' + renderBody(text, terms) + '</div></div>';
  return '<div class="msg' + (hit ? ' mhit' : '') + '" data-i="' + i + '">' +
    '<details class="' + discClass + '"' + (hit || DISC_OPEN.has(i) ? ' open' : '') + '>' +
    '<summary onclick="discToggle(this)">' + chevIcon() + '<span class="dt">' + esc(title) + '</span>' +
    '<span class="ddot"></span><span class="dsum">' + esc(oneLine(text, 140)) + '</span>' +
    (hit ? '<span class="hittag">命中 L' + m.lineno + '</span>' : '<span class="dln muted">L' + m.lineno + '</span>') +
    '</summary>' +
    '<div class="dbody' + (clamp ? ' clamp' : '') + '">' + renderBody(text, terms) + '</div>' +
    expandBtn + '</details></div>';
}
function renderSession(mode) {
  if (!SESS) return;
  const s = SESS.data;
  const prevH = document.body.scrollHeight;
  const rawBtn = document.getElementById('sessRaw');
  rawBtn.textContent = SESS.raw ? '返回索引版' : '读取全文';
  rawBtn.classList.toggle('primary', !SESS.raw);
  document.getElementById('sessMeta').innerHTML =
    '<span class="hosttag ' + (SESS.host === 'local' ? 'local' : '') + '">' + esc(SESS.host) + '</span>' +
    '<span class="srcbadge">' + esc(s.source || '') + '</span>' +
    '<span>' + s.count + ' 条消息 · ' +
    esc((s.started_at || '').slice(0, 16).replace('T', ' ')) + ' → ' + esc((s.ended_at || '').slice(11, 19)) + '</span>' +
    (SESS.raw ? '<span class="pill warn">原始全文</span>' : '<span class="pill idle">索引版 · 单条≤20k</span>') +
    (s.cwd ? '<span class="muted mono">' + esc(s.cwd) + '</span>' : '');
  const rows = [];
  if (SESS.lo > 0)
    rows.push('<div class="loadbar"><button onclick="expandOlder()">↑ 加载更早 ' + Math.min(120, SESS.lo) + ' 条</button></div>');
  s.messages.slice(SESS.lo, SESS.hi).forEach((m, off) => {
    const i = SESS.lo + off;
    const day = (m.ts || '').slice(0, 10);
    const prevDay = i > 0 ? (s.messages[i - 1].ts || '').slice(0, 10) : '';
    if (day && day !== prevDay) { rows.push('<div class="daysep">' + esc(day) + '</div>'); }
    rows.push(renderMsg(m, i, m.lineno === SESS.anchor, SESS.terms));
  });
  if (SESS.hi < s.count)
    rows.push('<div class="loadbar"><button onclick="expandNewer()">加载更晚 ' + Math.min(120, s.count - SESS.hi) + ' 条 ↓</button></div>');
  document.getElementById('sessBody').innerHTML = '<div class="trans">' + rows.join('') + '</div>';
  const n = SESS.matched.length;
  const pos = n ? SESS.matched.indexOf(SESS.cur) + 1 : 0;
  const pb = document.getElementById('sessPrev'), nb = document.getElementById('sessNext');
  pb.disabled = nb.disabled = n === 0;
  pb.textContent = n ? '↑ ' + pos + '/' + n : '↑';
  nb.textContent = '↓';
  requestAnimationFrame(() => {
    if (mode === 'anchor' || mode === 'match') {
      const sel = mode === 'match' ? '.msg[data-i="' + SESS.cur + '"]' : '.msg.mhit';
      const el = document.querySelector(sel) || document.querySelector('.trans');
      if (mode === 'match') { const d = el && el.querySelector('details.disc'); if (d) d.open = true; }
      if (el) el.scrollIntoView({ block: mode === 'anchor' ? 'start' : 'center' });
      if (mode === 'anchor') window.scrollBy(0, -76);
    } else if (mode === 'older') {
      window.scrollBy(0, document.body.scrollHeight - prevH);
    }
  });
}
function expandOlder() { SESS.lo = Math.max(0, SESS.lo - 120); renderSession('older'); }
function expandNewer() { SESS.hi = Math.min(SESS.data.count, SESS.hi + 120); renderSession('stay'); }
function expandMsg(i) { EXPANDED.add(i); renderSession('stay'); }
function gotoMatch(dir) {
  if (!SESS || !SESS.matched.length) return;
  let pos = SESS.matched.indexOf(SESS.cur);
  if (pos < 0) pos = 0; else pos = (pos + dir + SESS.matched.length) % SESS.matched.length;
  SESS.cur = SESS.matched[pos];
  DISC_OPEN.add(SESS.cur);
  if (SESS.cur < SESS.lo) SESS.lo = Math.max(0, SESS.cur - 90);
  if (SESS.cur >= SESS.hi) SESS.hi = Math.min(SESS.data.count, SESS.cur + 91);
  renderSession('match');
}
async function toggleRaw() {
  if (!SESS) return;
  const next = !SESS.raw;
  if (next) toast('从原始 JSONL 读取未截断全文，大会话可能较慢…');
  const r = await api('/api/session', { path: SESS.data.path, host: SESS.host, raw: next });
  if (!r.ok) { toast('读取失败：' + r.error, 'err'); return; }
  initSession(r.session, SESS.host, SESS.anchor, SESS.terms, next);
}
function copySessionPath() {
  if (!SESS) return;
  navigator.clipboard.writeText(SESS.data.path).then(
    () => toast('已复制会话路径', 'ok'),
    () => toast('复制失败', 'err'));
}

/* ---------- icons ---------- */
function iconDb() { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>'; }
function iconMsg() { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'; }
function iconDev() { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="7" rx="2"/><rect x="2" y="13" width="20" height="7" rx="2"/><path d="M6 7.5h.01M6 16.5h.01"/></svg>'; }
function iconClock() { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>'; }
function srcIcon(s) {
  if (s === 'claude')
    return '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.5c.5 3.9 2.1 6.1 5.2 7.1-3.1 1-4.7 3.2-5.2 7.1-.5-3.9-2.1-6.1-5.2-7.1C9.9 8.6 11.5 6.4 12 2.5Z"/><path d="M18.5 14.5c.3 2 1.1 3.1 2.7 3.6-1.6.5-2.4 1.6-2.7 3.6-.3-2-1.1-3.1-2.7-3.6 1.6-.5 2.4-1.6 2.7-3.6Z" opacity=".85"/></svg>';
  if (s === 'codex')
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 8-4 4 4 4M16 8l4 4-4 4M13 5l-2 14"/></svg>';
  return '<span style="font-family:Georgia,serif;font-size:16px;font-weight:700;line-height:1">π</span>';
}
function iconChevron() {
  return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg>';
}

refresh(true);
if (location.hash === '#devices' || location.hash === '#search' || location.hash === '#logs') go(location.hash.slice(1));
else if (location.hash.startsWith('#session')) openSessionFromHash();
</script>
</body></html>
"""
