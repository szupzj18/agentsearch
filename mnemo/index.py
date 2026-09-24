import os
import sqlite3
import time

from .model import cjk_grams
from .sources import SOURCES, get_sources

DEFAULT_DB_PATH = os.path.expanduser("~/.mnemo/index.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path       TEXT PRIMARY KEY,
  source     TEXT NOT NULL,
  session_id TEXT,
  cwd        TEXT,
  mtime      REAL NOT NULL,
  size       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS file_ranges (
  path TEXT NOT NULL,
  lo   INTEGER NOT NULL,
  hi   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
  body,
  grams,
  source     UNINDEXED,
  session_id UNINDEXED,
  cwd        UNINDEXED,
  ts         UNINDEXED,
  role       UNINDEXED,
  kind       UNINDEXED,
  path       UNINDEXED,
  lineno     UNINDEXED,
  tokenize = "unicode61"
);
"""

DEFAULT_KINDS = ("text", "summary", "tool_call", "tool_result")


class Index:
    def __init__(self, db_path=None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.db = sqlite3.connect(self.db_path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ sync

    def sync(self, source_names=None, home=None, logger=None):
        log = logger or (lambda msg: None)
        sources = get_sources(source_names, home=home)
        stats = dict(files_new=0, files_updated=0, files_removed=0, messages=0)

        for source in sources:
            current = {}
            for path in source.files():
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                current[path] = (st.st_mtime, st.st_size)

            rows = self.db.execute(
                "SELECT path, mtime, size FROM files WHERE source = ?", (source.name,)
            ).fetchall()
            known = {r["path"]: (r["mtime"], r["size"]) for r in rows}

            for path, (mtime, size) in current.items():
                if path not in known:
                    stats["files_new"] += 1
                elif known[path] != (mtime, size):
                    stats["files_updated"] += 1
                else:
                    continue
                self._reindex_file(source, path, mtime, size, stats, log)

            gone = set(known) - set(current)
            for path in gone:
                self._drop_path(path)
                self.db.execute("DELETE FROM files WHERE path = ?", (path,))
                stats["files_removed"] += 1

        if stats["files_removed"]:
            # Safety net for FTS rows left without a covering file_ranges row
            # (interrupted reindex in older code): their hits would fail context.
            self.db.execute(
                "DELETE FROM messages WHERE rowid NOT IN"
                " (SELECT m.rowid FROM messages m"
                "  JOIN file_ranges fr ON m.rowid BETWEEN fr.lo AND fr.hi)"
            )

        self.db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_sync', ?)",
            (str(time.time()),),
        )
        return stats

    def _reindex_file(self, source, path, mtime, size, stats, log):
        try:
            session_id, cwd, msgs = source.parse(path)
        except Exception as exc:  # corrupt or unexpected file: skip, don't crash sync
            log("skip %s: %s" % (path, exc))
            return
        self._drop_path(path)
        if not msgs:
            self.db.execute(
                "INSERT OR REPLACE INTO files(path, source, session_id, cwd, mtime, size)"
                " VALUES(?,?,?,?,?,?)",
                (path, source.name, session_id, cwd, mtime, size),
            )
            return
        rows = []
        for lineno, msg in msgs:
            body = msg.text
            rows.append(
                (
                    body,
                    cjk_grams(body),
                    msg.role,
                    msg.kind,
                    lineno,
                    msg.ts,
                    source.name,
                    session_id,
                    cwd,
                    path,
                )
            )
        self.db.execute("BEGIN")
        lo = self.db.execute("SELECT COALESCE(MAX(rowid), 0) + 1 FROM messages").fetchone()[0]
        self.db.executemany(
            "INSERT INTO messages(body, grams, role, kind, lineno, ts,"
            " source, session_id, cwd, path)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        hi = lo + len(rows) - 1
        self.db.execute(
            "INSERT INTO file_ranges(path, lo, hi) VALUES(?,?,?)", (path, lo, hi)
        )
        self.db.execute(
            "INSERT OR REPLACE INTO files(path, source, session_id, cwd, mtime, size)"
            " VALUES(?,?,?,?,?,?)",
            (path, source.name, session_id, cwd, mtime, size),
        )
        self.db.execute("COMMIT")
        stats["messages"] += len(rows)

    def _drop_path(self, path):
        for r in self.db.execute(
            "SELECT lo, hi FROM file_ranges WHERE path = ?", (path,)
        ).fetchall():
            self.db.execute("DELETE FROM messages WHERE rowid BETWEEN ? AND ?", (r["lo"], r["hi"]))
        self.db.execute("DELETE FROM file_ranges WHERE path = ?", (path,))

    # ------------------------------------------------------------------ info

    def counts(self):
        out = {}
        for r in self.db.execute(
            "SELECT source, COUNT(*) AS files FROM files GROUP BY source"
        ):
            out[r["source"]] = {"files": r["files"], "messages": 0}
        for r in self.db.execute(
            "SELECT source, COUNT(*) AS n FROM messages GROUP BY source"
        ):
            out.setdefault(r["source"], {"files": 0, "messages": 0})["messages"] = r["n"]
        return out

    def last_sync(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        return float(row["value"]) if row else None
