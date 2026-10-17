"""SQLite storage for meetings, transcript segments, and full-text search.

The database is encrypted with SQLCipher (AES-256) when a key is given.
"""
import os
import threading
import time
from pathlib import Path

import numpy as np
from sqlcipher3 import dbapi2 as sqlite3

PLAIN_HEADER = b"SQLite format 3\x00"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    app         TEXT,              -- zoom / teams / meet / manual
    user_notes  TEXT NOT NULL DEFAULT '',
    ai_notes    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    speaker     TEXT NOT NULL,     -- 'Me' or 'Them'
    t_start     REAL NOT NULL,     -- seconds since meeting start
    t_end       REAL NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON segments(meeting_id, t_start);

-- Voice clusters found in a meeting, with their fingerprint, so a rename can teach a voice profile.
CREATE TABLE IF NOT EXISTS meeting_speakers (
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    cluster     TEXT NOT NULL,     -- '<channel>:<n>', e.g. 'Them:0'
    name        TEXT NOT NULL,
    embedding   BLOB,
    seconds     REAL NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'auto',   -- auto / profile / you / user
    PRIMARY KEY (meeting_id, cluster)
);
CREATE TABLE IF NOT EXISTS speaker_turns (
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL,
    t_start     REAL NOT NULL,
    t_end       REAL NOT NULL,
    cluster     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_meeting ON speaker_turns(meeting_id);
-- Saved voice profiles: one running-average fingerprint per person.
CREATE TABLE IF NOT EXISTS voices (
    name        TEXT PRIMARY KEY,
    embedding   BLOB NOT NULL,
    samples     INTEGER NOT NULL DEFAULT 1,
    is_self     INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);

-- Note templates: built-ins are refreshed from templates.py on start; users can add their own.
CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    icon        TEXT NOT NULL DEFAULT 'file',
    description TEXT NOT NULL DEFAULT '',
    prompt      TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 100
);

