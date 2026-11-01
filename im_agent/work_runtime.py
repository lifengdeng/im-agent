"""Resource admission, bounded proactive work and progress-only supervision."""
import json
import math
import time

from .contracts import Rejected, actor, canonical, fingerprint, identifier


def install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS work_resources (
            run TEXT PRIMARY KEY, item TEXT NOT NULL, resources TEXT NOT NULL,
            state TEXT NOT NULL, proof TEXT);
        CREATE TABLE IF NOT EXISTS proactive_candidates (
            id TEXT PRIMARY KEY, case_key TEXT NOT NULL, policy TEXT NOT NULL, input TEXT NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, result TEXT);
        CREATE TABLE IF NOT EXISTS work_observations (
            item TEXT PRIMARY KEY, progress TEXT NOT NULL, since REAL NOT NULL,
            issue TEXT, revision INTEGER NOT NULL);
    ''')


class Resources:
    @staticmethod
    def acquire(db, item, run, resources, *, limit=2):
        identifier(item); identifier(run)
        if not isinstance(resources, list) or len(resources) > 8 or type(limit) is not int or not 1 <= limit <= 32:
            raise Rejected('work_resource_budget')
        for key in resources: identifier(key)
        resources = sorted(set(resources))
        prior = db.execute('SELECT * FROM work_resources WHERE run=?', (run,)).fetchone()
        if prior:
            if prior['item'] != item or json.loads(prior['resources']) != resources: raise Rejected('work_resource_binding_changed')
            return prior['state'] == 'active'
        rows = db.execute("SELECT * FROM work_resources WHERE state IN ('active','unknown')").fetchall()
        if len(rows) >= limit: return False
        if any(r['item']==item or set(json.loads(r['resources'])) & set(resources) for r in rows): return False
        db.execute("INSERT INTO work_resources VALUES(?,?,?,'active',NULL)", (run, item, canonical(resources)))
        return True

    @staticmethod
    def finish(db, run, state, *, proof=None):
        if state not in {'closed','unknown'}: raise Rejected('work_resource_state_invalid')
        row = db.execute('SELECT state,proof FROM work_resources WHERE run=?', (run,)).fetchone()
        if row is None: return
        if row['state']=='closed' and state!='closed': raise Rejected('work_resource_already_closed')
        if row['proof'] is not None and proof not in {None,row['proof']}: raise Rejected('work_resource_proof_changed')
        if row[0] == 'unknown' and state == 'closed' and not proof: raise Rejected('work_resource_reconciliation_required')
        if proof is not None and (not isinstance(proof,str) or len(proof)!=64 or any(c not in '0123456789abcdef' for c in proof)):
            raise Rejected('work_resource_proof_invalid')
        db.execute('UPDATE work_resources SET state=?,proof=coalesce(?,proof) WHERE run=?', (state, proof, run))


class Proactive:
    """Rules and allowed capabilities come only from the trusted composition root."""
    def __init__(self, policies):
        self.policies = {}
        for policy in policies:
            required = {'id','revision','source','kinds','capability','mode','hourly_budget'}
            if (not isinstance(policy,dict) or set(policy)!=required or policy['mode'] not in {'off','shadow','enabled'}
                    or type(policy['hourly_budget']) is not int or not 1<=policy['hourly_budget']<=16
                    or not isinstance(policy['kinds'],list) or not 1<=len(policy['kinds'])<=8):
                raise Rejected('proactive_policy_invalid')
            for key in ['id','revision','source','capability']: identifier(policy[key])
            for kind in policy['kinds']: identifier(kind)
            if policy['id'] in self.policies: raise Rejected('proactive_policy_collision')
            self.policies[policy['id']] = dict(policy)

    def propose(self, db, policy_id, event, case_key, *, now=None):
        now = time.time() if now is None else now
        policy = self.policies[policy_id]; identifier(case_key)
        actor(event['actor'])
        for key in ['id','source','kind']: identifier(event[key])
        if type(now) not in {int,float} or not math.isfinite(now) or len(canonical(event).encode())>24576:
            raise Rejected('proactive_input_budget')
        if policy['source'] != event['source'] or event['kind'] not in policy['kinds'] or policy['mode']=='off': return None
        key = fingerprint([policy,event['actor'],event['id']])
        prior = db.execute('SELECT * FROM proactive_candidates WHERE id=?', (key,)).fetchone()
        if prior: return dict(prior)
        state = 'shadow' if policy['mode']=='shadow' else 'queued'
        used = db.execute("SELECT COUNT(*) FROM proactive_candidates WHERE policy=? AND created>? AND state IN ('queued','running','ready','unknown')",
                          (canonical(policy),now-3600)).fetchone()[0]
        if state=='queued' and used>=policy['hourly_budget']: state='budget_exhausted'
        db.execute('INSERT INTO proactive_candidates VALUES(?,?,?,?,?,?,NULL)',
                   (key,case_key,canonical(policy),canonical(event),state,now))
        return dict(db.execute('SELECT * FROM proactive_candidates WHERE id=?',(key,)).fetchone())

    def claim(self, db, candidate):
        row = db.execute('SELECT * FROM proactive_candidates WHERE id=?',(candidate,)).fetchone()
        if not row or row['state']!='queued': return None
        policy = json.loads(row['policy'])
        if self.policies.get(policy['id']) != policy:
            db.execute("UPDATE proactive_candidates SET state='rejected' WHERE id=?",(candidate,)); return None
        db.execute("UPDATE proactive_candidates SET state='running' WHERE id=? AND state='queued'",(candidate,))
        return {'id':candidate,'policy':policy,'event':json.loads(row['input']),'case_key':row['case_key']}

    @staticmethod
    def finish(db, candidate, result):
        if not isinstance(result,dict) or len(canonical(result).encode())>8192: raise Rejected('proactive_result_budget')
        changed = db.execute("UPDATE proactive_candidates SET state='ready',result=? WHERE id=? AND state='running'",
                             (canonical(result),candidate)).rowcount
        if not changed: raise Rejected('proactive_result_requires_reconciliation')

    @staticmethod
    def recover(db):
        db.execute("UPDATE proactive_candidates SET state='unknown' WHERE state='running'")


class Watchdog:
    """No retries, model calls or repair tools. Repeated heartbeats are not progress."""
    @staticmethod
    def observe(db, item, state, checkpoint, *, now=None, stall_after=900):
        identifier(item)
        now=time.time() if now is None else now
        if (type(now) not in {int,float} or not math.isfinite(now) or type(stall_after) is not int or stall_after<1
                or len(canonical(checkpoint).encode())>4096):
            raise Rejected('watchdog_time_invalid')
        progress=fingerprint([state,checkpoint])
        row=db.execute('SELECT * FROM work_observations WHERE item=?',(item,)).fetchone()
        since=now if row is None or row['progress']!=progress else row['since']
        issue = ('reconciliation_required' if state=='unknown' else 'authentication_required' if state=='waiting_auth'
                 else 'no_progress' if state in {'running','waiting'} and now-since>=stall_after else None)
        old = row['issue'] if row else None
        revision=(row['revision'] if row else 0)+int(old!=issue)
        db.execute('INSERT OR REPLACE INTO work_observations VALUES(?,?,?,?,?)',(item,progress,since,issue,revision))
        if issue==old: return None
        return {'id':fingerprint([item,revision,issue]),'kind':issue or 'recovered','item':item,
                'revision':revision,'context_only':True,'repair_authorized':False}
