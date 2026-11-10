"""Independent, crash-safe PC journal and private login snapshot."""
import json
from pathlib import Path
import sqlite3
import hashlib
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
                CREATE TABLE IF NOT EXISTS receipts(day TEXT, identity TEXT, status TEXT, attempts INTEGER DEFAULT 0,
                    PRIMARY KEY(day,identity));
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

    def receipt_statuses(self, day):
        with self.connect() as db:
            return dict(db.execute('SELECT identity,status FROM receipts WHERE day=?', (day,)))

    def prepare_receipts(self, day, identities):
        if len(set(identities)) != 4:
            raise ValueError('Four distinct identities required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=dict(db.execute('SELECT identity,status FROM receipts WHERE day=?',(day,)))
            if prior:
                if set(prior)!=set(identities): raise ValueError('Plan changed during the day')
                return
            if db.execute('SELECT 1 FROM days WHERE day=?',(day,)).fetchone():
                raise ValueError('Legacy uncertain batch cannot be retried automatically')
            db.execute("INSERT INTO days VALUES (?,'reserved')",(day,))
            db.executemany("INSERT INTO receipts(day,identity,status) VALUES (?,?,'pending')",[(day,i) for i in identities])

    def summarize_receipts(self, day):
        states=list(self.receipt_statuses(day).values())
        if len(states)!=4: raise ValueError('Incomplete receipt journal')
        status='success' if all(s=='success' for s in states) else 'unknown'
        with self.connect() as db:
            db.execute('UPDATE days SET status=? WHERE day=?',(status,day))
        return status


def receipt_identity(kind, uid):
    return hashlib.sha256((kind+':'+uid).encode()).hexdigest()


class TargetJournal:
    """Retries only before the durable trigger boundary; uncertain sends stay blocked."""
    def __init__(self, journal, identity):
        self.journal,self.identity=journal,identity

    def contains(self, day):
        with self.journal.connect() as db:
            row=db.execute('SELECT status,attempts FROM receipts WHERE day=? AND identity=?',(day,self.identity)).fetchone()
        return row is None or row[0] not in ('pending','safe_failed','reserved') or row[1]>=3

    def reserve(self, day):
        with self.journal.connect() as db:
            return db.execute("UPDATE receipts SET status='reserved',attempts=attempts+1 WHERE day=? AND identity=? AND status IN ('pending','safe_failed','reserved') AND attempts<3",(day,self.identity)).rowcount==1

    def before_trigger(self, day):
        with self.journal.connect() as db:
            if db.execute("UPDATE receipts SET status='triggering' WHERE day=? AND identity=? AND status='reserved'",(day,self.identity)).rowcount!=1:
                raise ValueError('Trigger reservation missing')

    def finish(self, day, status):
        with self.journal.connect() as db:
            row=db.execute('SELECT status FROM receipts WHERE day=? AND identity=?',(day,self.identity)).fetchone()
            if row is None: raise ValueError('Missing target journal')
            if status=='success' and row[0]=='triggering': next_status='success'
            elif status=='unknown' and row[0]=='reserved': next_status='safe_failed'
            elif status=='unknown' and row[0]=='triggering': next_status='unknown'
            else: raise ValueError('Invalid target transition')
            db.execute('UPDATE receipts SET status=? WHERE day=? AND identity=?',(next_status,day,self.identity))


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
