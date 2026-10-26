import collections
import datetime as dt
import json
import math
import os
import re
import uuid
from pathlib import Path


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# Shared tolerance for node timestamps produced just after the envelope timestamp.
NODE_FUTURE_TOLERANCE_SECONDS = 2.0


def node_time_error(node, timestamp, stale_seconds):
    updated=node.get('updated_at')
    if not number(timestamp):return 'В снимке отсутствует корректный timestamp'
    if not number(updated):return 'У узла отсутствует корректный updated_at'
    age=timestamp-updated
    if age < -NODE_FUTURE_TOLERANCE_SECONDS:
        return f'Метка узла опережает снимок на {-age:.3f} с; проверьте часы источника'
    if age > stale_seconds:
        return f'Данные узла старше порога: {age:.3f} с > {stale_seconds:g} с'
    return None


def env_file(path):
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('\"\''))


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


class Log:
    def __init__(self, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        self.run_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        self.seq = 0
        self.secrets = [os.getenv(k) for k in ('DOME_KEY', 'DEEPSEEK_API_KEY') if os.getenv(k)]
        self.file = open(Path(folder) / ('agent_' + self.run_id + '.jsonl'), 'a', encoding='utf-8')

    def clean(self, v):
        if isinstance(v, dict):
            return {k: ('[REDACTED]' if any(s in k.lower() for s in ('api_key', 'authorization', 'dome_key')) else self.clean(x)) for k,x in v.items()}
        if isinstance(v, list): return [self.clean(x) for x in v]
        if isinstance(v, str):
            for s in self.secrets: v = v.replace(s, '[REDACTED]')
        return v

    def write(self, kind, **fields):
        self.seq += 1
        row = dict(fields, ts=dt.datetime.now().astimezone().isoformat(timespec='milliseconds'), run_id=self.run_id, seq=self.seq, kind=kind)
        self.file.write(json.dumps(self.clean(row), ensure_ascii=False, default=str) + '\n')
        self.file.flush()
        return self.seq

    def close(self): self.file.close()


class Search:
    """Local BM25 with words and Russian prefix features; no downloads/API calls."""
    def __init__(self, path):
        self.rows = [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()]
        self.docs = [collections.Counter(self.tokens(r.get('embedding_text', r['text']))) for r in self.rows]
        self.df = collections.Counter(t for d in self.docs for t in d)
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lengths)/max(1,len(self.docs))

    @staticmethod
    def tokens(text):
        ts = re.findall(r'[a-zа-яё0-9_]+', text.lower().replace('₂','2'))
        stop=set('и в на с со по для при из к от до не что как это или а но у за во об the a an of to in is and or'.split())
        ts=[t for t in ts if t not in stop]
        return ts + [part for t in ts if '_' in t for part in t.split('_') if len(part)>2] + ['~'+t[:5] for t in ts if re.fullmatch('[а-яё]{6,}',t)]

    def search(self, query, k=5):
        terms = set(self.tokens(query)); scores=[]; N=len(self.rows)
        for i,d in enumerate(self.docs):
            score=0
            for t in terms:
                f=d.get(t,0)
                if f:
                    idf=math.log(1+(N-self.df[t]+.5)/(self.df[t]+.5))
                    score+=idf*f*2.5/(f+1.5*(.25+.75*self.lengths[i]/max(1,self.avg)))
            if score: scores.append((score,i))
        weighted=[]
        for score,i in scores:
            role=self.rows[i]['metadata'].get('document_role','')
            score*= {'incident_case':1.35,'telemetry_profile':.70,'operator_account':.75}.get(role,1.0)
            weighted.append((score,i))
        codes=set(re.findall(r'\b(?:e[0-9]{2}|il_[a-z_]+|[a-z]+_[a-z_]+)\b',query.lower()))
        weighted=[(score*(1.4 if codes & set(self.tokens(self.rows[i]['text'])) else 1),i) for score,i in weighted]
        chosen=[];counts=collections.Counter()
        for score,i in sorted(weighted,reverse=True):
            role=self.rows[i]['metadata'].get('document_role','')
            cap={'telemetry_profile':2,'operator_account':1,'incident_case':3}.get(role,k)
            if counts[role]>=cap:continue
            chosen.append((score,i));counts[role]+=1
            if len(chosen)>=k:break
        # Include an original evidence chunk alongside a leading historical case.
        cases=[i for _,i in chosen if self.rows[i]['metadata'].get('document_role')=='incident_case']
        if cases and k>=2:
            refs={r.get('chunk_id') for r in self.rows[cases[0]]['metadata'].get('source_refs',[])}
            evidence=[(score,i) for score,i in sorted(weighted,reverse=True) if self.rows[i]['id'] in refs and self.rows[i]['metadata'].get('document_role') in ('event_evidence','telemetry_profile')]
            if evidence and not any(self.rows[i]['id'] in refs and self.rows[i]['metadata'].get('document_role') in ('event_evidence','telemetry_profile') for _,i in chosen):
                if len(chosen)>=k:chosen[-1]=evidence[0]
                else:chosen.append(evidence[0])
        return [dict(id=self.rows[i]['id'],text=self.rows[i]['text'],metadata=self.rows[i]['metadata'],score=round(score,4)) for score,i in chosen]

    def for_issues(self, issues, k=8):
        found={}
        # One best match per issue before filling: avoid the first device dominating RAG.
        primary=[]
        for issue in issues[:8]:
            q=issue['node_id']+' '+issue['code']+' '+issue['summary']+' '+json.dumps(issue.get('evidence',{}),ensure_ascii=False)
            hits=self.search(q,3)
            if hits:primary.append(hits[0])
            for hit in hits:found[hit['id']]=hit
        result=[];seen=set()
        for hit in primary+sorted(found.values(),key=lambda x:x['score'],reverse=True):
            if hit['id'] not in seen:result.append(hit);seen.add(hit['id'])
            if len(result)>=k:break
        return result



