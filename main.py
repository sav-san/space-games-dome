import argparse
import asyncio
import datetime as dt
import json
import os
import signal
import time
from pathlib import Path
from dome_agent import __version__
from dome_agent.core import Detector, Search, Log, env_file, save_json, number
from dome_agent.llm import LLM
from dome_agent.client import DomeClient
from dome_agent.trends import Trends

ROOT=Path(__file__).resolve().parent


class Agent:
    def __init__(self,log,knowledge,state):
        self.log=log;self.search=Search(knowledge);self.state=Path(state);self.detector=Detector(float(os.getenv('AGENT_STALE_SECONDS','20')))
        self.llm=LLM(log);self.last_received=0;self.generation=0;self.role=None;self.frame=None;self.issues=[];self.signature=None;self.last_observation=0;self.catalog=[]
        self.ui_callback=None;self.ui_diagnosis=None;self.ui_diagnosis_time=0;self.ui_signature=None;self.ui_stamp=None;self.ui_llm_status="Ожидание анализа"
        self.trends=Trends();self.last_analysis_start=0
        self.next_analysis=0;self.last_timestamp=None;self.server_changed=time.monotonic()

    def emit_ui(self):
        if not self.ui_callback:return
        fresh=self.role=="team" and self.frame is not None and time.monotonic()-self.last_received<=15 and time.monotonic()-self.server_changed<=20
        diagnosis=self.ui_diagnosis if fresh and self.ui_signature==self.signature and time.monotonic()-self.ui_diagnosis_time<120 else None
        self.ui_callback({"role":self.role,"fresh":fresh,"frame":self.frame,"issues":self.issues if fresh else [],"catalog":self.catalog if self.role=="team" else [],"diagnosis":diagnosis,"diagnosis_timestamp":self.ui_stamp if diagnosis else None,"llm_status":self.ui_llm_status})

    def status(self,role,generation):
        self.role=role;self.generation=generation;self.frame=None;self.last_received=0;self.issues=[];self.signature=None;self.last_timestamp=None
        self.trends=Trends();self.trend_stamp=None;self.trend_issues=[]
        self.ui_diagnosis=None;self.ui_llm_status='Ожидание анализа'
        self.detector=Detector(float(os.getenv('AGENT_STALE_SECONDS','20')))
        self.emit_ui()
        save_json(self.state/'latest.json',{'status':role,'generation':generation,'current_telemetry':False,'diagnostics':[]})

    def receive(self,m,generation):
        timestamp=m.get('timestamp')
        if not number(timestamp):
            self.log.write('error',where='telemetry',error='Missing finite server timestamp');return
        if self.last_timestamp is not None and timestamp<self.last_timestamp:
            self.log.write('error',where='telemetry',error='Out-of-order server timestamp; ignored');return
        if timestamp!=self.last_timestamp:self.server_changed=time.monotonic()
        self.last_timestamp=timestamp
        self.last_received=time.monotonic();self.generation=generation
        raw=m['data']['nodes']
        nodes={k:v for k,v in raw.items() if isinstance(k,str) and isinstance(v,dict)}
        self.issues=self.detector.analyze(nodes,timestamp)
        # Reuse trend hints on repeated frames; update only on a new server timestamp.
        if getattr(self,'trend_stamp',None)!=timestamp:
            self.trend_issues=self.trends.analyze(nodes,timestamp,self.issues);self.trend_stamp=timestamp
        self.issues+=getattr(self,'trend_issues',[])
        order={'critical':0,'high':1,'medium':2,'low':3}
        self.issues.sort(key=lambda x:order[x['severity']])
        self.frame={'timestamp':timestamp,'game_time':m['data'].get('game_time'),'nodes':nodes}
        sig=tuple((x['id'],x['severity']) for x in self.issues)
        if sig!=self.signature:
            self.next_analysis=min(self.next_analysis,max(time.monotonic(),self.last_analysis_start+15))
        if sig!=self.signature or time.monotonic()-self.last_observation>=30:
            self.signature=sig;self.last_observation=time.monotonic()
            self.log.write('observation',summary=f'Найдено признаков: {len(self.issues)}',metrics={'node_count':len(nodes),'server_timestamp':timestamp},anomalies=self.issues)
            self.publish()
        self.emit_ui()

    def publish(self):
        report={'status':'diagnostic_only','current_telemetry':True,'received_at':dt.datetime.now().astimezone().isoformat(),'server_timestamp':self.frame['timestamp'],'game_time':self.frame['game_time'],'valid_for_seconds':15,'diagnostics':self.issues,'llm':None,'note':'Автодиагностика не управляет оборудованием; команды доступны отдельно через подтверждение в чате GUI.'}
        save_json(self.state/'latest.json',report)
        print(f'[{report["received_at"]}] Признаков: {len(self.issues)}; '+ '; '.join(x['summary'] for x in self.issues[:3]),flush=True)
        return report

    async def worker(self,client):
        interval=max(15,float(os.getenv('AGENT_ANALYSIS_INTERVAL','60')))
        while True:
            await asyncio.sleep(1)
            self.catalog=client.catalog
            self.emit_ui()
            if self.role!='team' or self.frame is None:continue
            if time.monotonic()-self.last_received>15 or time.monotonic()-self.server_changed>20:
                save_json(self.state/'latest.json',{'status':'telemetry_timeout','current_telemetry':False,'diagnostics':[]})
                if time.monotonic()-self.last_observation>30:
                    self.log.write('observation',summary='Телеметрия не приходит более 15 секунд',metrics={},anomalies=['stream_timeout']);self.last_observation=time.monotonic()
                continue
            if time.monotonic()<self.next_analysis or self.llm.chat_active:continue
            self.last_analysis_start=time.monotonic()
            self.next_analysis=self.last_analysis_start+interval
            captured=json.loads(json.dumps(self.issues))
            compact={}
            keys=('state','status','online','updated_at','error_code','fault_code','hold_reason','sensor_state','confidence','reading_delay_s','co2_ppm','soc_pct','power_w','temperature_c','humidity_pct','progress_pct','webhook_state','clock_offset_s','link_state','packet_loss_pct','airflow_m3_h','fan_state','alarm','alarm_state','smoke_detected','dose_rate_usv_h','concentration_ppm','pressure_bar','level_pct','available','reception','grid_state','load_power_w','grid_power_w','power_deficit_w','inverter_temp_c','wind_speed_ms')
            for node,v in self.frame['nodes'].items():
                if node!='meeting_01':compact[node]={k:value for k,value in v.items() if 'history' not in k and k not in ('recent_messages','passage_events','files','last_gcode')}
            if not captured:
                captured=[{'id':'PERIODIC_REVIEW','node_id':'station','code':'REVIEW','summary':'Периодический обзор телеметрии, явных тревог правилами не найдено','evidence':{'snapshot_timestamp':self.frame['timestamp']},'recommended_steps':[]}]
            sig=self.signature;gen=self.generation;stamp=self.frame['timestamp'];began=time.monotonic()
            query=' '.join(x['node_id']+' '+x['code']+' '+x['summary'] for x in captured[:8])
            context=self.search.for_issues(captured,8)
            ids=[c['id'] for c in context]
            related={x['node_id'] for x in captured[:8]}
            catalog=[{k:n.get(k) for k in ('node_id','controls','operator_controls','service_actions')} for n in client.catalog if isinstance(n,dict) and n.get('node_id') in related]
            self.log.write('decision',situation='Диагностика наблюдаемых отклонений',cause='Причины не подтверждены',confidence=0.5,evidence=captured[:8],plan=['Сопоставить данные с документацией и историей'],source='rag',rag_refs=ids)
            self.ui_llm_status="Анализ DeepSeek…";self.emit_ui()
            diagnosis=await self.llm.diagnose(captured,context,catalog,compact)
            if self.role!='team' or gen!=self.generation or sig!=self.signature or time.monotonic()-self.last_received>15 or time.monotonic()-self.server_changed>20 or time.monotonic()-began>self.llm.timeout+10:
                self.ui_llm_status='Снимок изменился; ожидание нового анализа';self.emit_ui()
                self.log.write('error',where='diagnosis',error='Snapshot superseded; result not published');continue
            self.ui_diagnosis=diagnosis;self.ui_diagnosis_time=time.monotonic();self.ui_signature=sig;self.ui_stamp=stamp
            self.ui_llm_status="Ответ DeepSeek получен" if diagnosis else (self.llm.last_diagnosis_error or "Ответ диагностики не получен")+"; работают локальные правила"
            self.emit_ui()
            report=self.publish()
            report.update(rag_refs=ids,diagnosis_as_of_timestamp=stamp,llm=diagnosis,llm_status='ok' if diagnosis else 'error',llm_error=self.llm.last_diagnosis_error)
            save_json(self.state/'latest.json',report)
            self.log.write('decision',situation=(diagnosis or {}).get('summary','Локальные диагностические рекомендации'),cause='Гипотезы; требуется проверка',confidence=0.5,evidence=captured[:8],plan=(diagnosis or {}).get('hypotheses',[x['recommended_steps'] for x in captured[:8]]),source='mixed' if diagnosis else 'rules',rag_refs=ids)
            if diagnosis:print('DeepSeek: '+diagnosis['summary'],flush=True)


