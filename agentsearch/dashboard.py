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
from .search import DEFAULT_KINDS
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
    per_host, warnings = [], []

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
        payload = []
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
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="token" content="__TOKEN__">
<title>agentsearch 管理面板</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#16181d; color:#e6e8ee; font:13px/1.5 -apple-system,"PingFang SC",sans-serif; }
header { padding:16px 24px; background:#1d2027; border-bottom:1px solid #2c303a; display:flex; align-items:center; gap:12px; }
h1 { font-size:15px; margin:0; font-weight:600; }
h2 { font-size:13px; margin:0 0 10px; color:#9aa3b2; text-transform:uppercase; letter-spacing:.05em; }
main { padding:20px 24px; max-width:1100px; }
section { background:#1d2027; border:1px solid #2c303a; border-radius:8px; padding:16px; margin-bottom:16px; }
.row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(330px,1fr)); gap:12px; }
.card { background:#20242d; border:1px solid #2f343f; border-radius:6px; padding:12px 14px; }
.card.local { border-color:#3d5a45; }
.name { font-weight:600; font-size:14px; }
.badge { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px; margin-left:6px; }
.badge.ok { background:#1e3a29; color:#5fd08a; }
.badge.bad { background:#3d2226; color:#ff8089; }
.badge.muted { background:#2c303a; color:#9aa3b2; }
.kv { color:#9aa3b2; margin:6px 0; font-family:ui-monospace,Menlo,monospace; font-size:12px; word-break:break-all; }
.kv b { color:#cfd6e2; font-weight:500; }
button { background:#2b3140; color:#e6e8ee; border:1px solid #3d4452; border-radius:5px; padding:5px 11px; cursor:pointer; font-size:12px; }
button:hover { background:#353c4d; }
button.danger { border-color:#5a3038; color:#ff8089; }
button:disabled { opacity:.5; cursor:default; }
input[type=text],input[type=number] { background:#16181d; border:1px solid #3d4452; color:#e6e8ee; border-radius:5px; padding:6px 9px; font-size:12px; }
input[type=text] { min-width:160px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { text-align:left; padding:5px 8px; border-bottom:1px solid #2c303a; vertical-align:top; }
th { color:#9aa3b2; font-weight:500; }
code { font-family:ui-monospace,Menlo,monospace; color:#b8c2d4; }
#log { background:#121419; border:1px solid #2c303a; border-radius:6px; padding:10px 12px; height:140px; overflow:auto;
       font-family:ui-monospace,Menlo,monospace; font-size:11.5px; white-space:pre-wrap; }
.log-err { color:#ff8089; } .log-ok { color:#5fd08a; }
.hostchip { display:inline-block; padding:2px 9px; border-radius:10px; margin:2px 4px 2px 0; font-size:11.5px; font-family:ui-monospace,monospace; }
.hostchip.ok { background:#1e3a29; color:#5fd08a; } .hostchip.bad { background:#3d2226; color:#ff8089; }
.muted { color:#7c8595; }
.snip { color:#b8c2d4; max-width:420px; }
a { color:#7ab8ff; }
</style></head>
<body>
<header><h1>agentsearch 管理面板</h1><span class="muted" id="hdr"></span>
  <span style="flex:1"></span>
  <button onclick="pingAll()">测试全部连接</button>
  <button onclick="syncAll()">全部增量同步</button>
</header>
<main>
<section>
  <h2>本机</h2>
  <div class="card local" id="localCard"><span class="muted">加载中…</span></div>
</section>

<section>
  <h2>远程设备</h2>
  <div class="grid" id="cards"><span class="muted">加载中…</span></div>
  <div class="row" style="margin-top:12px">
    <input type="text" id="addName" placeholder="名称 如 devbox-109">
    <input type="text" id="addHost" placeholder="SSH host（默认同名称）">
    <input type="text" id="addBin" placeholder="远端启动器路径（默认 ~/agentsearch/bin/agentsearch）" style="min-width:300px">
    <button onclick="addRemote()">添加（rsync 安装并建索引）</button>
  </div>
</section>

<section>
  <h2>搜索诊断（按设备耗时拆分）</h2>
  <div class="row">
    <input type="text" id="q" placeholder="查询词，如：实验 重开" style="min-width:260px">
    <input type="number" id="lim" value="20" min="1" max="50" style="width:70px" title="每设备取数">
    <label class="muted"><input type="checkbox" id="allHosts" checked onchange="renderHostPickers()"> 全部设备</label>
    <span id="hostPickers"></span>
    <button onclick="runSearch()">查询</button>
  </div>
  <div id="chips" style="margin:10px 0"></div>
  <div id="searchOut"></div>
</section>

<section>
  <h2>操作日志</h2>
  <div id="log"></div>
</section>
</main>

<script>
const TOKEN = document.querySelector('meta[name=token]').content;
let STATUS = null;

function log(msg, cls) {
  const el = document.getElementById('log');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = new Date().toLocaleTimeString() + '  ' + msg;
  el.appendChild(line); el.scrollTop = el.scrollHeight;
}
async function api(path, body) {
  const opt = body === undefined
    ? { headers: { 'X-Dashboard-Token': TOKEN } }
    : { method: 'POST', headers: { 'X-Dashboard-Token': TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  return r.json();
}
function esc(s) { return String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function fmtTs(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; }
function totals(sources) {
  let f = 0, m = 0;
  for (const k in sources) { f += sources[k].files; m += sources[k].messages; }
  return f + ' sessions / ' + m.toLocaleString() + ' messages';
}

async function refresh() {
  STATUS = await api('/api/status');
  if (STATUS.error) { log(STATUS.error, 'log-err'); return; }
  document.getElementById('hdr').textContent = STATUS.db;
  const src = Object.entries(STATUS.sources)
    .map(([k,v]) => k + ': ' + v.files + ' / ' + v.messages.toLocaleString()).join('　');
  document.getElementById('localCard').innerHTML =
    '<div class="name">local</div>' +
    '<div class="kv">last sync: <b>' + fmtTs(STATUS.last_sync) + '</b></div>' +
    '<div class="kv">' + esc(src) + '</div>' +
    '<div class="row" style="margin-top:8px"><button onclick="syncLocal()">增量同步</button>' +
    '<button onclick="remoteStatus(\'local\')">查看明细</button></div>';
  renderCards(); renderHostPickers();
}
function renderCards() {
  const box = document.getElementById('cards');
  if (!STATUS.remotes.length) { box.innerHTML = '<span class="muted">尚未注册设备</span>'; return; }
  box.innerHTML = STATUS.remotes.map(r =>
    '<div class="card" id="card-' + esc(r.name) + '">' +
    '<div class="name">' + esc(r.name) + '<span class="badge muted" id="badge-' + esc(r.name) + '">未测试</span></div>' +
    '<div class="kv">host: <b>' + esc(r.host) + '</b></div>' +
    '<div class="kv">bin: <b>' + esc(r.bin) + '</b></div>' +
    '<div class="kv" id="stat-' + esc(r.name) + '"></div>' +
    '<div class="row" style="margin-top:8px">' +
    '<button onclick="pingOne(\'' + esc(r.name) + '\')">测试</button>' +
    '<button onclick="remoteStatus(\'' + esc(r.name) + '\')">索引状态</button>' +
    '<button onclick="syncOne(\'' + esc(r.name) + '\')">同步</button>' +
    '<button onclick="updateOne(\'' + esc(r.name) + '\')">更新代码</button>' +
    '<button class="danger" onclick="removeOne(\'' + esc(r.name) + '\')">移除</button>' +
    '</div></div>').join('');
}
function renderHostPickers() {
  const all = document.getElementById('allHosts').checked;
  const names = ['local'].concat(STATUS.remotes.map(r => r.name));
  document.getElementById('hostPickers').innerHTML = all ? '' :
    names.map(n => '<label class="muted"><input type="checkbox" class="hpick" value="' + esc(n) + '" checked> ' + esc(n) + '</label>').join(' ');
}
function setBadge(name, state, text) {
  const b = document.getElementById('badge-' + name);
  b.className = 'badge ' + state; b.textContent = text;
}

async function pingAll() {
  log('测试全部连接…');
  const r = await api('/api/ping', {});
  for (const x of r.results) {
    if (!document.getElementById('badge-' + x.name)) continue;
    if (x.ok) { setBadge(x.name, 'ok', x.ms + ' ms'); log(x.name + ': ' + x.ms + ' ms', 'log-ok'); }
    else { setBadge(x.name, 'bad', '不可达'); log(x.name + ': ' + x.error, 'log-err'); }
  }
}
async function pingOne(name) {
  const r = (await api('/api/ping', { name })).results[0];
  if (r.ok) { setBadge(name, 'ok', r.ms + ' ms'); log(name + ': ' + r.ms + ' ms', 'log-ok'); }
  else { setBadge(name, 'bad', '不可达'); log(name + ': ' + r.error, 'log-err'); }
}
async function remoteStatus(name) {
  if (name === 'local') { log('local: ' + totals(STATUS.sources) + '，last sync ' + fmtTs(STATUS.last_sync)); return; }
  const el = document.getElementById('stat-' + name);
  if (el) el.textContent = '查询中…';
  const r = await api('/api/remote-status?name=' + encodeURIComponent(name));
  if (!r.ok) { if (el) el.textContent = ''; log(name + ' 状态失败: ' + r.error, 'log-err'); return; }
  const s = r.status;
  if (el) el.innerHTML = 'last sync: <b>' + fmtTs(s.last_sync) + '</b>　(' + r.ms + ' ms)<br>' + esc(totals(s.sources));
  log(name + ' 索引: ' + totals(s.sources), 'log-ok');
}
async function syncLocal() {
  log('local 增量同步…');
  const r = await api('/api/sync', {});
  if (r.ok) { log('local: +' + r.stats.files_new + ' 新 / ' + r.stats.messages + ' 消息 (' + r.ms + ' ms)', 'log-ok'); refresh(); }
  else log(r.error, 'log-err');
}
async function syncOne(name) {
  log(name + ' 增量同步…');
  const r = await api('/api/sync', { name });
  if (r.ok) { log(name + ': ' + r.output + ' (' + r.ms + ' ms)', 'log-ok'); remoteStatus(name); }
  else log(r.error, 'log-err');
}
function syncAll() { syncLocal(); STATUS.remotes.forEach(r => syncOne(r.name)); }
async function updateOne(name) {
  if (!confirm('重新 rsync 代码到 ' + name + ' 并增量建索引？')) return;
  log(name + ' 更新代码…');
  const r = await api('/api/remotes/update', { name });
  if (r.ok) { r.logs.forEach(l => log(l)); log(name + ' 更新完成', 'log-ok'); }
  else log(r.error, 'log-err');
}
async function removeOne(name) {
  if (!confirm('移除设备 ' + name + '？不会删除设备上的文件。')) return;
  const r = await api('/api/remotes/remove', { name });
  if (r.ok) { log('已移除 ' + name, 'log-ok'); refresh(); } else log(r.error, 'log-err');
}
async function addRemote() {
  const name = document.getElementById('addName').value.trim();
  const host = document.getElementById('addHost').value.trim() || name;
  const bin = document.getElementById('addBin').value.trim();
  if (!name) { log('需要填写名称', 'log-err'); return; }
  log('添加 ' + name + '（安装+建索引可能耗时几十秒）…');
  const r = await api('/api/remotes/add', { name, host, bin });
  if (r.ok) { r.logs.forEach(l => log(name + ': ' + l)); log('已添加 ' + name, 'log-ok'); refresh(); }
  else log(r.error, 'log-err');
}
function pickedHosts() {
  if (document.getElementById('allHosts').checked) return null;
  return [...document.querySelectorAll('.hpick:checked')].map(e => e.value);
}
async function runSearch() {
  const query = document.getElementById('q').value.trim();
  if (!query) return;
  const limit = parseInt(document.getElementById('lim').value, 10) || 20;
  const r = await api('/api/search', { query, limit, hosts: pickedHosts() });
  if (r.error) { log(r.error, 'log-err'); return; }
  document.getElementById('chips').innerHTML = r.per_host.map(h => h.ok
    ? '<span class="hostchip ok">' + esc(h.host) + ' · ' + h.ms + ' ms · ' + h.hits + ' 命中</span>'
    : '<span class="hostchip bad">' + esc(h.host) + ' · 不可达</span>').join('')
    + (r.warnings.length ? '<div class="muted" style="margin-top:4px">' + r.warnings.map(esc).join('<br>') + '</div>' : '');
  document.getElementById('searchOut').innerHTML =
    '<table><tr><th>#</th><th>设备</th><th>来源</th><th>时间</th><th>摘要</th><th>位置</th></tr>' +
    r.merged.map((h, i) => '<tr><td>' + (i + 1) + '</td><td><code>' + esc(h.host) + '</code></td>' +
      '<td>' + esc(h.source) + '</td><td>' + esc((h.ts || '').slice(0, 16).replace('T', ' ')) + '</td>' +
      '<td class="snip">' + esc((h.snippet || '').replace(/\[\[|\]\]/g, '')) + '</td>' +
      '<td class="muted"><code>' + esc(h.path.split('/').pop()) + ':' + h.lineno + '</code></td></tr>').join('')
    + '</table>';
  log('搜索完成: ' + r.merged.length + ' 条合并结果');
}
refresh();
</script>
</body></html>
"""
