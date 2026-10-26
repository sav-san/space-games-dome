"""Deterministic, offline log ingestion; preserves source line references."""
import collections,hashlib,json,math,re,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
K=ROOT/'knowledge'
def digest(b):return hashlib.sha256(b).hexdigest()
def write(p,obj):p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
def num(x):return type(x) in (int,float) and math.isfinite(x)
def flat(x,path=''):
 if isinstance(x,dict):
  for k,v in x.items():yield from flat(v,f'{path}.{k}' if path else k)
 elif isinstance(x,list):
  for i,v in enumerate(x):yield from flat(v,f'{path}[{i}]')
 else:yield path,x

def build():
 base=K/'base_chunks.jsonl'
 if not base.exists():shutil.copyfile(K/'chunks.jsonl',base)
 chunks=[json.loads(l) for l in base.read_text().splitlines() if l.strip()]
 manifest=[];seen=set();new=[];profiles={};events_by_day={}
 def add(text,kind,source,sha,refs,nodes=(),date=None):
  text='Исторические данные, не текущее состояние. '+text
  h=digest((kind+sha+text).encode())[:20]
  new.append({'id':'logs_v07:'+h,'text':text,'embedding_text':' '.join(nodes)+' '+text,'metadata':{'source':source,'source_sha256':sha,'document_role':kind,'nodes':list(nodes),'date':date,'source_refs':refs,'historical':True,'causal_certainty':'not_established','version':'logs_v07'}})
 for p in sorted((K/'log_sources').glob('*.jsonl')):
  raw=p.read_bytes();sha=digest(raw);rows=[]
  for i,l in enumerate(raw.decode('utf-8-sig').splitlines(),1):
   if l.strip():rows.append((i,json.loads(l)))
  dates=sorted({r.get('ts','')[:10] for _,r in rows})
  item={'file':p.name,'sha256':sha,'records':len(rows),'dates':dates,'duplicate_of':None}
  if sha in seen:item['duplicate_of']=next(m['file'] for m in manifest if m['sha256']==sha);manifest.append(item);continue
  seen.add(sha);manifest.append(item)
  # Oct 14 telemetry/events are already covered by the original corpus; retain raw provenance.
  if sha in json.loads((K/'existing_log_hashes.json').read_text()):
   item['ingestion']='exact_duplicate_of_existing_corpus';continue
  date=dates[0]
  if p.name.startswith('logbook'):
   for line,r in rows:
    parts=re.split(r'(?<=[.!?])\s+',r['text']);buf=''
    for part in parts+['']:
     if len(buf)+len(part)>1500 or not part:
      if buf:add('Запись оператора '+r['ts']+'. Утверждения автора и гипотезы; не считать доказанными причинами. Запись может описывать предыдущие дни или более поздние события. Не переносить советы о снятии защит без независимой проверки.\n'+buf,'operator_account',p.name,sha,[{'file':p.name,'line_start':line,'line_end':line}],date=date)
      buf=''
     buf+=part+' '
  elif p.name.startswith('events'):
   events_by_day[date]=[r for _,r in rows]
   groups=collections.defaultdict(list);passages=0
   for line,r in rows:
    if r.get('param')=='passage_events':passages+=1;continue
    groups[r.get('node_id','station')].append((line,r))
   for node,items in groups.items():
    buf=[];size=0
    for line,r in items+[(None,None)]:
     text=json.dumps(r,ensure_ascii=False) if r else ''
     if buf and (size+len(text)>2800 or r is None):
      refs=[{'file':p.name,'line_start':n,'line_end':n} for n,_ in buf]
      add('События узла '+node+' за '+date+'. success означает принятие команды, не физическое выполнение. Действия прошлого могут быть недоступны текущему агенту.\n'+'\n'.join(json.dumps(v,ensure_ascii=False) for _,v in buf),'event_evidence',p.name,sha,refs,[node],date)
      buf=[];size=0
     if r:buf.append((line,r));size+=len(text)
   item['passage_records_aggregated']=passages
  elif p.name.startswith('telemetry'):
   stats=collections.defaultdict(dict)
   for line,r in rows:
    for node,n in r.get('nodes',{}).items():
     for key,v in flat(n):
      if key=='updated_at' or key.startswith(('history','recent_messages','passage_events')):continue
      rec=stats[node].setdefault(key,{'numeric_count':0,'null_count':0,'states':{},'first':v,'last':v})
      rec['last']=v
      if num(v):
       rec['numeric_count']+=1
       for typ,op in [('min',lambda a,b:a<b),('max',lambda a,b:a>b)]:
        if typ not in rec or op(v,rec[typ]):rec[typ]=v;rec[typ+'_at']=r['ts'];rec[typ+'_line']=line
      elif v is None:rec['null_count']+=1
      elif len(rec['states'])<20:rec['states'][str(v)]=rec['states'].get(str(v),0)+1
   for node,fields in stats.items():
    profiles[date+':'+node]=fields
    # bounded chunks; every measured extremum has exact source line.
    buf={}
    for key,rec in list(fields.items())+[(None,None)]:
     if buf and (len(json.dumps(buf,ensure_ascii=False))>2200 or key is None):
      add('Сводка измерений '+node+' за '+date+'. Минимумы/максимумы относятся к отдельным моментам, не обязательно совпадают. Нормативные пороги из этих диапазонов не выводятся.\n'+json.dumps(buf,ensure_ascii=False),'telemetry_profile',p.name,sha,[{'file':p.name,'line_start':rows[0][0],'line_end':rows[-1][0]}],[node],date);buf={}
     if key is not None:buf[key]=rec
 write(K/'log_manifest.json',manifest);write(K/'telemetry_statistics.json',profiles)
 # Curated search cards refer to evidence chunks; no unsupported causal certainty.
 cards=json.loads((K/'case_notes.json').read_text())
 for case in cards:
  refs=[{'chunk_id':r['id']} for r in new if r['metadata']['date'] in case['dates'] and (set(r['metadata']['nodes'])&set(case['nodes']) or r['metadata']['document_role']=='operator_account')]
  text=case['title']+'\nСимптомы и поиск: '+case['symptoms']+'\nИнтерпретация: '+case['interpretation']+'\nПроверки и предложения: '+case['steps']+'\nПроверка результата: '+case['verification']+'\nИсторический период: '+', '.join(case['dates'])
  add(text,'incident_case','case_notes.json',digest((K/'case_notes.json').read_bytes()),refs,case['nodes'],','.join(case['dates']))
 ids={r['id'] for r in chunks}
 chunks.extend(r for r in new if r['id'] not in ids)
 (K/'chunks.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in chunks),encoding='utf-8')
 summary={'input_files':len(manifest),'input_rows':sum(m['records'] for m in manifest),'base_chunks':len(ids),'added_chunks':len(chunks)-len(ids),'total_chunks':len(chunks),'new_types':dict(collections.Counter(r['metadata']['document_role'] for r in new)),'dates':sorted({d for m in manifest for d in m['dates']})}
 write(K/'ingestion_report.json',summary);print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':build()