async def live(args,log):
    for key in ('DOME_URL','DOME_KEY','DEEPSEEK_API_KEY'):
        if not os.getenv(key):raise ValueError('Заполните '+key+' в .env')
    if not os.environ['DOME_URL'].startswith(('ws://','wss://')):raise ValueError('DOME_URL должен начинаться с ws:// или wss://')
    agent=Agent(log,ROOT/'knowledge/chunks.jsonl',args.state)
    agent.ui_callback=getattr(args,'ui_callback',None)
    agent.status('starting',0)
    client=DomeClient(os.environ['DOME_URL'],os.environ['DOME_KEY'],log,agent.receive,agent.status)
    stop=asyncio.Event();loop=asyncio.get_running_loop()
    for s in (signal.SIGTERM,signal.SIGINT):
        try:loop.add_signal_handler(s,stop.set)
        except (NotImplementedError,RuntimeError,ValueError):pass
    async def wait_stop():
        external=getattr(args,"stop_event",None)
        while not stop.is_set() and not (external and external.is_set()):
            await asyncio.sleep(.2)
    chat_task=None
    if getattr(args,'chat_inbox',None) is not None:
        from dome_agent.chat import Chat
        chat_task=asyncio.create_task(Chat(agent,client,args.chat_inbox,args.chat_output,getattr(args,'chat_cancel_event',None)).run())
    tasks=[asyncio.create_task(client.run()),asyncio.create_task(agent.worker(client)),asyncio.create_task(wait_stop())]
    if chat_task:tasks.insert(0,chat_task)
    try:
        done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is not tasks[-1]:t.result()
    finally:
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        agent.status('stopped',client.generation)