class Detector:
    def __init__(self, stale=20):
        self.stale=stale; self.last={}; self.known=set()

    def analyze(self, nodes, timestamp):
        out=[]
        def issue(code, node, category, severity, summary, evidence, steps):
            out.append(dict(id=code+':'+node, code=code, node_id=node, category=category, severity=severity, summary=summary,
                            evidence=evidence, recommended_steps=steps, certainty='observed_symptom', cause='Причина требует проверки'))
        healthy={}
        for node,n in nodes.items():
            if node=='meeting_01' or not isinstance(n,dict): continue
            updated=n.get('updated_at'); old=self.last.get(node)
            if number(updated):
                if old is None or old[0]!=updated: self.last[node]=(updated,timestamp)
            frozen=old is not None and old[0]==updated and number(timestamp) and timestamp-old[1]>self.stale
            age=timestamp-updated if number(timestamp) and number(updated) else None
            bad=n.get('online') is False or n.get('status')=='UNKNOWN' or frozen or (age is not None and (age>self.stale or age < -NODE_FUTURE_TOLERANCE_SECONDS))
            healthy[node]=not bad
            if bad:
                issue('STALE',node,'telemetry_quality','high','Нет актуальных данных узла',{'status':n.get('status'),'online':n.get('online'),'updated_at':updated,'age_s':age},['Проверить связь и изменение updated_at; не делать вывод об исправности по последнему показанию.','Сверить доступность service_actions.reconnect через describe_nodes.'])
                continue
            if n.get('sensor_state') in ('DEGRADED','FAULT'):
                issue('SENSOR_QUALITY',node,'sensor_quality','medium','Снижена достоверность датчика',{'sensor_state':n.get('sensor_state'),'confidence':n.get('confidence')},['Сопоставить независимые измерения с учётом времени и задержки.'])
            errors={k:n[k] for k in ('error_code','alarm_code','fault_code','hold_reason') if n.get(k)}
            if errors or n.get('alarm') is True or n.get('error') is True or n.get('state') in ('FAULT','ERROR','ALARM'):
                issue('FAULT',node,'equipment_fault','high','Ошибка или удержание оборудования',errors or {'state':n.get('state'),'alarm':n.get('alarm')},['Установить причину по коду ошибки и свежим данным.','Не сбрасывать защиту и не повторять resume до устранения причины.'])
            if n.get('smoke_detected') is True or (node.startswith(('chem_sensor','radiation_sensor')) and n.get('alarm_state')=='ALARM'):
                issue('HAZARD',node,'safety','critical','Сигнал потенциальной опасности',{'smoke_detected':n.get('smoke_detected'),'alarm_state':n.get('alarm_state')},['Применить утверждённую аварийную процедуру; не объявлять сигнал ложным без проверки.'])
            for k in ('co2_ppm','temperature_c','humidity_pct'):
                if k in n and n[k] is None:
                    issue('NULL_'+k,node,'telemetry_quality','medium','Показание отсутствует',{'field':k,'value':None},['Получить независимый актуальный источник.'])
            if number(n.get('clock_offset_s')) and abs(n['clock_offset_s'])>5:
                issue('CLOCK',node,'communication','high','Рассинхронизация часов',{'clock_offset_s':n['clock_offset_s']},['Уточнить, какие временные отметки затронуты.','Проверить разрешённую синхронизацию и затем clock_offset_s.'])
        for node in self.known-set(nodes):
            issue('MISSING',node,'telemetry_quality','high','Узел исчез из полного снимка',{},['Дождаться свежей телеметрии и проверить связь и права.'])
        self.known|={n for n in nodes if n!='meeting_01'}
        def valid(n):return healthy.get(n,False)
        p=nodes.get('printer_3d_01') or {}
        if valid('printer_3d_01') and p.get('webhook_state')=='COMPLETED' and (p.get('state')=='PRINTING' or (number(p.get('progress_pct')) and p['progress_pct']<100)):
            issue('WEBHOOK','printer_3d_01','conflicting_data','high','Webhook сообщает завершение при незавершённой печати',{k:p.get(k) for k in ('state','webhook_state','progress_pct','z_mm')},['Не открывать бокс по webhook.','Проверить динамику progress_pct, z_mm и состояние принтера.'])
        climate={n:{k:v.get(k) for k in ('co2_ppm','reading_delay_s','sensor_state','confidence')} for n,v in nodes.items() if n.startswith('climate_sensor') and isinstance(v,dict) and valid(n) and number(v.get('co2_ppm'))}
        high=[n for n,v in climate.items() if v['co2_ppm']>2000]
        if high:
            values=[v['co2_ppm'] for v in climate.values()]
            disagree=len(values)>1 and max(values)-min(values)>700
            issue('CO2','climate','conflicting_data' if disagree else 'environment','high' if disagree else 'critical','Расхождение CO₂; реальная опасность не исключена' if disagree else 'Высокий CO₂',climate,['Сопоставить датчики с учётом reading_delay_s, качества и динамики.','Проверить приток и интерлок IL_CO2; большинство датчиков само по себе не доказывает ложную тревогу.'])
        b=nodes.get('battery_01') or {}
        if valid('battery_01') and number(b.get('soc_pct')) and b['soc_pct']<35:
            issue('LOW_SOC','battery_01','energy','high' if b['soc_pct']<25 else 'medium','Низкий запас заряда АКБ',{'soc_pct':b['soc_pct'],'heuristic_warning_pct':35},['Оценить потребление, генерацию, доступность сети и прогноз автономности.','35% — настроечная эвристика предупреждения, не уставка сервера.'])
        for line in (nodes.get('smart_panel_01') or {}).get('lines',[]) or []:
            if not isinstance(line,dict) or not valid('smart_panel_01'):continue
            if line.get('alarm') or line.get('state') in ('TRIPPED','RCD_TRIP') or (number(line.get('lockout_remaining_s')) and line['lockout_remaining_s']>0):
                issue('LINE_'+str(line.get('line')),'smart_panel_01','power_distribution','high','Авария или блокировка линии',{k:line.get(k) for k in ('line','name','state','power_w','current_a','alarm_code','lockout_reason','lockout_remaining_s')},['Проверить причину защиты, актуальное назначение линии и фактическую нагрузку.','Не переключать замороженное реле повторно.'])
        auto=nodes.get('dome_automation_01') or {}
        for key,v in (auto.get('interlocks') or {}).items():
            if isinstance(v,dict) and v.get('state')=='TRIPPED' and valid('dome_automation_01'):
                issue(key,'dome_automation_01','interlock','critical' if key=='IL_SMOKE' else 'high','Сработал интерлок '+key,{'interlock':key,'state':v['state'],'reason':v.get('reason')},['Выяснить причину срабатывания по актуальным данным.','Продолжение станка возможно только после устранения удержания.'])
        order={'critical':0,'high':1,'medium':2,'low':3}
        return sorted(out,key=lambda x:order[x['severity']])
