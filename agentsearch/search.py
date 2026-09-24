from .model import CJK_RUN, has_cjk
from .sources import SOURCES

DEFAULT_KINDS = ("text", "summary", "tool_call", "tool_result")


def _phrase(token):
    return '"' + token.replace('"', '""') + '"'


def _cjk_match(token):
    parts = []
    pos = 0
    for m in CJK_RUN.finditer(token):
        if m.start() > pos:
            seg = token[pos : m.start()].strip().lower()
            if seg:
                parts.append(_phrase(seg))
        run = m.group(0)
        if len(run) == 1:
            parts.append(_phrase(run))
        else:
            bigrams = " AND ".join(_phrase(run[i : i + 2]) for i in range(len(run) - 1))
            parts.append("(" + bigrams + ")")
        pos = m.end()
    if pos < len(token):
        seg = token[pos:].strip().lower()
        if seg:
            parts.append(_phrase(seg))
    return " AND ".join(parts)


def build_match(query):
    groups = []
    for tok in query.split():
        alts = ["body : %s*" % _phrase(tok)]
        if has_cjk(tok):
            grams_q = _cjk_match(tok)
            if grams_q:
                alts.append("grams : (%s)" % grams_q)
        groups.append("(" + " OR ".join(alts) + ")")
    return " AND ".join(groups)


def search(
    index,
    query,
    sources=None,
    kinds=None,
    cwd=None,
    since=None,
    limit=20,
):
    where = ["messages MATCH ?"]
    params = [build_match(query)]
    if sources:
        where.append("source IN (%s)" % ",".join("?" * len(sources)))
        params.extend(sources)
    if kinds is None:
        kinds = DEFAULT_KINDS
    if kinds:
        where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        params.extend(kinds)
    if cwd:
        where.append("cwd LIKE ?")
        params.append("%" + cwd.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if since:
        where.append("ts >= ?")
        params.append(since)
    params.append(limit)

    sql = (
        "SELECT path, lineno, source, session_id, cwd, ts, role, kind, "
        "snippet(messages, 0, '[[', ']]', ' … ', 18) AS snippet, "
        "bm25(messages) AS rank "
        "FROM messages WHERE " + " AND ".join(where) + " ORDER BY rank LIMIT ?"
    )
    return [dict(r) for r in index.db.execute(sql, params).fetchall()]


def get_session(index, path):
    rng = index.db.execute(
        "SELECT lo, hi FROM file_ranges WHERE path = ?", (path,)
    ).fetchone()
    if not rng:
        return None
    lo, hi = rng["lo"], rng["hi"]

    meta = index.db.execute(
        "SELECT source, session_id, cwd FROM files WHERE path = ?", (path,)
    ).fetchone()
    span = index.db.execute(
        "SELECT MIN(ts) AS started_at, MAX(ts) AS ended_at FROM messages"
        " WHERE rowid BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()

    messages = [
        {
            "lineno": r["lineno"],
            "ts": r["ts"],
            "role": r["role"],
            "kind": r["kind"],
            "text": r["body"],
        }
        for r in index.db.execute(
            "SELECT lineno, ts, role, kind, body FROM messages"
            " WHERE rowid BETWEEN ? AND ? ORDER BY rowid",
            (lo, hi),
        ).fetchall()
    ]
    return {
        "path": path,
        "source": meta["source"] if meta else None,
        "session_id": meta["session_id"] if meta else None,
        "cwd": meta["cwd"] if meta else None,
        "started_at": span["started_at"],
        "ended_at": span["ended_at"],
        "count": len(messages),
        "messages": messages,
    }


def _source_for(index, path):
    row = index.db.execute(
        "SELECT source FROM files WHERE path = ?", (path,)
    ).fetchone()
    if not row:
        return None
    cls = SOURCES.get(row["source"])
    return cls() if cls else None


def raw_session(index, path):
    """Read the full session straight from its JSONL file, no 20k cap."""
    source = _source_for(index, path)
    if source is None:
        return None
    sid, cwd, parsed = source.parse(path, clip_text=False)
    messages = [
        {"lineno": lineno, "ts": m.ts, "role": m.role, "kind": m.kind, "text": m.text}
        for lineno, m in parsed
    ]
    return {
        "path": path,
        "source": source.name,
        "session_id": sid,
        "cwd": cwd,
        "started_at": messages[0]["ts"] if messages else "",
        "ended_at": messages[-1]["ts"] if messages else "",
        "count": len(messages),
        "messages": messages,
        "raw": True,
    }


def raw_context(index, path, line, before=4, after=8):
    sess = raw_session(index, path)
    if sess is None:
        return None
    messages = sess["messages"]
    hits = [i for i, m in enumerate(messages) if m["lineno"] == line]
    if not hits:
        return []
    lo = max(0, hits[0] - before)
    hi = min(len(messages), hits[-1] + 1 + after)
    hit_set = set(hits)
    window = messages[lo:hi]
    for offset, m in enumerate(window):
        m["hit"] = (lo + offset) in hit_set
    return window


def get_context(index, path, line, before=4, after=8, home=None):
    rng = index.db.execute(
        "SELECT lo, hi FROM file_ranges WHERE path = ?", (path,)
    ).fetchone()
    if not rng:
        return None
    lo, hi = rng["lo"], rng["hi"]

    hit_rows = [
        r[0]
        for r in index.db.execute(
            "SELECT rowid FROM messages WHERE rowid BETWEEN ? AND ? AND lineno = ?"
            " ORDER BY rowid",
            (lo, hi, line),
        ).fetchall()
    ]
    if not hit_rows:
        return []
    first_hit, last_hit = hit_rows[0], hit_rows[-1]

    win = index.db.execute(
        "SELECT"
        "  (SELECT MIN(rowid) FROM (SELECT rowid FROM messages"
        "    WHERE rowid BETWEEN ? AND ? ORDER BY rowid DESC LIMIT ?)) AS win_lo,"
        " (SELECT MAX(rowid) FROM (SELECT rowid FROM messages"
        "    WHERE rowid BETWEEN ? AND ? ORDER BY rowid ASC LIMIT ?)) AS win_hi",
        (lo, first_hit - 1, before, last_hit + 1, hi, after),
    ).fetchone()
    win_lo = win["win_lo"] if win["win_lo"] is not None else first_hit
    win_hi = win["win_hi"] if win["win_hi"] is not None else last_hit

    out = []
    for r in index.db.execute(
        "SELECT rowid AS rid, lineno, ts, role, kind, body FROM messages"
        " WHERE rowid BETWEEN ? AND ? ORDER BY rowid",
        (win_lo, win_hi),
    ).fetchall():
        out.append(
            {
                "lineno": r["lineno"],
                "ts": r["ts"],
                "role": r["role"],
                "kind": r["kind"],
                "text": r["body"],
                "hit": first_hit <= r["rid"] <= last_hit,
            }
        )
    return out
