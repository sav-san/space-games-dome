"""Desktop telemetry tables, diagnostics and reviewed chat control."""
import asyncio
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from types import SimpleNamespace
from main import ROOT, live
from dome_agent.core import env_file, Log
from dome_agent.presentation import GROUPS, records, field_title


def flatten(value, path=''):
    if isinstance(value, dict) and value:
        for k, v in value.items():
            yield from flatten(v, f'{path}.{k}' if path else str(k))
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            yield from flatten(v, f'{path}[{i}]')
    else:
        yield path, json.dumps(value, ensure_ascii=False)


def notifications(data):
    if not data.get('fresh'):
        return 'Актуальных данных нет. Ожидание подключения / своей смены или восстановление связи.\nСтарые значения не подтверждают текущее состояние.'
    lines=[]
    for issue in data.get('issues', []):
        lines += [f"[{issue.get('severity', '')}] {issue.get('node_id', '')}: {issue.get('summary', '')}",
                  'Факты: '+json.dumps(issue.get('evidence', {}), ensure_ascii=False),
                  'Проверки: '+ '; '.join(str(x) for x in issue.get('recommended_steps', [])), '']
    if not lines:lines=['Локальные правила не обнаружили отклонений.', '']
    d=data.get('diagnosis')
    if d:
        lines += ['АНАЛИЗ DEEPSEEK — возможные причины, требуют проверки',
                  'Снимок API: '+str(data.get('diagnosis_timestamp')),d.get('summary','')]+(['Набор признаков изменился после анализа; показаны только гипотезы по ещё активным признакам.'] if d.get('partial') else [])+['']
        for h in d.get('hypotheses',[]):
            lines += ['Возможная причина: '+h['cause'],
                      'Связанные признаки: '+', '.join(h['evidence_issue_ids']),
                      'Проверить: '+'; '.join(h['checks']),
                      'Предлагаемые действия: '+'; '.join(h['proposed_steps']),
                      'Источники RAG: '+', '.join(h['rag_refs']), '']
    else:lines += ['Актуальная гипотеза причины пока не получена. Причина не установлена.']
    lines += ['Статус LLM: '+data.get('llm_status','Ожидание')]
    return '\n'.join(lines)