def replay(args,log):
    detector=Detector();trends=Trends();episodes=[];previous=set();count=0
    with open(args.replay,encoding='utf-8-sig') as f:
        for line in f:
            if not line.strip():continue
            row=json.loads(line);nodes=row.get('nodes',{});nodes={k:v for k,v in nodes.items() if isinstance(v,dict)}
            stamps=[v['updated_at'] for v in nodes.values() if number(v.get('updated_at'))]
            if not stamps:continue
            timestamp=sorted(stamps)[len(stamps)//2]
            issues=detector.analyze(nodes,timestamp);issues+=trends.analyze(nodes,timestamp,issues);present={x['id'] for x in issues};count+=1
            for x in issues:
                if x['id'] not in previous:episodes.append({'ts':row.get('ts'),'problem':x})
            previous=present
    report={'mode':'historical_replay','llm_called':False,'snapshots':count,'episodes':episodes}
    save_json(Path(args.state)/'replay_report.json',report)
    log.write('observation',summary='Исторический прогон завершён',metrics={'snapshots':count,'episodes':len(episodes)},anomalies=[])
    print(json.dumps({'snapshots':count,'episodes':len(episodes),'report':str(Path(args.state)/'replay_report.json')},ensure_ascii=False))


def main():
    env_file(ROOT/'.env')
    parser=argparse.ArgumentParser(description='Купол: диагностика телеметрии и рекомендации')
    parser.add_argument('--replay',help='Локальный telemetry.jsonl; без сети и LLM')
    parser.add_argument('--state',default=os.getenv('AGENT_STATE_DIR',str(ROOT/'state')))
    args=parser.parse_args();log=Log(os.getenv('AGENT_LOG_DIR',str(ROOT/'logs')))
    log.write('start',agent='dome-diagnostic-agent',version=__version__,models=[os.getenv('DEEPSEEK_MODEL','deepseek-v4-flash')],config={'mode':'replay' if args.replay else 'diagnostic_only','rag':'local_BM25'})
    reason='normal'
    try:
        if args.replay:replay(args,log)
        else:asyncio.run(live(args,log))
    except KeyboardInterrupt:reason='interrupt'
    except Exception as e:
        reason='error';log.write('error',where='main',error=type(e).__name__)
        print('Ошибка запуска:',log.clean(str(e)))
        raise SystemExit(1)
    finally:log.write('stop',reason=reason);log.close()

if __name__=='__main__':main()
