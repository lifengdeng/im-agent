"""Explicitly enabled, bounded host execution. No queued futures or retries."""
from collections import deque
import json
import re
import threading

from .contracts import Rejected, canonical, identifier
from .service import execute_claim


class ExecutionPool:
    def __init__(self, client, host, *, capacity=2, executor='pooled-host', heartbeat_interval=10):
        if type(capacity) is not int or not 1<=capacity<=32:raise Rejected('execution_capacity_invalid')
        identifier(executor)
        if type(heartbeat_interval) not in {int,float} or not 0<heartbeat_interval<=10:
            raise Rejected('execution_heartbeat_interval_invalid')
        self.client,self.host,self.capacity=client,host,capacity
        self.executor,self.heartbeat_interval=executor,heartbeat_interval
        self.lock,self.dispatch=threading.Lock(),threading.Lock()
        self.active={};self.closing=False
        self.completed=self.unknown=0;self.outcomes=deque(maxlen=64)

    def _run(self, claim):
        code=None
        try:execute_claim(self.client,self.host,claim,heartbeat_interval=self.heartbeat_interval)
        except BaseException as exc:
            code=str(exc) if isinstance(exc,Rejected) and re.fullmatch('[a-z_]{1,100}',str(exc)) else type(exc).__name__
        finally:
            with self.lock:
                self.active.pop(claim['execution'],None)
                if code:self.unknown+=1
                else:self.completed+=1
                self.outcomes.append({'execution':claim['execution'],'state':'unknown' if code else 'completed','error_code':code})

    def step(self):
        """Fill available slots and return. The caller can continue maintenance."""
        started=0
        with self.dispatch:
            # Bound dispatch work even when very short jobs finish immediately.
            for _ in range(self.capacity):
                with self.lock:
                    if self.closing or len(self.active)>=self.capacity:break
                claim=self.client.call('/v1/claim',{'executor':self.executor})
                if not claim:break
                claim=json.loads(canonical(claim))
                thread=threading.Thread(target=self._run,args=(claim,),daemon=True)
                with self.lock:
                    if claim['execution'] in self.active:raise Rejected('execution_already_dispatched')
                    self.active[claim['execution']]=thread
                try:thread.start()
                except Exception:
                    with self.lock:self.active.pop(claim['execution'],None)
                    raise
                started+=1
        return started

    def status(self):
        with self.lock:
            return {'capacity':self.capacity,'active':len(self.active),'closing':self.closing,
                    'completed':self.completed,'unknown':self.unknown}

    def close(self):
        """Stop intake, then drain accepted leases with their heartbeats alive."""
        with self.dispatch:
            with self.lock:
                self.closing=True;threads=list(self.active.values())
        for thread in threads:thread.join()
