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

    def register_request(self, request):
        """Bind identity before the first model/tool call, including failed runs."""
        key = fingerprint(request)
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT fingerprint FROM requests WHERE request_id=?',
                                  (request['request_id'],)).fetchone()
            if row and row[0] != key:
                self.db.rollback()
                return {'ok': False, 'error': 'idempotency_conflict', 'retryable': False}
            self.db.execute('INSERT OR IGNORE INTO requests VALUES (?,?)', (request['request_id'], key))
            self.db.commit()
            return {'ok': True}
        except sqlite3.Error:
            self.db.rollback()
            return {'ok': False, 'error': 'storage_error', 'retryable': False}

    def get(self, request):
        row = self.db.execute('SELECT payload FROM drafts WHERE fingerprint=?', (fingerprint(request),)).fetchone()
        return json.loads(row[0]) if row else None

    def invalidate(self, material_id, current_revision=None, reason='review_not_approved', request=None):
        """Revoke stale proofs and release reservations; keep the historical draft row."""
        changed = 0
        try:
            self.db.execute('BEGIN IMMEDIATE')
            for key, payload in self.db.execute('SELECT fingerprint, payload FROM drafts').fetchall():
                draft = json.loads(payload)
                if request is not None and key != fingerprint(request):
                    continue
                if draft.get('material_id') != material_id or draft.get('draft_status') == 'invalidated':
                    continue
                if current_revision is not None and draft.get('source_revision') == current_revision:
                    continue
                if draft.get('effective_slot'):
                    draft['previous_effective_slot'] = draft['effective_slot']
                draft.update(effective_slot=None, draft_status='invalidated', invalidation_reason=reason)
                self.db.execute('UPDATE drafts SET effective_slot=NULL, payload=? WHERE fingerprint=?',
                                (json.dumps(draft, ensure_ascii=False), key))
                changed += 1
            self.db.commit()
            return changed
        except sqlite3.Error:
            self.db.rollback()
            raise

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
            self.db.execute('INSERT OR IGNORE INTO requests VALUES (?,?)', (request['request_id'], key))
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
