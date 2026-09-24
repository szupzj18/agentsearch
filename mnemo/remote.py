import concurrent.futures
import json
import os
import shlex
import subprocess

from .index import Index
from .search import search as local_search

CONFIG_DIR = os.path.expanduser("~/.mnemo")
CONFIG_PATH = os.path.join(CONFIG_DIR, "remotes.json")
LOCAL = "local"
RRF_K = 60
CONNECT_TIMEOUT = 8


class RemoteError(Exception):
    pass


# Machines provisioned with a stock MIT krb5.conf (missing the Byted realm)
# keep a user-level override at ~/.krb5.conf; point ssh/GSSAPI at it when the
# launcher itself was started without KRB5_CONFIG in its environment.
_USER_KRB5_CONF = os.path.join(os.path.expanduser("~"), ".krb5.conf")
if os.path.exists(_USER_KRB5_CONF) and not os.environ.get("KRB5_CONFIG"):
    os.environ["KRB5_CONFIG"] = _USER_KRB5_CONF


# --------------------------------------------------------------------- config

def load_remotes():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for r in data.get("remotes", []):
        if r.get("name") and r.get("host"):
            r.setdefault("bin", "~/mnemo/bin/mnemo")
            out.append(r)
    return out


def save_remotes(remotes):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"remotes": remotes}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def add_remote(name, host, bin_path="~/mnemo/bin/mnemo"):
    remotes = load_remotes()
    if any(r["name"] == name for r in remotes):
        raise RemoteError("remote %r already registered" % name)
    remotes.append({"name": name, "host": host, "bin": bin_path})
    save_remotes(remotes)


def remove_remote(name):
    remotes = load_remotes()
    kept = [r for r in remotes if r["name"] != name]
    if len(kept) == len(remotes):
        raise RemoteError("no remote named %r" % name)
    save_remotes(kept)


def get_remote(name):
    for r in load_remotes():
        if r["name"] == name:
            return r
    raise RemoteError("no remote named %r" % name)


# ------------------------------------------------------------------------ ssh

def _socket(remote):
    return os.path.join(CONFIG_DIR, "ssh-%s" % remote["name"])


def _ssh_base(remote):
    return [
        "ssh",
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=" + _socket(remote),
        "-o", "ControlPersist=10m",
        "-o", "ConnectTimeout=%d" % CONNECT_TIMEOUT,
        "-o", "BatchMode=yes",
        remote["host"],
    ]


def _remote_command(remote, argv):
    bin_path = remote["bin"]
    if bin_path.startswith("~/"):
        bin_render = "~/" + shlex.quote(bin_path[2:])
    else:
        bin_render = shlex.quote(bin_path)
    parts = ["python3", bin_render] + [shlex.quote(a) for a in argv]
    return " ".join(parts)


def remote_exec(remote, argv, timeout=60):
    cmd = _ssh_base(remote) + [_remote_command(remote, argv)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError("%s: timed out after %ds" % (remote["name"], timeout))
    except OSError as exc:
        raise RemoteError("%s: %s" % (remote["name"], exc))
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "exit code %d" % p.returncode).strip()
        raise RemoteError("%s: %s" % (remote["name"], msg.splitlines()[-1][:200]))
    return p.stdout


def ping(remote):
    remote_exec(remote, ["--version"], timeout=CONNECT_TIMEOUT + 4)


