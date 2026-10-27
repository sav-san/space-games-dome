"""Documented lock controls; physical operator actions are not API grants."""
import re
from .control_policy import machine_problem

ACTIONS={'set_lock','lock_door','unlock_door'}
PROMPT='''Управление замками — доступная агенту операция, если она есть в controls.
thermal_insulation_01 — бокс FabLab: set_lock value={"lock":0 или 1,"locked":true или false}.
Пользовательская дверца 1 соответствует индексу 0, дверца 2 — индексу 1. Без номера уточни, какую из двух; «обе» — два шага.
thermal_insulation_02 — Химлаб: set_lock value={"locked":true или false}, оба замка.
access_control_01 — входные двери помещений: lock_door/unlock_door value=MAIN_AIRLOCK/FABLAB/STORAGE/SERVER_ROOM.
Дверца бокса FabLab и дверь помещения FABLAB — разные устройства, не подменяй одно другим.
По просьбе открыть дверцу подготовь доступную разблокировку, пояснив, что физически её открывает человек.
Не объявляй все операторские действия запрещёнными: выполни доступную часть через controls.
operator_controls сами по себе не дают прав control; не выдумывай open_door и не выдавай разблокировку за физическое открытие.
Для недоступной части укажи конкретное отсутствующее право, а не отказ от всей просьбы.'''


def validate(c):
    node,action,value=c['node_id'],c['action'],c.get('value')
    if action=='set_lock':
        if node=='thermal_insulation_01':
            if not isinstance(value,dict) or set(value)!={'lock','locked'} or type(value['lock']) is not int or value['lock'] not in (0,1) or type(value['locked']) is not bool:
                raise ValueError('FabLab: нужны lock=0 или 1 и locked=true или false')
        elif node=='thermal_insulation_02':
            if type(value) is not bool and not (isinstance(value,dict) and set(value)=={'locked'} and type(value['locked']) is bool):
                raise ValueError('Химлаб: нужны true/false или {locked: true/false}')
        else:raise ValueError('Неизвестный бокс для set_lock')
    elif node!='access_control_01' or not isinstance(value,str) or value not in ('MAIN_AIRLOCK','FABLAB','STORAGE','SERVER_ROOM'):
        raise ValueError('Укажите дверь MAIN_AIRLOCK/FABLAB/STORAGE/SERVER_ROOM узла access_control_01')


def locked(c):
    if c['action']!='set_lock':return c['action']=='lock_door'
    v=c['value'];return v if type(v) is bool else v['locked']


def guard(agent,c):
    n=agent.frame['nodes'][c['node_id']]
    if c['action']=='set_lock':
        doors=n.get('doors');indices=[c['value']['lock']] if c['node_id']=='thermal_insulation_01' else [0,1]
        if not isinstance(doors,list) or any(len(doors)<=i or doors[i] not in ('OPEN','CLOSED') for i in indices):
            raise ValueError('Положение выбранной дверцы не подтверждено телеметрией')
        if locked(c) and any(doors[i]!='CLOSED' for i in indices):raise ValueError('Перед запиранием нужно физически закрыть дверцу')
        if not locked(c) and c['node_id']=='thermal_insulation_01':
            problem=machine_problem(agent,'printer_3d_01')
            if problem:raise ValueError('Разблокировка бокса FabLab пока недоступна: '+problem)
            if any(i.get('node_id') in ('printer_3d_01','thermal_insulation_01') and i.get('category') in ('safety','interlock','conflicting_data','telemetry_quality') for i in agent.issues):
                raise ValueError('Перед разблокировкой проверьте защитные условия и противоречия данных FabLab')
    else:
        doors=n.get('doors');door=doors.get(c['value']) if isinstance(doors,dict) else None
        if not isinstance(door,dict) or door.get('state') not in ('OPEN','CLOSED') or type(door.get('locked')) is not bool:
            raise ValueError('Состояние выбранной двери не подтверждено телеметрией')
        if n.get('lockdown') is True and not locked(c):raise ValueError('Серверный lockdown активен; обычная разблокировка его не обходит')
        if locked(c) and door['state']!='CLOSED':raise ValueError('Перед запиранием нужно физически закрыть дверь')


def expected(c):
    if c['action']=='set_lock':
        if c['node_id']=='thermal_insulation_01':return {'lock_index':c['value']['lock'],'state':'LOCKED' if locked(c) else 'UNLOCKED'}
        return {'lock':'LOCKED' if locked(c) else 'UNLOCKED'}
    return {'door':c['value'],'locked':locked(c)}


def observed(c,n):
    if c['action']=='set_lock':return {k:n.get(k) for k in ('locks','lock','doors','updated_at','status')}
    doors=n.get('doors',{})
    return {'door':c['value'],'state':doors.get(c['value']) if isinstance(doors,dict) else None,'updated_at':n.get('updated_at'),'status':n.get('status')}


def verified(c,n):
    target='LOCKED' if locked(c) else 'UNLOCKED'
    if c['action']=='set_lock':
        if c['node_id']=='thermal_insulation_01':
            values=n.get('locks');index=c['value']['lock']
            return isinstance(values,list) and len(values)>index and values[index]==target
        return n.get('lock')==target
    doors=n.get('doors');door=doors.get(c['value']) if isinstance(doors,dict) else None
    return isinstance(door,dict) and door.get('locked') is locked(c)


def direct_request(text):
    """Only whole, immediate, unambiguous imperative requests bypass the LLM."""
    s=text.lower().strip().rstrip('.!').replace('ё','е')
    m=re.fullmatch(r'(?:пожалуйста,? )?(открой|разблокируй|закрой|заблокируй) (?:обе (?:дверцы|двери)|(?:дверцу|замок)(?: (?:номер |№\s*)?[12])?) (?:бокса )?(?:fablab|фаблаб)(?: (?:номер |№\s*)?([12]))?',s)
    if not m:return None
    is_locked=m[1] in ('закрой','заблокируй')
    numbers=re.findall(r'[12]',s)
    if len(numbers)>1:return None
    indices=[0,1] if 'обе ' in s else ([int(numbers[0])-1] if numbers else [])
    if not indices:return {'answer':'У бокса FabLab две дверцы. Укажите «дверцу 1», «дверцу 2» или «обе дверцы». Я могу управлять замками; физическое открытие и закрытие выполняет человек.','commands':[]}
    return {'answer':('Подготовлено запирание' if is_locked else 'Подготовлена разблокировка')+' замка(ов) дверцы '+', '.join(str(i+1) for i in indices)+'. Подтвердите план кнопкой. Это управление замком: физически открывает или закрывает дверцу человек.',
            'commands':[{'node_id':'thermal_insulation_01','action':'set_lock','value':{'lock':i,'locked':is_locked},'delay_seconds':0} for i in indices]}
