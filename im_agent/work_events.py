"""Immutable source events, explicit subscriptions and scoped context reads."""
import json

from .contracts import Rejected, actor, canonical, fingerprint, identifier


def install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS work_source_events (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, actor TEXT NOT NULL,
            source TEXT NOT NULL, thread TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS work_subscriptions (
            item TEXT NOT NULL, source TEXT NOT NULL, thread TEXT NOT NULL,
            PRIMARY KEY(item,source,thread));
        CREATE TABLE IF NOT EXISTS work_event_inbox (
            item TEXT NOT NULL, event TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY(item,event));
        CREATE INDEX IF NOT EXISTS work_source_thread ON work_source_events(actor,source,thread);
    ''')


class Events:
    def __init__(self, items, allow):
        self.items, self.allow = items, allow

    def permitted(self, who, source, thread):
        actor(who); identifier(source); identifier(thread)
        if self.allow(who, source, thread) is not True:
            raise Rejected('work_source_scope_rejected')

    def subscribe(self, db, item, who, item_thread, source, thread):
        self.items.get(db, item, who, item_thread)
        self.permitted(who, source, thread)
        db.execute('INSERT OR IGNORE INTO work_subscriptions VALUES(?,?,?)', (item, source, thread))

    def unsubscribe(self, db, item, who, item_thread, source, thread):
        self.items.get(db, item, who, item_thread)
        db.execute('DELETE FROM work_subscriptions WHERE item=? AND source=? AND thread=?', (item, source, thread))

    def receive(self, db, value):
        fields = {'id','actor','source','thread','kind','summary','body','links'}
        if not isinstance(value, dict) or set(value) != fields:
            raise Rejected('work_source_event_invalid')
        for key in ['id','source','thread','kind']: identifier(value[key])
        self.permitted(value['actor'], value['source'], value['thread'])
        if (any(not isinstance(value[k], str) for k in ['summary','body'])
                or not 1 <= len(value['summary']) <= 500 or len(value['body'].encode()) > 16384
                or not isinstance(value['links'], list) or len(value['links']) > 8):
            raise Rejected('work_source_event_budget')
        for ref in value['links']: identifier(ref)
        key = fingerprint([value['actor'], value['source'], value['id']])
        digest = fingerprint(value)
        prior = db.execute('SELECT digest FROM work_source_events WHERE id=?', (key,)).fetchone()
        if prior:
            if prior[0] != digest: raise Rejected('work_source_event_collision')
            return {'id': key, 'duplicate': True, 'delivered': 0}
        db.execute('INSERT INTO work_source_events VALUES(?,?,?,?,?,?)',
                   (key, digest, canonical(value['actor']), value['source'], value['thread'], canonical(value)))
        delivered = 0
        rows = db.execute('''SELECT w.* FROM work_items w JOIN work_subscriptions s ON s.item=w.id
            WHERE w.actor=? AND s.source=? AND s.thread=? AND w.state IN ('queued','running','waiting')''',
            (canonical(value['actor']), value['source'], value['thread'])).fetchall()
        for row in rows:
            try: self.items.get(db, row['id'], value['actor'], row['thread'])
            except Rejected: continue
            db.execute("INSERT OR IGNORE INTO work_event_inbox VALUES(?,?,'queued')", (row['id'], key))
            delivered += 1
        return {'id': key, 'duplicate': False, 'delivered': delivered}

    def _event(self, db, item, who, item_thread, reference):
        self.items.get(db, item, who, item_thread); identifier(reference)
        row = db.execute('''SELECT e.* FROM work_source_events e JOIN work_subscriptions s
            ON s.source=e.source AND s.thread=e.thread WHERE s.item=? AND e.id=? AND e.actor=?''',
            (item, reference, canonical(who))).fetchone()
        if row is None: raise Rejected('work_context_unavailable')
        self.permitted(who, row['source'], row['thread'])
        value = json.loads(row['payload'])
        if (fingerprint(value) != row['digest'] or canonical(value['actor']) != row['actor']
                or value['source'] != row['source'] or value['thread'] != row['thread']
                or fingerprint([value['actor'], value['source'], value['id']]) != row['id']):
            raise Rejected('work_context_changed')
        return value

    def read(self, db, item, who, item_thread, reference, *, offset=0, limit=2000):
        if type(offset) is not int or not 0 <= offset <= 16384 or type(limit) is not int or not 1 <= limit <= 2000:
            raise Rejected('work_context_read_budget')
        value = self._event(db, item, who, item_thread, reference)
        links = []
        for ref in value['links']:
            try: related = self._event(db, item, who, item_thread, ref)
            except Rejected: continue
            links.append({'reference': ref, 'summary': related['summary']})
        body = value['body'][offset:offset+limit]
        return {'reference': reference, 'source': value['source'], 'kind': value['kind'], 'summary': value['summary'],
                'body': body, 'next_offset': offset+len(body) if offset+len(body) < len(value['body']) else None,
                'links': links, 'context_only': True, 'grants_authority': False}

    def context(self, db, item, who, item_thread, *, budget=4096):
        self.items.get(db, item, who, item_thread)
        if type(budget) is not int or not 512 <= budget <= 8192: raise Rejected('work_context_budget')
        entries = []
        rows = db.execute('''SELECT event FROM work_event_inbox WHERE item=?
            ORDER BY rowid DESC LIMIT 8''', (item,)).fetchall()
        for row in rows:
            try: value = self.read(db, item, who, item_thread, row[0], limit=160)
            except Rejected: continue
            if len(canonical({'events':entries+[value],'context_only':True,'grants_authority':False}).encode()) > budget: break
            entries.append(value)
            db.execute("UPDATE work_event_inbox SET state='seen' WHERE item=? AND event=?", (item, row[0]))
        return {'events': list(reversed(entries)), 'context_only': True, 'grants_authority': False}
