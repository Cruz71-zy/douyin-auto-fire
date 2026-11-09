"""Independent, crash-safe PC journal and private login snapshot."""
import json
from pathlib import Path
import sqlite3
from app.auth_state import normalize_storage_state, write_storage_state


class LocalJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(day TEXT PRIMARY KEY, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts(day TEXT, reason TEXT, PRIMARY KEY(day,reason));
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def status(self, day):
        with self.connect() as db:
            row = db.execute('SELECT status FROM days WHERE day=?', (day,)).fetchone()
        return row[0] if row else None

    def contains(self, day):
        return self.status(day) is not None

    def reserve(self, day):
        with self.connect() as db:
            return db.execute("INSERT OR IGNORE INTO days VALUES (?, 'reserved')", (day,)).rowcount == 1

    def finish(self, day, status):
        if status not in ('success', 'unknown'):
            raise ValueError('Invalid journal transition')
        with self.connect() as db:
            changed = db.execute("UPDATE days SET status=? WHERE day=? AND status='reserved'", (status, day)).rowcount
            if changed != 1:
                raise ValueError('Cannot overwrite an already final journal')

    def attempt(self, day):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT count FROM attempts WHERE day=?', (day,)).fetchone()
            if count and count[0] >= 3:
                return False
            db.execute('INSERT INTO attempts VALUES (?,1) ON CONFLICT(day) DO UPDATE SET count=count+1', (day,))
            return True

    def alert_once(self, day, reason):
        with self.connect() as db:
            return db.execute('INSERT OR IGNORE INTO alerts VALUES (?,?)', (day, reason)).rowcount == 1

    def release_alert(self, day, reason):
        with self.connect() as db:
            db.execute('DELETE FROM alerts WHERE day=? AND reason=?', (day, reason))


class LocalSessionStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        return normalize_storage_state(json.loads(self.path.read_text(encoding='utf-8')))

    def save(self, state):
        write_storage_state(self.path, state)


def due(now):
    minutes = now.hour * 60 + now.minute
    return 450 <= minutes < 1410  # 07:30 to 23:30; no past-day catch-up.
