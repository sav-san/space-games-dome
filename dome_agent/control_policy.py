"""Device-specific power dependencies from current telemetry, never line numbers."""
from .core import number,node_time_error
MACHINES={'cnc_01','printer_3d_01'}
IDLE={'IDLE','COMPLETED','STOPPED','CANCELLED'}
STOP_ACTIONS={'pause','abort'}


def line_dependencies(line):
    name=str(line.get('name','')).lower()
    deps=set();known=False
    for terms,nodes in [
        (('чпу','cnc','фрезер'),{'cnc_01'}),
        (('принтер','printer','печать'),{'printer_3d_01'}),
        (('фаблаб','fablab'),{'printer_3d_01'}),
        (('вытяж','рабочее место','мастерск','workstation'),{'cnc_01','workstation'}),
        (('охлажден','cooling'),MACHINES),
        (('вентиляц','ventilation'),MACHINES),
    ]:
        if any(t in name for t in terms):deps.update(nodes);known=True
    if any(t in name for t in ('освещ','lighting','light','резерв','reserve','химлаб','chemlab','вод','water')):known=True
    return deps,known


def machine_problem(agent,node):
    n=agent.frame['nodes'].get(node,{})
    threshold=getattr(getattr(agent,'detector',None),'stale',20.0)
    if n.get('online') is not True or n.get('status')=='UNKNOWN' or node_time_error(n,agent.frame['timestamp'],threshold):
        return f'Нет свежего подтверждения состояния {node}'
    if any(i.get('node_id')==node and i.get('code') in ('STALE','MISSING') for i in agent.issues):return f'Данные {node} не обновляются'
    state=n.get('state')
    if state not in IDLE:return f'{node} находится в {state or "неизвестном состоянии"}'
    if node=='printer_3d_01' and any(number(n.get(k)) and n[k]>0 for k in ('nozzle_target_c','bed_target_c','heater_power_pct')):
        return 'У принтера включён нагрев'
    if node=='cnc_01' and any(number(n.get(k)) and n[k]>0 for k in ('spindle_rpm','spindle_target_rpm')):
        return 'Шпиндель ЧПУ ещё вращается или задана его работа'
    return None


def check_dependencies(agent,c,line=None):
    action=c['action'];node=c['node_id'];nodes=agent.frame['nodes']
    if action not in ('line_off','turn_off'):return
    if action=='line_off':
        if line.get('state')=='OFF':return
        deps,known=line_dependencies(line)
        if not known and any(machine_problem(agent,k) for k in MACHINES):
            raise ValueError(f"Назначение линии {line.get('line')} «{line.get('name')}» не распознано; уточните её связь с работающим оборудованием")
    else:
        deps={node} if node in MACHINES else {
            'equipment_cooling_01':MACHINES,
            'fume_extraction_01':{'cnc_01','workstation'},
            'supply_ventilation_01':MACHINES,
            'solar_inverter_01':MACHINES,
            'dizel_1':MACHINES if nodes.get('solar_inverter_01',{}).get('ac_source') in ('GENERATOR','DIESEL') else set(),
        }.get(node,set())
    for dep in sorted(deps):
        if dep=='workstation':
            panel=nodes.get('smart_panel_01',{})
            state=panel.get('workstation_mode',panel.get('workstation_state'))
            if state in ('SOLDERING','HOT_AIR'):
                raise ValueError('Эта линия/вытяжка обслуживает активное рабочее место '+state)
        else:
            reason=machine_problem(agent,dep)
            if reason:raise ValueError('Нельзя отключить питание/обслуживание этого устройства: '+reason+'. Другие независимые линии доступны. Для остановки используйте pause/abort, если сервер разрешает, либо оператора.')
    if line and 'аварийн' in str(line.get('name','')).lower() and any(i.get('severity')=='critical' for i in agent.issues):
        raise ValueError('Аварийное освещение требуется при текущей критической тревоге')
