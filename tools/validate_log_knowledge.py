import collections,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dome_agent.core import Detector,number,Search
from dome_agent.trends import Trends
K=ROOT/'knowledge';rows=[json.loads(l) for l in (K/'chunks.jsonl').read_text().splitlines()];ids={r['id'] for r in rows}
assert len(ids)==len(rows)
manifest=json.loads((K/'log_manifest.json').read_text());sources={m['file']:m for m in manifest}
for m in manifest:assert hashlib.sha256((K/'log_sources'/m['file']).read_bytes()).hexdigest()==m['sha256']
for r in rows:
 if r['metadata'].get('version')!='logs_v07':continue
 for ref in r['metadata']['source_refs']:
  if 'chunk_id' in ref:assert ref['chunk_id'] in ids
  else:assert 1<=ref['line_start']<=ref['line_end']<=sources[ref['file']]['records']
reports=[]
for p in sorted((K/'log_sources').glob('telemetry*.jsonl')):
 detector=Detector();trends=Trends();previous=set();episodes=collections.Counter();count=0;first=None;last=None
 for l in p.open():
  r=json.loads(l);nodes=r['nodes'];first=first or r['ts'];last=r['ts'];stamps=sorted(n['updated_at'] for n in nodes.values() if number(n.get('updated_at')));stamp=stamps[len(stamps)//2]
  issues=detector.analyze(nodes,stamp);issues+=trends.analyze(nodes,stamp,issues)
  current={i['id'] for i in issues}
  for i in issues:
   if i['id'] not in previous:episodes[i['code']]+=1
  previous=current;count+=1
 reports.append({'file':p.name,'snapshots':count,'from':first,'to':last,'signal_episodes':dict(episodes)})
report={'sources_and_references_valid':True,'llm_called':False,'equipment_commands_sent':False,'replay':reports,'note':'Эпизоды — сигналы правил, не размеченная точность диагностики. Поминутные снимки могут пропускать короткие события.'}
(K/'validation_v07.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'snapshots':sum(r['snapshots'] for r in reports),'sources':len(manifest),'chunks':len(rows),'refs_valid':True}))