def install(remote, logger=lambda m: None, timeout=600):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rsync = [
        "rsync", "-az", "--delete",
        "--exclude", ".git",
        "--exclude", "__pycache__",
        "--exclude", "*.pyc",
        "--exclude", "index.db",
        "-e", "ssh",
        repo + "/",
        "%s:~/mnemo/" % remote["host"],
    ]
    try:
        p = subprocess.run(rsync, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemoteError("rsync to %s failed: %s" % (remote["host"], exc))
    if p.returncode != 0:
        raise RemoteError("rsync to %s failed: %s" % (remote["host"], p.stderr.strip()[:200]))
    logger("code synced; building index on %s" % remote["name"])
    out = remote_exec(remote, ["index"], timeout=timeout)
    logger(out.strip())
    ping(remote)


# ----------------------------------------------------------------- search io

def search_argv(query, sources, kinds, cwd, since, limit):
    argv = ["search", query, "--json", "--limit", str(limit), "--host", LOCAL]
    # --host local pins the remote to its own index. Without it the remote
    # would fan out to its own registered devices, chaining the federation,
    # re-tagging other hosts' hits with the remote's name.
    if sources:
        argv += ["--source", ",".join(sources)]
    if kinds is not None:
        if kinds:
            argv += ["--kind", ",".join(kinds)]
        else:
            argv.append("--all-kinds")
    if cwd:
        argv += ["--cwd", cwd]
    if since:
        argv += ["--since", since]
    return argv


def _remote_search(remote, query, sources, kinds, cwd, since, limit, sync):
    if sync:
        remote_exec(remote, ["index"], timeout=120)
    out = remote_exec(
        remote, search_argv(query, sources, kinds, cwd, since, limit), timeout=30
    )
    rows = json.loads(out)
    for h in rows:
        h["host"] = remote["name"]
    return rows


def _rrf(per_host, limit):
    scores = {}
    payload = {}
    for host, hits in per_host:
        for i, h in enumerate(hits):
            key = (host, h["path"], h["lineno"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + i + 1)
            payload[key] = h
    keys = sorted(scores, key=lambda k: (-scores[k], k[0], k[1], k[2]))
    return [payload[k] for k in keys[:limit]]


def _local_search(db_path, query, sources, kinds, cwd, since, limit):
    idx = Index(db_path)
    try:
        return local_search(
            idx, query, sources=sources, kinds=kinds,
            cwd=cwd, since=since, limit=limit,
        )
    finally:
        idx.close()


def fan_out_search(
    index,
    query,
    sources=None,
    kinds=None,
    cwd=None,
    since=None,
    limit=20,
    hosts=None,
    sync_remotes=True,
):
    """Search local index plus registered remotes in parallel.

    hosts: optional subset (names); None means local + every remote.
    Returns (hits, warnings).
    """
    remotes = load_remotes()
    wanted = set(hosts) if hosts else None
    known = {LOCAL} | {r["name"] for r in remotes}
    if wanted is not None:
        unknown = wanted - known
        if unknown:
            raise RemoteError("unknown host(s): %s" % ", ".join(sorted(unknown)))

    selected = [r for r in remotes if wanted is None or r["name"] in wanted]
    include_local = wanted is None or LOCAL in wanted
    warnings = []

    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected) + 1) as pool:
        if include_local:
            jobs[pool.submit(
                _local_search, index.db_path, query, sources, kinds, cwd, since, limit,
            )] = LOCAL
        for r in selected:
            jobs[pool.submit(
                _remote_search, r, query, sources, kinds, cwd, since, limit, sync_remotes
            )] = r["name"]
        per_host = []
        for fut in concurrent.futures.as_completed(jobs):
            name = jobs[fut]
            try:
                per_host.append((name, fut.result()))
            except RemoteError as exc:
                warnings.append(str(exc))

    if include_local:
        for h in next((rows for host, rows in per_host if host == LOCAL), []):
            h["host"] = LOCAL

    hits = _rrf(per_host, limit)
    return hits, warnings


def remote_session(remote, path, head=None, tail=None, raw=False, timeout=60):
    argv = ["session", path, "--json"]
    if head is not None:
        argv += ["--head", str(head)]
    if tail is not None:
        argv += ["--tail", str(tail)]
    if raw:
        argv.append("--raw")
    return json.loads(remote_exec(remote, argv, timeout=timeout))


def remote_context(remote, path, line, before, after, raw=False, timeout=30):
    argv = ["context", path, str(line), "--json",
            "--before", str(before), "--after", str(after)]
    if raw:
        argv.append("--raw")
    out = remote_exec(remote, argv, timeout=timeout)
    return json.loads(out)


def remote_status(remote, timeout=20):
    out = remote_exec(remote, ["status", "--json"], timeout=timeout)
    return json.loads(out)
