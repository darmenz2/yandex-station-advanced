"""Declarative native tray menu. Layout and actions are testable without Windows.
No HTML/WebView/browser, no account credentials in this model's persistence.
"""
from __future__ import annotations
from dataclasses import dataclass,field
import copy
import time
from .spatial import config,ROLES,LAYOUTS

BG='#141414';CARD='#252525';TEXT='#f7f7f7';MUTED='#a5a5a5';YELLOW='#ffdc3a';LINE='#383838'
@dataclass
class Item:
    kind:str;x:int;y:int;w:int;h:int;text:str='';action:str='';data:dict=field(default_factory=dict)
    value:float=0.;minimum:float=0.;maximum:float=1.;accent:bool=False;small:bool=False

class Model:
    def __init__(self,dispatch,confirm):
        self.dispatch=dispatch;self.confirm=confirm;self.state={};self.page='home';self.scroll=0
        self.selected=set();self.draft=config();self.editing=False;self.peer_id='';self.preset={'left':'','bass':'','right':''}
        self.pick_key='';self.pick_options=[];self.pick_return='';self.remember=True;self.mic=None
        self.mode={'playback_mode':'main'};self.error='';self.error_until=0;self.nodes=[];self.total=0
    def update(self,state):
        self.state=state or {}
        if not self.editing:self.draft=copy.deepcopy(self.state.get('spatial',config()))
    def toast(self,text):self.error=text;self.error_until=time.monotonic()+15
    def goto(self,page):self.page=page;self.scroll=0
    def peers(self):return self.state.get('desktop',{}).get('peers',[])
    def emit(self,command,data=None,advanced=False):self.dispatch('advanced' if advanced else command,{'command':command,**(data or {})} if advanced else (data or {}))
    def choose(self,key,options,back):self.pick_key=key;self.pick_options=options;self.pick_return=back;self.goto('pick')
    def act(self,action,data=None,value=None):
        data=data or {}
        if action=='page':
            if data['page']=='channels' and not self.editing:self.draft=copy.deepcopy(self.state.get('spatial',config()));self.editing=True
            if data['page']=='mic':self.emit('mic_list',advanced=True)
            self.goto(data['page']);return
        if action=='send':self.emit(data['command'],data.get('data'),data.get('advanced',False));return
        if action=='remember':self.remember=not self.remember;return
        if action=='qr':self.goto('qr');self.emit('qr',{'remember':self.remember});return
        if action=='select':
            if data['id'] in self.selected:self.selected.remove(data['id'])
            else:self.selected.add(data['id'])
            return
        if action=='all':self.selected={d['id'] for d in self.state.get('devices',[]) if d.get('host')};return
        if action=='connect':
            devices=[d for d in self.state.get('devices',[]) if d['id'] in self.selected]
            self.emit('connect_group',{'devices':devices},True);return
        if action=='trust':
            trust=self.state.get('desktop',{}).get('group_trust',[])
            text='Подтвердите IP и отпечатки именно своих Станций:\n\n'+'\n\n'.join(t['device']['name']+' · '+t['device']['host']+'\n'+t['fingerprint'] for t in trust)
            if self.confirm(text):
                devices=[d for d in self.state.get('devices',[]) if d['id'] in self.selected]
                self.emit('connect_group',{'devices':devices,'accepted':{t['device']['id']:t['fingerprint'] for t in trust}},True)
            return
        if action=='approve':
            if self.confirm('Разрешить аудио через '+data['ip']+'? Подтверждайте только адрес своего доверенного роутера. Это не откроет доступ всей сети.'):
                self.emit('peer_approve',data,True)
            return
        if action=='play':self.emit('live',self.mode);return
        if action=='master':self.emit('master_volume',{'value':value},True);return
        if action=='station':self.emit('station_volume',{'id':data['id'],'value':value},True);return
        if action=='prepare':
            if self.confirm('Создать выход «Яндекс станция»? Используется отдельный VB-CABLE (лицензия VB-Audio). Если он не установлен, откроется его оригинальный мастер. Установка/переименование потребуют UAC, возможно перезагрузки. Защита Windows не отключается.'):
                self.emit('prepare_output',{'confirmed':True},True)
            return
        if action=='pick':
            key=data['key'];options=data['options'];self.choose(key,options,data.get('back',self.page));return
        if action=='picked':
            k=self.pick_key;v=data['value']
            if k in self.preset:self.preset[k]=v
            elif k=='role':self.draft['speakers'].setdefault(self.peer_id,{'role':'stereo'})['role']=v
            elif k=='layout':self.draft['layout']=v;self.draft['speakers']={}
            elif k=='mic':self.mic=v
            elif k=='mode':self.mode=v
            self.goto(self.pick_return);return
        if action=='preset':
            self.goto('preset');return
        if action=='apply21':
            self.emit('preset21',self.preset,True);self.editing=False;self.goto('channels');return
        if action=='toggle_spatial':self.draft['enabled']=not self.draft['enabled'];self.editing=True;return
        if action=='crossover':self.draft['crossover_hz']=round(value);self.editing=True;return
        if action=='editpeer':self.peer_id=data['id'];self.draft['speakers'].setdefault(self.peer_id,{'role':'stereo','gain_db':0,'delay_ms':0,'invert':False});self.goto('peer');return
        if action in ('gain','delay','invert','delay_step'):
            row=self.draft['speakers'][self.peer_id]
            if action=='gain':row['gain_db']=round(value,1)
            elif action=='delay':row['delay_ms']=round(value,1)
            elif action=='delay_step':row['delay_ms']=max(0,min(1000,row.get('delay_ms',0)+data['value']))
            else:row['invert']=not row.get('invert',False)
            self.editing=True;return
        if action=='save':self.emit('spatial_save',{'config':self.draft},True);self.editing=False;return
        if action=='room_start':
            if self.mic is None:self.toast('Выберите микрофон.');return
            if self.confirm('Временно включить выбранный микрофон? Остановите музыку, оставьте микрофон у места прослушивания. Прозвучат негромкие сигналы по очереди. Аудио не сохраняется и не отправляется в интернет. Закрытие меню отменяет замер. Результат — приблизительное выравнивание задержек, не room-EQ и не фаза баса.'):
                self.emit('room_start',{'index':self.mic,'confirmed':True},True)
            return
        if action=='room_apply':self.emit('room_apply',advanced=True);return
        if action=='silence':self.emit('continuity',{'enabled':not self.state.get('continuity',{}).get('enabled',True)});return
        if action=='policy':self.emit('refresh_policy',{'seconds':data['seconds']},True);return
    def build(self,width=430):
        self.nodes=[];self.y=78;self.width=width;self.inner=width-40
        self.nodes.append(Item('header',20,18,self.inner,46,'Яндекс станция',small=False))
        if self.page!='home':self.nodes.append(Item('button',width-90,20,70,32,'Назад','page',{'page':'advanced' if self.page in ('channels','mic','output','stream') else 'channels' if self.page in ('preset','peer') else 'home'},small=True))
        if self.error and time.monotonic()<self.error_until:self.label(self.error,68)
        s=self.state;desktop=s.get('desktop',{});live=s.get('media',{}).get('kind')=='live'
        if self.page=='home':
            self.label('ADVANCED  ·  неофициальное приложение',24)
            self.button(('■  Остановить' if live else '▶  Передавать звук'), 'send' if live else 'play', {'command':'stop'} if live else {},accent=True)
            self.button('К живому звуку','send',{'command':'resync'})
            master=desktop.get('native_audio',{}).get('selected') or {}
            if master:
                self.label('Громкость Windows · '+master.get('name',''),30)
                self.slider('Общий уровень','master',master.get('volume',0),0,1)
                self.button('Включить звук' if master.get('mute') else 'Без звука', 'send',{'command':'master_mute','advanced':True,'data':{'value':not master.get('mute',False)}})
            else:self.button('Создать выход «Яндекс станция»','page',{'page':'output'})
            for p in self.peers():
                d=p['device'];row=s.get('spatial',{}).get('speakers',{}).get(d['id'],{})
                self.label(d['name']+'  ·  '+ROLES.get(row.get('role','stereo'),'Стерео'),28)
                self.slider('Громкость колонки','station',p['state'].get('volume',0) or 0,0,1,{'id':d['id']})
                approval=p.get('peer_approval')
                if approval:self.button('Разрешить аудио через '+approval.get('ip','?'),'approve',{'id':d['id'],'ip':approval.get('ip',''),'challenge':approval.get('challenge','')})
            self.button('Колонки · выбрать одну или все','page',{'page':'connect'})
            self.button('Аккаунт · QR-вход','page',{'page':'qr'})
            self.button('Advanced menu  →','page',{'page':'advanced'})
            self.button('Выход','send',{'command':'shutdown'})
        elif self.page=='qr':
            auth=s.get('auth',{});self.label('Вход в аккаунт владельца колонок',36)
            if auth.get('logged_in'):
                self.label('Вход выполнен: '+auth.get('name','Яндекс'),50)
                self.button('Найти колонки','send',{'command':'scan'});self.button('Выйти из аккаунта','send',{'command':'logout'})
            else:
                mat=s.get('qr_matrix')
                if mat:self.nodes.append(Item('qr',(width-230)//2,self.y,230,230,data={'matrix':mat}));self.y+=244
                self.label({'waiting':'Отсканируйте QR телефоном и подтвердите вход.','expired':'Срок QR истёк. Получите новый код.','error':'Вход не выполнен. Повторите запрос.'}.get(auth.get('qr_status'),'Пароль в программе вводить не нужно.'),50)
                self.button(('☑ ' if self.remember else '☐ ')+'Запомнить вход на этом ПК','remember')
                self.button('Получить новый QR','qr',accent=True)
        elif self.page=='connect':
            self.button('Найти колонки','send',{'command':'scan'},accent=True);self.button('Выбрать все','all')
            for d in s.get('devices',[]):
                self.button(('☑ ' if d['id'] in self.selected else '☐ ')+d['name']+' · '+d.get('host','нет IP'),'select',{'id':d['id']})
            if not s.get('devices'):self.label('Нажмите поиск. Компьютер и Станции должны видеть друг друга по сети.',60)
            self.button('Подключить выбранные','connect',accent=True)
            if desktop.get('group_trust'):self.button('Проверить сертификаты…','trust',accent=True)
            self.label('Группа играет из одного источника, но часы Станций независимы. Микрофонный замер — в Advanced menu.',64)
        elif self.page=='advanced':
            self.label('Дополнительные настройки',30)
            for t,page in [('Каналы и бас · стерео / 2.1 / 5.1','channels'),('Микрофон · выровнять колонки','mic'),('Аудиовыход Windows','output'),('Передача и устойчивость','stream'),('Штатный Яндекс · музыка / пара','native')]:self.button(t,'page',{'page':page})
            self.button('Сохранить диагностику','send',{'command':'export_native','advanced':True})
            self.label('WAV/PCM · 10 мс — основной режим. Каналы настраиваются без страницы браузера.',64)
        elif self.page=='channels':
            self.label('Роли каждой колонки',30)
            self.button('Пресет 2.1 · Лайт + Миди + Лайт','preset',accent=True)
            self.button(('☑ ' if self.draft['enabled'] else '☐ ')+'Разделять каналы','toggle_spatial')
            self.button('Вход: '+self.draft['layout'],'pick',{'key':'layout','options':[(k,k) for k in LAYOUTS]})
            self.slider('Раздел баса, Гц','crossover',self.draft['crossover_hz'],40,1500)
            for p in self.peers():
                d=p['device'];r=self.draft['speakers'].get(d['id'],{'role':'off'})
                self.button(d['name']+'  →  '+ROLES[r['role']],'editpeer',{'id':d['id']})
            self.button('Сохранить назначения','save',accent=True)
            self.label('Изменения применяются после остановки и нового запуска. Вход 5.1/7.1 требует соответствующего формата Windows, не стерео.',72)
        elif self.page=='preset':
            self.label('2.1 · две Лайт 2 + Миди',36)
            options=[(p['device']['name'],p['device']['id']) for p in self.peers()]
            for key,label in [('left','Слева · Лайт 2'),('bass','В центре · Миди / бас'),('right','Справа · Лайт 2')]:
                name=next((a for a,b in options if b==self.preset[key]),'Выберите колонку')
                self.button(label+'\n'+name,'pick',{'key':key,'options':options,'back':'preset'},height=62)
            self.button('Применить пресет 2.1','apply21',accent=True)
            self.label('Миди: низкие частоты среднего L+R. Лайт: соответствующий канал выше раздела. Начальный раздел 120 Гц, уровень баса −3 дБ. Это не отдельная LFE-дорожка.',110)
        elif self.page=='peer':
            row=self.draft['speakers'][self.peer_id]
            name=next((p['device']['name'] for p in self.peers() if p['device']['id']==self.peer_id),'Станция')
            self.label(name,36)
            opts=[(ROLES[k],k) for k in ('stereo',*LAYOUTS[self.draft['layout']],'bass','off')]
            self.button('Роль: '+ROLES[row['role']],'pick',{'key':'role','options':opts,'back':'peer'})
            self.slider('Уровень, дБ','gain',row.get('gain_db',0),-24,6)
            self.slider('Добавочная задержка, мс','delay',row.get('delay_ms',0),0,1000)
            self.button('Задержка −1 мс','delay_step',{'value':-1});self.button('Задержка +1 мс','delay_step',{'value':1})
            self.button(('☑ ' if row.get('invert') else '☐ ')+'Инверсия полярности','invert')
            self.button('К списку колонок','page',{'page':'channels'})
        elif self.page=='pick':
            self.label('Выберите вариант',28)
            for title,value in self.pick_options:self.button(title,'picked',{'value':value})
        elif self.page=='output':
            self.label('Отдельный аудиовыход Windows',32)
            self.button('Создать / назвать «Яндекс станция»','prepare',accent=True)
            self.button('Обновить устройства','send',{'command':'native_refresh','advanced':True})
            for e in desktop.get('native_audio',{}).get('endpoints',[]):
                self.button(e['name'],'send',{'command':'bind_endpoint','advanced':True,'data':{'id':e['id']}})
            self.label('Резерв: захват физического выхода',34)
            for e in s.get('outputs',[]):self.button(e['name'],'send',{'command':'set_capture','advanced':True,'data':{'key':e['key']}})
            self.button('Звук Windows…','send',{'command':'sound_settings','advanced':True})
            self.label('Новый выход создаёт VB-CABLE, не переименование динамиков. После мастера/перезагрузки нажмите создание ещё раз для имени и выбора. По умолчанию устройство переключается в Windows вручную.',110)
        elif self.page=='stream':
            self.label('Режим передачи',28)
            modes=[('Основной · WAV/PCM · 10 мс',{'playback_mode':'main'}),('Эксперимент · PCM · 1 мс',{'playback_mode':'experimental'}),('Fallback · MP3',{'playback_mode':'fallback','transport':'mp3','latency_profile':'fast'}),('Fallback · HLS',{'playback_mode':'fallback','transport':'hls','latency_profile':'balanced'})]
            self.button(next((a for a,b in modes if b==self.mode),'Основной PCM'),'pick',{'key':'mode','options':modes,'back':'stream'})
            self.label('Плановый «Живой звук»: выключен по умолчанию. Перезапуск может дать слышимую паузу.',64)
            for sec,t in [(0,'Не прерывать музыку по таймеру'),(240,'Обновлять раз в 4 минуты'),(600,'Обновлять раз в 10 минут')]:self.button(t,'policy',{'seconds':sec})
            self.button(('☑ ' if s.get('continuity',{}).get('enabled',True) else '☐ ')+'Обновлять после длительной тишины','silence')
            self.label('Сетевой запас ограничен, но более мягок к пачкам 1 мс и короткому Wi-Fi джиттеру. Гарантии отсутствия обрывов без проверки на колонках нет.',84)
        elif self.page=='mic':
            room=s.get('room',{});running=room.get('status') in ('starting','measuring','analyzing')
            self.label('Выровнять задержки колонок',30)
            self.label('Микрофон у места прослушивания. Не переносите его между колонками. Замер не исправляет дрейф часов и фазу баса.',80)
            if not running:
                options=[(m['name'],m['index']) for m in s.get('microphones',[])]
                self.button(next((a for a,b in options if b==self.mic),'Выберите микрофон'),'pick',{'key':'mic','options':options,'back':'mic'})
                self.button('Замерить все колонки','room_start',accent=True)
            else:
                self.label(str(room.get('progress',0))+'% · '+room.get('speaker','Подготовка'),48)
                self.button('Отменить и выключить микрофон','send',{'command':'room_cancel','advanced':True},accent=True)
            result=room.get('result') or {}
            for r in result.get('rows',[]):
                name=next((p['device']['name'] for p in self.peers() if p['device']['id']==r['id']),r['id'])
                self.label(name+': '+str(r['delay_ms'])+' мс · '+str(r['matched'])+'/3\nДобавить: '+str(r.get('correction_ms','—'))+' мс',54)
            if result.get('valid'):self.button('Применить предложенные задержки','room_apply',accent=True)
            elif room.get('status')=='done':self.label('Надёжного совпадения нет. Ничего автоматически не применено.',52)
            if room.get('error'):self.label(room['error'],60)
        self.total=self.y+20
        return self.nodes
    def label(self,text,height=40):self.nodes.append(Item('label',20,self.y,self.inner,height,text,small=True));self.y+=height+4
    def button(self,text,action,data=None,accent=False,height=42):self.nodes.append(Item('button',20,self.y,self.inner,height,text,action,data or {},accent=accent));self.y+=height+6
    def slider(self,text,action,value,minimum,maximum,data=None):
        self.nodes.append(Item('slider',20,self.y,self.inner,65,text,action,data or {},value=value,minimum=minimum,maximum=maximum));self.y+=72