-- One search row per meeting, rebuilt whenever its content changes.
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    title, user_notes, ai_notes, transcript,
    tokenize = 'porter unicode61'
);
"""


def _key_pragma(key: bytes) -> str:
    return f"PRAGMA key = \"x'{key.hex()}'\""


def is_plain(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == PLAIN_HEADER
    except OSError:
        return False


def encrypt_existing(path: Path, key: bytes):
    """One-time migration of an unencrypted database to SQLCipher, verified before the plain copy is removed."""
    tmp = path.with_suffix(".encrypting")
    tmp.unlink(missing_ok=True)
    plain = sqlite3.connect(str(path))
    plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    tables = [r[0] for r in plain.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'search_%'")]
    counts = {t: plain.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    plain.execute(f"ATTACH DATABASE ? AS enc KEY \"x'{key.hex()}'\"", (str(tmp),))
    plain.execute("SELECT sqlcipher_export('enc')")
    plain.execute("DETACH DATABASE enc")
    plain.close()
    check = sqlite3.connect(str(tmp))
    check.execute(_key_pragma(key))
    for t, n in counts.items():
        got = check.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        if got != n:
            check.close()
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"encryption check failed for {t}: {got} != {n}")
    check.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    os.replace(tmp, path)


class DB:
    def __init__(self, path: Path, key: bytes | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        if key and is_plain(path):
            encrypt_existing(path, key)
        self.encrypted = bool(key)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        if key:
            self._conn.execute(_key_pragma(key))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        mcols = {r[1] for r in self._conn.execute("PRAGMA table_info(meetings)")}
        if "template_id" not in mcols:
            self._conn.execute("ALTER TABLE meetings ADD COLUMN template_id TEXT")
        if "followup" not in mcols:
            self._conn.execute("ALTER TABLE meetings ADD COLUMN followup TEXT NOT NULL DEFAULT ''")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(segments)")}
        if "channel" not in cols:   # older databases: speaker used to be the channel
            self._conn.execute("ALTER TABLE segments ADD COLUMN channel TEXT")
            self._conn.execute("UPDATE segments SET channel = speaker")
        if "cluster" not in cols:
            self._conn.execute("ALTER TABLE segments ADD COLUMN cluster TEXT")
        # Meetings interrupted by a crash or quit never got an end time; close them at their last words.
        self._conn.execute(
            "UPDATE meetings SET ended_at = started_at + COALESCE("
            "(SELECT MAX(t_end) FROM segments s WHERE s.meeting_id = meetings.id), 0) "
            "WHERE ended_at IS NULL"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def _q(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    # --- meetings -------------------------------------------------------
    def create_meeting(self, title: str, app: str = "manual", template_id: str | None = None) -> int:
        cur = self._q(
            "INSERT INTO meetings(title, started_at, app, template_id) VALUES (?,?,?,?)",
            (title, time.time(), app, template_id),
        )
        self.reindex(cur.lastrowid)
        return cur.lastrowid

    def end_meeting(self, mid: int):
        self._q("UPDATE meetings SET ended_at=? WHERE id=?", (time.time(), mid))

    def update_meeting(self, mid: int, **fields):
        allowed = {"title", "user_notes", "ai_notes", "template_id", "followup"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        self._q(f"UPDATE meetings SET {cols} WHERE id=?", (*sets.values(), mid))
        self.reindex(mid)

    def delete_meeting(self, mid: int):
        self._q("DELETE FROM meetings WHERE id=?", (mid,))
        self._q("DELETE FROM search WHERE rowid=?", (mid,))

    def get_meeting(self, mid: int) -> dict | None:
        row = self._q("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        m = dict(row)
        m["segments"] = self.get_segments(mid)
        return m

    def list_meetings(self, limit: int = 200) -> list[dict]:
        rows = self._q(
            "SELECT id, title, started_at, ended_at, app FROM meetings "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_meetings_full(self, limit: int = 200) -> list[dict]:
        """Like list_meetings but includes notes (for the home dashboard)."""
        rows = self._q(
            "SELECT id, title, started_at, ended_at, app, user_notes, ai_notes FROM meetings "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- segments -------------------------------------------------------
    def add_segment(self, mid: int, speaker: str, t_start: float, t_end: float, text: str,
                    channel: str | None = None) -> dict:
        channel = channel or speaker
        cur = self._q(
            "INSERT INTO segments(meeting_id, speaker, channel, t_start, t_end, text) VALUES (?,?,?,?,?,?)",
            (mid, speaker, channel, t_start, t_end, text),
        )
        return {"id": cur.lastrowid, "speaker": speaker, "channel": channel,
                "t_start": t_start, "t_end": t_end, "text": text, "cluster": None}

    def replace_segments(self, mid: int, segments: list[dict]):
        """Swap the whole transcript (e.g. live draft -> refined pass) in one transaction."""
        with self._lock:
            self._conn.execute("DELETE FROM segments WHERE meeting_id=?", (mid,))
            self._conn.executemany(
                "INSERT INTO segments(meeting_id, speaker, channel, cluster, t_start, t_end, text) VALUES (?,?,?,?,?,?,?)",
                [(mid, s["speaker"], s.get("channel") or s["speaker"], s.get("cluster"),
                  s["t_start"], s["t_end"], s["text"]) for s in segments],
            )
            self._conn.commit()
        self.reindex(mid)

    def get_segments(self, mid: int) -> list[dict]:
        rows = self._q(
            "SELECT id, speaker, channel, cluster, t_start, t_end, text FROM segments "
            "WHERE meeting_id=? ORDER BY t_start, id",
            (mid,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_segment(self, seg_id: int) -> dict | None:
        row = self._q("SELECT id, meeting_id, speaker, channel, cluster, t_start, t_end, text FROM segments WHERE id=?",
                      (seg_id,)).fetchone()
        return dict(row) if row else None

    def update_segment(self, seg_id: int, text: str) -> int | None:
        """Edit one transcript line; returns its meeting id."""
        row = self._q("SELECT meeting_id FROM segments WHERE id=?", (seg_id,)).fetchone()
        if not row:
            return None
        self._q("UPDATE segments SET text=? WHERE id=?", (text, seg_id))
        self.reindex(row["meeting_id"])
        return row["meeting_id"]

    def delete_segment(self, seg_id: int) -> int | None:
        row = self._q("SELECT meeting_id FROM segments WHERE id=?", (seg_id,)).fetchone()
        if not row:
            return None
        self._q("DELETE FROM segments WHERE id=?", (seg_id,))
        self.reindex(row["meeting_id"])
        return row["meeting_id"]

    # --- templates --------------------------------------------------------
    def sync_builtin_templates(self, builtins: list[dict]):
        with self._lock:
            self._conn.executemany(
                "INSERT INTO templates(id, name, icon, description, prompt, builtin, position) VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, icon=excluded.icon, description=excluded.description, "
                "prompt=excluded.prompt, position=excluded.position",
                [(t["id"], t["name"], t["icon"], t["description"], t["prompt"], i) for i, t in enumerate(builtins)])
            self._conn.commit()

    def templates(self) -> list[dict]:
        rows = self._q("SELECT id, name, icon, description, prompt, builtin FROM templates "
                       "ORDER BY builtin DESC, position, name").fetchall()
        return [dict(r) for r in rows]

    def template(self, tid: str | None) -> dict | None:
        if not tid:
            return None
        row = self._q("SELECT id, name, icon, description, prompt, builtin FROM templates WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def save_template(self, tid: str | None, name: str, description: str, prompt: str, icon: str = "file") -> str:
        if tid:
            self._q("UPDATE templates SET name=?, description=?, prompt=?, icon=? WHERE id=? AND builtin=0",
                    (name, description, prompt, icon, tid))
            return tid
        tid = f"custom-{int(time.time() * 1000)}"
        self._q("INSERT INTO templates(id, name, icon, description, prompt, builtin) VALUES (?,?,?,?,?,0)",
                (tid, name, icon, description, prompt))
        return tid

    def delete_template(self, tid: str):
        self._q("DELETE FROM templates WHERE id=? AND builtin=0", (tid,))
        self._q("UPDATE meetings SET template_id=NULL WHERE template_id=?", (tid,))

    # --- speakers & voice profiles ----------------------------------------
    def save_speaker_analysis(self, mid: int, turns: list[dict], speakers: list[dict]):
        """turns: [{channel, t_start, t_end, cluster}]; speakers: [{cluster, name, embedding, seconds, source}]."""
        with self._lock:
            self._conn.execute("DELETE FROM speaker_turns WHERE meeting_id=?", (mid,))
            self._conn.execute("DELETE FROM meeting_speakers WHERE meeting_id=?", (mid,))
            self._conn.executemany(
                "INSERT INTO speaker_turns(meeting_id, channel, t_start, t_end, cluster) VALUES (?,?,?,?,?)",
                [(mid, t["channel"], t["t_start"], t["t_end"], t["cluster"]) for t in turns])
            self._conn.executemany(
                "INSERT INTO meeting_speakers(meeting_id, cluster, name, embedding, seconds, source) VALUES (?,?,?,?,?,?)",
                [(mid, s["cluster"], s["name"], _blob(s.get("embedding")), s["seconds"], s["source"]) for s in speakers])
            self._conn.commit()

    def speaker_turns(self, mid: int) -> list[dict]:
        rows = self._q("SELECT channel, t_start, t_end, cluster FROM speaker_turns WHERE meeting_id=?", (mid,)).fetchall()
        return [dict(r) for r in rows]

    def meeting_speakers(self, mid: int) -> list[dict]:
        rows = self._q("SELECT cluster, name, embedding, seconds, source FROM meeting_speakers "
                       "WHERE meeting_id=? ORDER BY seconds DESC", (mid,)).fetchall()
        return [{**dict(r), "embedding": _vec(r["embedding"])} for r in rows]

    def rename_cluster(self, mid: int, cluster: str, name: str):
        self._q("UPDATE meeting_speakers SET name=?, source='user' WHERE meeting_id=? AND cluster=?", (name, mid, cluster))
        self._q("UPDATE segments SET speaker=? WHERE meeting_id=? AND cluster=?", (name, mid, cluster))
        self.reindex(mid)

    def voices(self) -> dict:
        rows = self._q("SELECT name, embedding, samples, is_self, updated_at FROM voices").fetchall()
        return {r["name"]: {"embedding": _vec(r["embedding"]), "samples": r["samples"],
                            "is_self": bool(r["is_self"]), "updated_at": r["updated_at"]} for r in rows}

    def save_voice(self, name: str, embedding, samples: int, is_self: bool = False):
        self._q("INSERT INTO voices(name, embedding, samples, is_self, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET embedding=excluded.embedding, samples=excluded.samples, "
                "is_self=MAX(is_self, excluded.is_self), updated_at=excluded.updated_at",
                (name, _blob(embedding), samples, int(is_self), time.time()))

    def delete_voice(self, name: str):
        self._q("DELETE FROM voices WHERE name=?", (name,))

    def voice_meeting_counts(self) -> dict[str, int]:
        rows = self._q("SELECT name, COUNT(DISTINCT meeting_id) AS n FROM meeting_speakers GROUP BY name").fetchall()
        return {r["name"]: r["n"] for r in rows}

    def transcript_text(self, mid: int) -> str:
        return "\n".join(
            f"[{_ts(s['t_start'])}] {s['speaker']}: {s['text']}" for s in self.get_segments(mid)
        )

    # --- search ---------------------------------------------------------
    def reindex(self, mid: int):
        row = self._q("SELECT title, user_notes, ai_notes FROM meetings WHERE id=?", (mid,)).fetchone()
        if not row:
            return
        transcript = " ".join(s["text"] for s in self.get_segments(mid))
        self._q("DELETE FROM search WHERE rowid=?", (mid,))
        self._q(
            "INSERT INTO search(rowid, title, user_notes, ai_notes, transcript) VALUES (?,?,?,?,?)",
            (mid, row["title"], row["user_notes"], row["ai_notes"], transcript),
        )

    def search(self, query: str, limit: int = 50) -> list[dict]:
        # Quote each term so user input can't break FTS syntax; prefix-match the last one.
        terms = [t.replace('"', "") for t in query.split() if t.strip()]
        if not terms:
            return self.list_meetings(limit)
        fts = " ".join(f'"{t}"' for t in terms[:-1]) + f' "{terms[-1]}"*'
        rows = self._q(
            "SELECT m.id, m.title, m.started_at, m.ended_at, m.app, "
            "snippet(search, -1, '<mark>', '</mark>', '…', 12) AS snippet "
            "FROM search JOIN meetings m ON m.id = search.rowid "
            "WHERE search MATCH ? ORDER BY rank LIMIT ?",
            (fts.strip(), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def _blob(v) -> bytes | None:
    return None if v is None else np.asarray(v, dtype=np.float32).tobytes()


def _vec(b):
    return None if b is None else np.frombuffer(b, dtype=np.float32).copy()


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"
