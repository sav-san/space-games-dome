"""Read-only validator for document 03 section 5. python tools/check_logs.py logs"""
import datetime
import json
import pathlib
import sys

FIELDS={
 'start':('agent','version','models','config'), 'stop':('reason',),
 'connection':('event','details'), 'observation':('summary','metrics','anomalies'),
 'decision':('situation','cause','confidence','evidence','plan','source'),
 'action':('request','response','ref'), 'verification':('ref','expected','observed','ok'),
 'message':('direction','text'),
 'llm_call':('model','purpose','prompt_tokens','completion_tokens','latency_ms','ok','error','summary'),
 'error':('where','error')}


def validate(rows):
    errors=[];by_seq={};run=None
    for i,r in enumerate(rows,1):
        prefix=f'line {i}: '
        for f in ('ts','run_id','seq','kind'):
            if f not in r:errors.append(prefix+'missing '+f)
        try:
            ts=datetime.datetime.fromisoformat(r['ts'])
            if ts.tzinfo is None or '.' not in r['ts']:raise ValueError()
        except Exception:errors.append(prefix+'invalid ts')
        if r.get('seq')!=i:errors.append(prefix+'invalid sequence')
        if run is None:run=r.get('run_id')
        elif run!=r.get('run_id'):errors.append(prefix+'run_id changed')
        kind=r.get('kind')
        if kind not in FIELDS:errors.append(prefix+'unknown kind '+str(kind))
        for f in FIELDS.get(kind,()):
            if f not in r:errors.append(prefix+'missing '+f)
        if kind in ('action','verification'):
            parent=by_seq.get(r.get('ref'))
            wanted='decision' if kind=='action' else 'action'
            if parent is None or parent.get('kind')!=wanted:errors.append(prefix+'ref must point to '+wanted)
        if kind=='decision':
            c=r.get('confidence')
            if isinstance(c,bool) or not isinstance(c,(int,float)) or not 0<=c<=1:errors.append(prefix+'invalid confidence')
            if r.get('source') not in ('rules','llm','rag','mixed'):errors.append(prefix+'invalid source')
        if kind=='verification' and type(r.get('ok')) is not bool:errors.append(prefix+'ok must be boolean')
        if kind=='connection' and r.get('event')=='auth' and not all(k in r for k in ('role','team')):errors.append(prefix+'missing auth role/team')
        if kind=='message' and ('to' if r.get('direction')=='out' else 'sender') not in r:errors.append(prefix+'missing recipient/sender')
        if kind=='action' and not all(k in r.get('response',{}) for k in ('success','error')):errors.append(prefix+'incomplete response')
        by_seq[r.get('seq')]=r
    refs={r.get('ref') for r in rows if r.get('kind')=='verification'}
    for r in rows:
        if r.get('kind')=='action' and r.get('request',{}).get('type')=='control' and r['seq'] not in refs:errors.append(f"action {r['seq']}: no verification (possible interrupted run)")
    return errors

if __name__=='__main__':
    path=pathlib.Path(sys.argv[1] if len(sys.argv)>1 else 'logs')
    files=[path] if path.is_file() else sorted(path.glob('agent_*.jsonl'))
    errors=[]
    if not files:errors.append('Log files not found')
    for f in files:
        try:errors.extend(str(f)+': '+e for e in validate([json.loads(s) for s in f.read_text(encoding='utf-8').splitlines() if s.strip()]))
        except Exception as e:errors.append(str(f)+': '+str(e))
    print('\n'.join(errors) if errors else f'OK: {len(files)} files')
    sys.exit(bool(errors))
