"""SQLite transaction protects deduplication and ready-draft slot reservations."""
import json
import sqlite3
from pathlib import Path
from .domain import fingerprint


class DraftStore:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS drafts (fingerprint TEXT PRIMARY KEY, '
                        'draft_id TEXT UNIQUE, channel TEXT, effective_slot TEXT, payload TEXT NOT NULL)')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS occupied ON drafts(channel, effective_slot) '
                        'WHERE effective_slot IS NOT NULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def occupied(self, channel, request):
        rows = self.db.execute('SELECT effective_slot FROM drafts WHERE channel=? AND fingerprint<>? '
                               'AND effective_slot IS NOT NULL', (channel, fingerprint(request)))
        return {row[0] for row in rows}

    def save(self, request, draft, fail=False):
        key = fingerprint(request)
        try:
            self.db.execute('BEGIN IMMEDIATE')
            binding = self.db.execute('SELECT fingerprint FROM requests WHERE request_id=?',
                                      (request['request_id'],)).fetchone()
            if binding and binding[0] != key:
                self.db.rollback()
                return {'ok': False, 'error': 'idempotency_conflict', 'retryable': False}
            existing = self.db.execute('SELECT payload FROM drafts WHERE fingerprint=?', (key,)).fetchone()
            if existing:
                previous = json.loads(existing[0])
                revised = dict(draft, draft_id=previous['draft_id'])
                changed = previous != revised
                if changed:
                    if fail:
                        self.db.rollback()
                        return {'ok': False, 'error': 'save_unavailable', 'retryable': False}
                    self.db.execute('UPDATE drafts SET channel=?, effective_slot=?, payload=? WHERE fingerprint=?',
                                    (revised['channel'], revised['effective_slot'],
                                     json.dumps(revised, ensure_ascii=False), key))
                self.db.execute('INSERT OR IGNORE INTO requests VALUES (?,?)', (request['request_id'], key))
                self.db.commit()
                return {'ok': True, 'save_status': 'updated' if changed else 'existing', 'draft': revised}
            if fail:
                self.db.rollback()
                return {'ok': False, 'error': 'save_unavailable', 'retryable': False}
            draft = dict(draft, draft_id='draft-' + key[:16])
            self.db.execute('INSERT INTO drafts VALUES (?,?,?,?,?)',
                            (key, draft['draft_id'], draft['channel'], draft['effective_slot'],
                             json.dumps(draft, ensure_ascii=False)))
            self.db.execute('INSERT INTO requests VALUES (?,?)', (request['request_id'], key))
            self.db.commit()
            return {'ok': True, 'save_status': 'saved', 'draft': draft}
        except sqlite3.IntegrityError:
            self.db.rollback()
            return {'ok': False, 'error': 'slot_conflict', 'retryable': True}
        except sqlite3.Error:
            self.db.rollback()
            return {'ok': False, 'error': 'storage_error', 'retryable': False}

    def count(self):
        return self.db.execute('SELECT COUNT(*) FROM drafts').fetchone()[0]