class Window:
    def __init__(self, root):
        self.root=root;self.events=queue.Queue(maxsize=1);self.stop=threading.Event();self.chat_inbox=queue.Queue();self.chat_output=queue.Queue();self.chat_cancel_event=threading.Event();self.plan=None;self.data={};self.autonomy_disable_event=threading.Event();self.last_event=0;self.closing=False
        root.title('Купол — телеметрия и диагностика');root.geometry('1250x820');root.minsize(850,550)
        style=ttk.Style(root)
        style.configure('Telemetry.Treeview',background='#202630',fieldbackground='#202630',foreground='#e6edf3',rowheight=28)
        style.map('Telemetry.Treeview',foreground=[],background=[('selected','#384657')])
        self.status=tk.StringVar(value='Подключение…')
        ttk.Label(root,textvariable=self.status,wraplength=1200).pack(fill='x',padx=10,pady=8)
        tabs=ttk.Notebook(root);tabs.pack(fill='both',expand=True,padx=10,pady=5)
        panel=ttk.Frame(tabs);tabs.add(panel,text='Все показатели')
        self.query=tk.StringVar();ttk.Label(panel,text='Фильтр: узел, поле или значение').pack(anchor='w')
        ttk.Entry(panel,textvariable=self.query).pack(fill='x');self.query.trace_add('write',lambda *_:self.render_tree())
        self.group=tk.StringVar(value='Все');self.rows={};self.group_buttons={}
        categories=ttk.Frame(panel);categories.pack(fill='x',pady=6)
        for i,name in enumerate(GROUPS):
            b=ttk.Radiobutton(categories,text=name,value=name,variable=self.group,command=self.render_tree)
            b.grid(row=i//5,column=i%5,sticky='w',padx=6,pady=3);self.group_buttons[name]=b
        panes=ttk.Panedwindow(panel,orient='vertical');panes.pack(fill='both',expand=True)
        overview=ttk.Frame(panes);details=ttk.Frame(panes);panes.add(overview,weight=3);panes.add(details,weight=2)
        self.tree=self.make_table(overview,[('name','Узел (понятное название)',450),('state','Состояние',130),('level','Важность',140),('summary','Основные показатели',450)])
        self.tree.bind('<<TreeviewSelect>>',lambda _:self.render_details())
        ttk.Label(details,text='Красный — серьёзные ошибки · Оранжевый — обычные отклонения · Жёлтый — слабые предупреждения').pack(fill='x')
        self.detail_title=tk.StringVar(value='Выберите строку узла — все его показатели появятся в таблице ниже')
        ttk.Label(details,textvariable=self.detail_title,wraplength=1150).pack(fill='x',pady=5)
        self.details=self.make_table(details,[('field','Показатель (описание)',620),('value','Значение',500)])
        for table in (self.tree,self.details):
            for severity,color in [('critical','#ff5a5a'),('high','#ff5a5a'),('medium','#ffab40'),('low','#ffe45c')]:
                table.tag_configure(severity,foreground=color,background='#252b34')
        alerts=ttk.Frame(tabs);tabs.add(alerts,text='Отклонения и возможные причины');self.tabs=tabs;self.alert_tab=alerts
        self.alerts=ScrolledText(alerts,wrap='word',font=('Segoe UI',11));self.alerts.pack(fill='both',expand=True)
        for severity,color in [('critical','#ff5a5a'),('high','#ff5a5a'),('medium','#ffab40'),('low','#ffe45c')]:self.alerts.tag_configure(severity,foreground=color,background='#252b34')
        control=ttk.Frame(tabs);tabs.add(control,text='Возможности API');self.controls=ScrolledText(control,wrap='word');self.controls.pack(fill='both',expand=True)
        self.text(self.controls,'После подключения здесь появятся права из describe_nodes для принтера и ЧПУ. Для управления используйте вкладку чата.')
        chat=ttk.Frame(tabs);tabs.add(chat,text='Чат и управление');self.chat_tab=chat
        self.chat_view=ScrolledText(chat,wrap='word',state='disabled');self.chat_view.pack(fill='both',expand=True)
        self.plan_text=tk.StringVar(value='Нет подготовленной команды')
        ttk.Label(chat,textvariable=self.plan_text,wraplength=1100).pack(fill='x',padx=5,pady=5)
        buttons=ttk.Frame(chat);buttons.pack(fill='x')
        self.confirm_button=ttk.Button(buttons,text='Подтвердить показанный план',command=self.confirm,state='disabled');self.confirm_button.pack(side='left')
        ttk.Button(buttons,text='Отменить план / оставшиеся шаги',command=self.cancel_plan).pack(side='left')
        self.message=tk.StringVar();entry=ttk.Entry(chat,textvariable=self.message);entry.pack(fill='x',padx=4,pady=4);entry.bind('<Return>',lambda _:self.ask())
        ttk.Button(chat,text='Отправить',command=self.ask).pack(anchor='e')
        from dome_agent.autonomy import enabled_default
        self.auto_enabled=tk.BooleanVar(value=enabled_default())
        if not self.auto_enabled.get():self.autonomy_disable_event.set()
        def toggle_auto():
            if self.auto_enabled.get():self.autonomy_disable_event.clear()
            else:self.autonomy_disable_event.set()
            self.append_chat('Автономность '+('включена' if self.auto_enabled.get() else 'выключена')+'. Уже отправленная команда может завершиться.')
        ttk.Checkbutton(chat,text='Автономные действия при высокой уверенности (отчёты в чате)',variable=self.auto_enabled,command=toggle_auto).pack(anchor='w')
        ttk.Label(root,text='Показатели обновляются автоматически. Причины — гипотезы. Закрытие окна останавливает этого агента.').pack(pady=4)
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
        root.protocol('WM_DELETE_WINDOW',self.close);root.after(200,self.poll)

    def send(self,data):
        # Detach mutable agent data before crossing the thread boundary.
        item=json.loads(json.dumps(data,ensure_ascii=False))
        try:self.events.get_nowait()
        except queue.Empty:pass
        self.events.put_nowait(item)

    def run(self):
        log=None
        try:
            log=Log(os.getenv('AGENT_LOG_DIR',str(ROOT/'logs')))
            log.write('start',agent='dome-diagnostic-agent',interface='tkinter')
            args=SimpleNamespace(state=os.getenv('AGENT_STATE_DIR',str(ROOT/'state')),ui_callback=self.send,stop_event=self.stop,chat_inbox=self.chat_inbox,chat_output=self.chat_output.put,chat_cancel_event=self.chat_cancel_event,autonomy_disable_event=self.autonomy_disable_event)
            asyncio.run(live(args,log))
        except Exception as e:
            msg=log.clean(str(e)) if log else type(e).__name__
            self.send({'error':msg,'fresh':False})
            if log:log.write('error',where='gui_agent',error=msg)
        finally:
            if log:log.write('stop',reason='gui_closed_or_error');log.close()

    @staticmethod
    def text(widget,value):
        if getattr(widget,'last_text',None)==value:return
        pos=widget.yview();widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',value);widget.configure(state='disabled');widget.yview_moveto(pos[0]);widget.last_text=value

    def append_chat(self,text):
        self.chat_view.configure(state="normal");self.chat_view.insert("end",text+"\n\n");self.chat_view.see("end");self.chat_view.configure(state="disabled")

    def ask(self):
        text=self.message.get().strip()
        if not text:return
        self.message.set("");self.plan=None;self.confirm_button.configure(state="disabled");self.plan_text.set("Ожидание ответа…")
        self.append_chat("Вы: "+text);self.chat_inbox.put({"kind":"ask","text":text})

    def confirm(self):
        if self.plan:
            self.chat_cancel_event.clear()
            self.chat_inbox.put({"kind":"confirm","id":self.plan["id"]});self.plan=None;self.confirm_button.configure(state="disabled");self.plan_text.set("Подтверждение отправлено")

    def cancel_plan(self):
        self.chat_cancel_event.set()
        self.plan=None;self.confirm_button.configure(state="disabled");self.plan_text.set("Отмена отправлена");self.chat_inbox.put({"kind":"cancel"})

    @staticmethod
    def make_table(parent,columns):
        body=ttk.Frame(parent);body.pack(fill='both',expand=True)
        table=ttk.Treeview(body,columns=[x[0] for x in columns],show='headings',selectmode='browse',style='Telemetry.Treeview')
        for key,title,width in columns:table.heading(key,text=title);table.column(key,width=width,minwidth=90)
        ys=ttk.Scrollbar(body,orient='vertical',command=table.yview);xs=ttk.Scrollbar(body,orient='horizontal',command=table.xview)
        table.configure(yscrollcommand=ys.set,xscrollcommand=xs.set)
        table.grid(row=0,column=0,sticky='nsew');ys.grid(row=0,column=1,sticky='ns');xs.grid(row=1,column=0,sticky='ew')
        body.rowconfigure(0,weight=1);body.columnconfigure(0,weight=1)
        return table

    def render_tree(self):
        if not hasattr(self,'tree'):return
        all_rows=records(self.data);self.rows={r['id']:r for r in all_rows}
        query=self.query.get().strip().lower();wanted=set()
        for category,b in self.group_buttons.items():
            count=len(all_rows) if category=='Все' else sum(r['group']==category for r in all_rows)
            b.configure(text=f'{category} ({count})')
        for row in all_rows:
            if self.group.get()!='Все' and row['group']!=self.group.get():continue
            searchable=row['name']+' '+row['group']+' '+row['state']+' '+row['issues']+' '+' '.join(field_title(k)+' '+str(v) for k,v in row['fields'])
            if query not in searchable.lower():continue
            iid=row['id'];wanted.add(iid)
            vals=tuple(row[k] for k in ('name','state','level','summary'))
            tags=(row['severity'],) if row['severity'] else ()
            if self.tree.exists(iid):self.tree.item(iid,values=vals,tags=tags)
            else:self.tree.insert('','end',iid=iid,values=vals,tags=tags)
        for iid in self.tree.get_children():
            if iid not in wanted:self.tree.delete(iid)
        if not self.tree.selection() and self.tree.get_children():self.tree.selection_set(self.tree.get_children()[0])
        self.render_details()

    def render_details(self):
        selected=self.tree.selection();row=self.rows.get(selected[0]) if selected else None
        self.detail_title.set((row['name']+' — '+(row['issues'] or row['level'])) if row else 'Нет узлов по текущему фильтру')
        fields=row['fields'] if row else [];wanted=set()
        for key,value in fields:
            iid=json.dumps(key);wanted.add(iid);vals=(field_title(key),value)
            tags=(row['severity'],) if row['severity'] else ()
            if self.details.exists(iid):self.details.item(iid,values=vals,tags=tags)
            else:self.details.insert('','end',iid=iid,values=vals,tags=tags)
        for iid in self.details.get_children():
            if iid not in wanted:self.details.delete(iid)

    def poll(self):
        while True:
            try:event=self.chat_output.get_nowait()
            except queue.Empty:break
            self.append_chat("Агент: "+event["text"])
            if event.get("autonomous"):continue
            if (event.get("plan") or {}).get("origin")=="proactive_proposal":self.tabs.select(self.chat_tab)
            self.plan=event.get("plan");self.confirm_button.configure(state="normal" if self.plan else "disabled")
            self.plan_text.set(("ПРОВЕРЬТЕ ПЛАН:\n"+"\n".join(f"{i}. {c['node_id']} → {c['action']} ({json.dumps(c.get('value'),ensure_ascii=False)}), задержка {c.get('delay_seconds',0)} с" for i,c in enumerate(self.plan.get('commands',[self.plan]),1))) if self.plan else "Нет новой команды на подтверждение")
        try:self.data=self.events.get_nowait();self.last_event=time.monotonic()
        except queue.Empty:pass
        if self.last_event and time.monotonic()-self.last_event>15:self.data['fresh']=False
        frame=self.data.get('frame') or {};count=len(frame.get('nodes',{}));fresh=self.data.get('fresh',False)
        self.status.set(f"{os.getenv('DOME_URL','')} | Роль: {self.data.get('role','—')} | {'Данные актуальны' if fresh else 'НЕТ АКТУАЛЬНЫХ ДАННЫХ'} | Узлов: {count} | Время API: {frame.get('timestamp','—')} | Игровое время: {frame.get('game_time','—')}")
        self.render_tree();self.text(self.alerts,('Ошибка: '+self.data['error']) if self.data.get('error') else notifications(self.data))
        for tag in ('critical','high','medium','low'):self.alerts.tag_remove(tag,'1.0','end')
        lines=self.alerts.get('1.0','end').splitlines();tag=None
        for i,line in enumerate(lines,1):
            if line.startswith('['):tag=line[1:].split(']',1)[0]
            elif not line:tag=None
            if tag in ('critical','high','medium','low'):self.alerts.tag_add(tag,f'{i}.0',f'{i}.end')
        n=len(self.data.get('issues',[])) if fresh else 0
        self.tabs.tab(self.alert_tab,text=f'Отклонения ({n}) и возможные причины')
        catalog=[n for n in self.data.get('catalog',[]) if isinstance(n,dict) and n.get('node_id') in ('cnc_01','printer_3d_01')]
        info='Текущий каталог describe_nodes. Это объявленные права, не проверка выполнения команд.\nУправление доступно в чате после подтверждения конкретной команды.\n\n'
        info+=json.dumps(catalog,ensure_ascii=False,indent=2) if catalog else 'Каталог устройств пока не получен или устройства не представлены.'
        info+='\n\nОстановка производства по текущему каталогу:\n'
        for node_id in ('printer_3d_01','cnc_01'):
            node=next((x for x in catalog if x.get('node_id')==node_id),None)
            actions=[a for a in ('pause','abort') if node and a in node.get('controls',[])]
            info+=node_id+': '+(', '.join(actions) if actions else 'каталог не получен' if node is None else 'pause/abort сервером не предоставлены; нужен оператор или изменение серверных прав')+'\n'
        self.text(self.controls,info)
        if not self.closing:self.root.after(500,self.poll)

    def close(self):
        self.closing=True;self.autonomy_disable_event.set();self.chat_cancel_event.set();self.stop.set();self.status.set('Остановка агента…');self.wait_close()

    def wait_close(self):
        if self.thread.is_alive():self.root.after(100,self.wait_close)
        else:self.root.destroy()


if __name__=='__main__':
    env_file(ROOT/'.env')
    root=tk.Tk();Window(root);root.mainloop()
