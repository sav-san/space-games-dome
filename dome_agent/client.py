import asyncio
import json
import time
import uuid
import websockets


class DomeClient:
    def __init__(self,url,key,log,on_telemetry,on_status):
        self.url=url;self.key=key;self.log=log;self.on_telemetry=on_telemetry;self.on_status=on_status
        self.role=None;self.team=None;self.catalog=[];self.pending={};self.ws=None;self.last_send=0;self.lock=asyncio.Lock();self.generation=0

    async def request(self, kind):
        # Generic requests are read-only; reviewed controls use the separate control method.
        if kind not in ('describe_nodes','ping'):raise ValueError('Diagnostic client only permits read-only requests')
        return await self._request(kind,{})

    async def refresh_catalog(self):
        if self.role!="team":raise ValueError("Каталог управления доступен только роли team")
        result=await self.request("describe_nodes")
        if result.get("success") is not True or not isinstance(result.get("nodes"),list):
            self.catalog=[]
            raise ValueError("Сервер не вернул актуальный каталог прав; команды не отправлены")
        self.catalog=result["nodes"]
        return self.catalog

    async def control(self,node_id,action,value):
        from .chat import validate_command
        validate_command({"node_id":node_id,"action":action,"value":value},self.catalog)
        if self.role!="team":raise ValueError("Only team may control devices")
        return await self._request("control",{"node_id":node_id,"action":action,"value":value})

    async def _request(self,kind,fields):
        rid=uuid.uuid4().hex
        req={'type':kind,'request_id':rid,**fields}
        dec=(getattr(self,'control_decision_ref',None) if kind=='control' else None) or self.log.write('decision',situation='Управление устройством' if kind=='control' else 'Чтение состояния интерфейса',cause='Запрос через API',confidence=1.0,evidence=[],plan=[kind],source='rules')
        fut=asyncio.get_running_loop().create_future();self.pending[rid]=fut
        try:
            async with self.lock:
                await asyncio.sleep(max(0,.3-(time.monotonic()-self.last_send)))
                await self.ws.send(json.dumps(req));self.last_send=time.monotonic()
            response=await asyncio.wait_for(fut,10)
            action_ref=self.log.write('action',request=req,response=response,ref=dec)
            if kind=='control':self.last_control_action_ref=action_ref
            return response
        except (Exception,asyncio.CancelledError) as e:
            action_ref=self.log.write('action',request=req,response={'success':None,'error':type(e).__name__},ref=dec)
            if kind=='control':self.last_control_action_ref=action_ref
            raise
        finally:self.pending.pop(rid,None)

    async def reader(self):
        async for raw in self.ws:
            try:
                m=json.loads(raw)
                if not isinstance(m,dict):raise ValueError()
            except (ValueError,TypeError):
                self.log.write('error',where='websocket_json',error='Invalid JSON object');continue
            kind=m.get('type')
            if kind=='session_revoked':
                self.log.write('connection',event='revoked',details=m.get('reason'));return
            if kind=='telemetry':
                if m.get('role','team')!='team' or self.role!='team':continue
                data=m.get('data')
                if isinstance(data,dict) and isinstance(data.get('nodes'),dict):
                    self.on_telemetry(m,self.generation)
            elif kind=='meeting_message':
                self.log.write('message',direction='in',sender=(m.get('message') or {}).get('sender'),text=(m.get('message') or {}).get('text'))
            else:
                rid=m.get('request_id')
                if isinstance(rid,(str,int)) and rid in self.pending and not self.pending[rid].done():self.pending[rid].set_result(m)

    async def session(self):
        self.generation+=1
        async with websockets.connect(self.url,max_size=32*1024*1024,open_timeout=10,ping_interval=20,ping_timeout=20) as ws:
            self.ws=ws;self.log.write('connection',event='connected',details='WebSocket connected')
            auth_req={'type':'auth','key':'[REDACTED]'}
            dec=self.log.write('decision',situation='Авторизация в Куполе',cause='Начало сессии',confidence=1.0,evidence=[],plan=['auth'],source='rules')
            try:
                await ws.send(json.dumps({'type':'auth','key':self.key}))
                auth=json.loads(await asyncio.wait_for(ws.recv(),10))
                if not isinstance(auth,dict):raise ValueError('Invalid authentication response')
                self.log.write('action',request=auth_req,response=auth,ref=dec)
            except (Exception,asyncio.CancelledError) as e:
                self.log.write('action',request=auth_req,response={'success':None,'error':type(e).__name__},ref=dec);raise
            if auth.get('type')!='auth_response' or auth.get('success') is not True:raise RuntimeError('Authentication rejected')
            self.role=auth.get('role');self.team=auth.get('team');self.catalog=[]
            self.log.write('connection',event='auth',role=self.role,team=self.team,details='authenticated')
            self.on_status(self.role,self.generation)
            reader=asyncio.create_task(self.reader())
            try:
                if self.role=='team':
                    result=await self.request('describe_nodes')
                    self.catalog=result.get('nodes',[]) if result.get('success') and isinstance(result.get('nodes'),list) else []
                    await reader
                else:
                    # Reauthorize periodically when waiting for our shift.
                    await asyncio.wait({reader},timeout=20)
            finally:
                reader.cancel();await asyncio.gather(reader,return_exceptions=True)

    async def run(self):
        failures=0
        while True:
            started=time.monotonic()
            try:await self.session()
            except asyncio.CancelledError:raise
            except Exception as e:self.log.write('error',where='connection',error=type(e).__name__)
            finally:
                self.role=None;self.catalog=[];self.ws=None
                self.on_status('disconnected',self.generation)
                for f in self.pending.values():
                    if not f.done():f.set_exception(ConnectionError('Disconnected'))
                self.pending.clear()
                self.log.write('connection',event='disconnected',details='Reconnect scheduled')
            failures=0 if time.monotonic()-started>15 else min(failures+1,4)
            await asyncio.sleep(min(30,3*2**failures))
