"""Document 03 log payloads (no credentials, no full snapshots)."""
import os
from . import __version__
from . import access


def start_fields(interface):
    return dict(agent='dome-diagnostic-agent',version=__version__,models=[os.getenv('DEEPSEEK_MODEL','deepseek-v4-flash')],config={'dome_url':os.getenv('DOME_URL',''),'interface':interface,'autonomy':os.getenv('AGENT_AUTONOMY','true'),'proposals':os.getenv('AGENT_PROPOSALS','true'),'rag':'local_BM25'})


def metrics(nodes,stamp):
    result={'node_count':len(nodes),'server_timestamp':stamp}
    fields={'battery_01':('soc_pct','voltage_v'),'printer_3d_01':('state','progress_pct','hold_reason'),'cnc_01':('state','progress_pct','hold_reason'),'supply_ventilation_01':('state','speed_pct','airflow_m3_h'),'equipment_cooling_01':('state','coolant_temp_c','coolant_flow_l_min')}
    fields.update({k:('co2_ppm','temperature_c','humidity_pct','reading_delay_s','status') for k in nodes if k.startswith('climate_sensor')})
    for node,keys in fields.items():
        n=nodes.get(node,{})
        if isinstance(n,dict):result[node]={k:n[k] for k in keys if k in n}
    return result


def expected(c,before):
    action=c['action'];value=c.get('value')
    if action in access.ACTIONS:return access.expected(c)
    if action in ('line_on','line_off'):return {'line':value,'state':'ON' if action=='line_on' else 'OFF'}
    if action=='pause':return {'state':'PAUSED'}
    if action=='abort':return {'state':['IDLE','CANCELLED','STOPPED'],'hold_reason':None}
    if action in ('start_job','resume'):return {'state':'PRINTING' if c['node_id']=='printer_3d_01' else 'RUNNING',**({'file_name':value} if action=='start_job' else {})}
    if action=='home':return {'homed':True,'state':['IDLE','COMPLETED']}
    if action=='set_speed':return {'speed_pct':value}
    if action in ('turn_on','turn_off'):
        on=action=='turn_on'
        if c['node_id']=='equipment_cooling_01':return {'state':['ON','RUNNING'],'coolant_flow_l_min':'>0'} if on else {'state':'OFF'}
        return {k:on for k in ('running','enabled') if type(before.get(k)) is bool} or {'running_or_enabled':on}
    if action=='set_zero':return {k+'_mm':0 for k in value}
    if action=='move':return {k+'_mm':value[k]+(before.get(k+'_mm',0) if value['mode']=='relative' else 0) for k in ('x','y','z') if k in value}
    return {'command':c}


def observed(c,n):
    if c['action'] in access.ACTIONS:return access.observed(c,n)
    if c['action'] in ('line_on','line_off'):
        line=next((x for x in n.get('lines',[]) if isinstance(x,dict) and x.get('line')==c['value']),None)
        return {'line':line,'updated_at':n.get('updated_at'),'status':n.get('status')}
    fields=('updated_at','online','status','state','hold_reason','homed','file_name','speed_pct','running','enabled','coolant_flow_l_min','x_mm','y_mm','z_mm','alarm','alarm_code')
    return {k:n[k] for k in fields if k in n}
