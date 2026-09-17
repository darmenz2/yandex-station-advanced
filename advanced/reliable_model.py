"""Native menu state machine, including immediate click feedback and complete setup."""
from __future__ import annotations
import copy
import time
from .flyout_model import Model as Base, Item
from .spatial import ROLES,config
from .operations import LABELS,PASSIVE

PRESETS=[('Одинаковая музыка · 1 / 2 / 3 и более','all'),('Одна колонка','single'),
 ('Стерео · левая + правая','stereo'),('2.1 · Лайт слева + бас Миди + Лайт справа','bass21'),
 ('Лайт слева + низ / нижняя середина Миди + Лайт справа','midbass')]

class Model(Base):
    def __init__(self,dispatch,confirm):
        super().__init__(dispatch,confirm)
        self.local_wait=None;self.seen_last=0;self.selection_initialized=False
        self.setup_mode='all';self.setup_roles={'left':'','bass':'','right':''};self.setup_edited=False
    def devices(self):
        rows={d['id']:d for d in self.state.get('desktop',{}).get('saved_group',[]) if d.get('id')}
        rows.update({p['device']['id']:p['device'] for p in self.peers()})
        rows.update({d['id']:d for d in self.state.get('devices',[]) if d.get('id')})
        return list(rows.values())
    def update(self,state):
        super().update(state)
        op=self.state.get('operations',{});last=op.get('last') or {}
        if self.local_wait and (op.get('active') or last.get('id',0)>self.local_wait['serial']):self.local_wait=None
        if last.get('id',0)>self.seen_last:
            self.seen_last=last['id']
            if last.get('status')=='done':
                if last.get('command') in ('spatial_save','preset21'):self.editing=False;self.draft=copy.deepcopy(self.state.get('spatial',config()))
                if last.get('command') in ('apply_setup','confirm_setup'):self.goto('home')
            elif last.get('status')=='needs_confirmation':self.goto('setup')
        if not self.selection_initialized and self.devices():
            saved=self.state.get('setup') or {}
            self.setup_mode=saved.get('mode','all');self.setup_roles.update(saved.get('roles',{}))
            self.selected={p['device']['id'] for p in self.peers()} or {d['id'] for d in self.state.get('desktop',{}).get('saved_group',[])}
            self.selection_initialized=True
    def busy(self):return bool(self.local_wait or self.state.get('operations',{}).get('busy'))
    def toast(self,text):
        self.local_wait=None;self.error=text;self.error_until=time.monotonic()+120
    def emit(self,command,data=None,advanced=False):
        if command not in PASSIVE:
            if self.busy() and command not in ('stop','shutdown','disconnect'):
                self.error='Операция уже выполняется. Повторный запуск не отправлен.';self.error_until=time.monotonic()+8;return
            last=self.state.get('operations',{}).get('last') or {}
            self.local_wait={'command':command,'label':LABELS.get(command,'Выполнение команды'),
                             'serial':last.get('id',0),'at':time.monotonic()}
            self.error=''
        super().emit(command,data,advanced)
    def setup_payload(self):
        selected=[d for d in self.devices() if d['id'] in self.selected]
        return {'mode':self.setup_mode,'devices':selected,'roles':dict(self.setup_roles),'launch':dict(self.mode)}
    def act(self,action,data=None,value=None):
        data=data or {}
        if action=='native_music':
            if self.confirm('Передача Windows будет остановлена. Яндекс включит свою музыку на ВСЕХ Станциях дома — не только отмеченных в Advanced. Миди при этом не выделяется в басовый канал. Продолжить?'):
                self.emit('native_music',{'confirmed':True},True)
            return
        if action=='native_pair_probe':
            if self.confirm('Проверить звук Windows через уже созданную штатную пару? Advanced остановит независимые потоки и отправит полное стерео только главной колонке. Миди отдельно не подключается. Работоспособность передачи WAV ведомой зависит от прошивки; скрытого fallback не будет.'):
                self.emit('native_pair_probe',{'confirmed':True,'leader_id':data['leader_id']},True)
            return
        if action=='page' and data.get('page') in ('connect','preset'):
            self.goto('setup');return
        if action=='preset':self.setup_mode='bass21';self.goto('setup');return
        if action=='page' and data.get('page')=='setup':self.goto('setup');return
        if action=='setup_mode':self.setup_mode=data['mode'];self.setup_edited=True;return
        if action=='setup_pick':
            options=[(d['name'],d['id']) for d in self.devices() if d['id'] in self.selected]
            self.choose('setup_'+data['role'],options,'setup');return
        if action=='picked' and self.pick_key.startswith('setup_'):
            self.setup_roles[self.pick_key[6:]]=data['value'];self.goto('setup');return
        if action=='all':self.selected={d['id'] for d in self.devices() if d.get('host')};return
        if action=='none':self.selected.clear();return
        if action=='apply_setup':
            self.emit('apply_setup',self.setup_payload(),True);return
        if action=='trust':
            trust=self.state.get('desktop',{}).get('group_trust',[])
            text='Проверьте IP и сертификаты своих колонок:\n\n'+'\n\n'.join(t['device']['name']+' · '+t['device']['host']+'\n'+t['fingerprint'] for t in trust)
            if self.confirm(text):
                accepted={t['device']['id']:t['fingerprint'] for t in trust}
                if self.state.get('pending_setup'):self.emit('confirm_setup',{'accepted':accepted},True)
                else:self.emit('connect_group',{'devices':[d for d in self.devices() if d['id'] in self.selected],'accepted':accepted},True)
            return
        if action=='save':self.emit('spatial_save',{'config':copy.deepcopy(self.draft)},True);return
        if action=='apply21':self.emit('preset21',dict(self.preset),True);return
        if action=='connect':self.emit('connect_group',{'devices':[d for d in self.devices() if d['id'] in self.selected]},True);return
        if action=='play' and any(r['status']=='unassigned' for r in self.state.get('group_health',{}).get('rows',[])):
            self.goto('setup');self.toast('Не все каналы назначены. Выберите схему и примените её целиком.');return
        if action=='smooth':self.emit('smooth_policy',{'enabled':not self.state.get('smooth',{}).get('enabled',True)},True);return
        return super().act(action,data,value)
    def status_banner(self):
        ops=self.state.get('operations',{});active=ops.get('active');last=ops.get('last')
        if active:self.label(active['label']+' · '+str(active.get('elapsed_seconds',0))+' с\n'+active.get('detail',''),66)
        elif self.local_wait:
            wait=time.monotonic()-self.local_wait['at']
            self.label(self.local_wait['label']+('…' if wait<10 else ' · ожидаем ответ приложения…'),50)
        elif last:
            prefix={'done':'Готово','error':'Ошибка','cancelled':'Отменено','needs_confirmation':'Требуется подтверждение'}.get(last.get('status'),'Состояние')
            self.label(prefix+': '+last.get('detail',''),64 if len(last.get('detail',''))<135 else 88)
        if self.error and time.monotonic()<self.error_until:self.label(self.error,70)
        if self.busy():self.button('Остановить / отменить текущую операцию','send',{'command':'stop'})
    def _header(self,width):
        self.nodes=[];self.y=78;self.width=width;self.inner=width-40
        self.nodes.append(Item('header',20,18,self.inner,46,'Яндекс станция'))
        if self.page!='home':self.nodes.append(Item('button',width-90,20,70,32,'Назад','page',{'page':'home'},small=True))
    def _disable_busy(self):
        if self.busy():
            allow={'page','master','station'}
            for n in self.nodes:
                if n.action in allow:continue
                if n.action=='send' and n.data.get('command') in ('stop','shutdown','room_cancel'):continue
                if n.action:
                    n.action='';n.accent=False
        return self.nodes
    def build(self,width=430):
        if self.page not in ('home','setup','events','stream','native'):
            super().build(width)
            # Put a persistent operation result before the old page content.
            old=self.nodes;oldtotal=self.total;self.nodes=[];self.y=78
            self.status_banner();offset=self.y-78
            for n in old:
                if n.y>=78:n.y+=offset
            self.nodes=[n for n in old if n.y<78]+self.nodes+[n for n in old if n.y>=78]
            self.total=oldtotal+offset
            return self._disable_busy()
        self._header(width);self.status_banner()
        s=self.state;desk=s.get('desktop',{});health=s.get('group_health',{});live=s.get('media',{}).get('kind')=='live'
        if self.page=='home':
            native_source=s.get('yandex_native',{}).get('source',{})
            native_active=native_source.get('mode')=='yandex_music'
            if native_active:self.label('Источник: штатная музыка Яндекса. Звук Windows сейчас не передаётся.',64)
            if native_source.get('mode')=='pair_pcm_probe':self.label('Испытание штатной пары: полный стереопоток только главной колонке. Звучание ведомой пока не подтверждено.',84)
            live = live or native_active
            self.label(f"Подключены: {health.get('connected',0)} / {health.get('selected',0)}   ·   Получают звук: {health.get('receiving',0)}",32)
            self.button('■  Остановить звук' if live else '▶  Передавать звук', 'send' if live else 'play',{'command':'stop'} if live else {},accent=True)
            self.button('Настроить 1 / 2 / 3 колонки','page',{'page':'setup'})
            cap=(s.get('media',{}).get('capture') or {});device=cap.get('device') or {}
            if device:self.label('Источник: '+device.get('name','')+'\nPCM · '+str(cap.get('requested_block_ms',10))+' мс · '+('сигнал есть' if cap.get('input_level',0)>.001 else 'тишина'),56)
            if not s.get('auth',{}).get('logged_in'):self.button('Войти по QR-коду','page',{'page':'qr'},accent=True)
            master=desk.get('native_audio',{}).get('selected') or {}
            if master:self.slider('Громкость Windows','master',master.get('volume',0),0,1)
            states={p['device']['id']:p for p in self.peers()}
            for row in health.get('rows',[]):
                p=states.get(row['id']);role=s.get('spatial',{}).get('speakers',{}).get(row['id'],{}).get('role','stereo')
                self.label(row['name']+' · '+ROLES.get(role,role)+'\n'+row['label'],54)
                if p:
                    self.slider('Громкость колонки','station',p['state'].get('volume',0) or 0,0,1,{'id':row['id']})
                    approve=p.get('peer_approval')
                    if approve:self.button('Разрешить аудио через '+approve['ip'],'approve',{'id':row['id'],**approve},accent=True)
                if row['status'] in ('stalled','error','paused'):
                    self.button('Повторить только эту колонку','send',{'advanced':True,'command':'peer_retry','data':{'id':row['id']}})
            self.button('Защита от заиканий и состояния','page',{'page':'stream'})
            self.button('Штатный Яндекс · музыка / стереопара','page',{'page':'native'})
            self.button('Advanced menu','page',{'page':'advanced'})
            self.button('Журнал действий','page',{'page':'events'})
            self.button('Аккаунт · QR','page',{'page':'qr'})
            self.label('«Получает звук» означает приём HTTP, не измеренную синхронность динамиков. Точная акустическая задержка требует микрофонного замера.',68)
            self.label('Версия '+str(desk.get('version','')),26)
            self.button('Завершить приложение','send',{'command':'shutdown'})
        elif self.page=='setup':
            self.label('Система целиком · один запуск',32)
            self.button('Найти / обновить колонки','send',{'command':'scan'})
            if desk.get('group_trust'):
                self.label('Новый сертификат. Предыдущая музыка не заменяется до подтверждения.',52)
                self.button('Проверить и подтвердить сертификаты','trust',accent=True)
                self.button('Отменить подтверждение','send',{'advanced':True,'command':'cancel_group_trust'})
            self.label('1. Выберите схему',28)
            for name,mode in PRESETS:self.button(('● ' if mode==self.setup_mode else '○ ')+name,'setup_mode',{'mode':mode},height=56 if mode in ('midbass','bass21') else 44)
            self.label('2. Выберите колонки · отмечено '+str(len(self.selected)),32)
            self.button('Выбрать все','all');self.button('Снять все флажки','none')
            for d in self.devices():self.button(('☑ ' if d['id'] in self.selected else '☐ ')+d['name']+' · '+d.get('host','нет IP'),'select',{'id':d['id']})
            if not self.devices():self.label('Войдите по QR и нажмите поиск.',44)
            if self.setup_mode in ('stereo','bass21','midbass'):
                self.label('3. Назначьте каждую позицию',32)
                required=('left','right') if self.setup_mode=='stereo' else ('left','bass','right')
                for role in required:
                    label={'left':'Слева · Лайт 2','right':'Справа · Лайт 2','bass':'В центре · Миди'}[role]
                    name=next((d['name'] for d in self.devices() if d['id']==self.setup_roles[role]),'Выберите…')
                    self.button(label+'\n'+name,'setup_pick',{'role':role},height=60)
                if self.setup_mode!='stereo':self.label('Раздел: '+('400 Гц · Миди играет низ и нижнюю середину' if self.setup_mode=='midbass' else '120 Гц · Миди играет бас')+'. Постоянный запас уровня −3 дБ. Не автоматическое приглушение.',78)
            self.button('Применить схему и запустить','apply_setup',accent=True,height=48)
            self.label('Все назначения сохраняются вместе. Повторный запуск той же схемы не разрывает работающие соединения. Неверная/неполная схема не включается.',74)
        elif self.page=='stream':
            self.label('PCM · непрерывные отсчёты вместо таймерного темпа',60)
            self.label('Источник задаёт ход звука. Нет обязательной паузы между HTTP-блоками, пропуска обычных пачек и независимой смены темпа каждой колонки.',94)
            reserve=s.get('smooth',{}).get('pcm_reserve_ms',120)
            for ms in (80,120,200,300):
                self.button(('● ' if reserve==ms else '○ ')+f'Защитный резерв {ms} мс'+(' · по умолчанию' if ms==120 else ''),'send',{'advanced':True,'command':'pcm_reserve','data':{'ms':ms}})
            self.label('Резерв накапливается при старте и выдаётся Станции, затем следуют новые отсчёты. Это не измеренный буфер Станции. Для смены сначала остановите звук.',86)
            self.button(('☑ ' if s.get('smooth',{}).get('enabled') else '☐ ')+'Старый регулятор темпа · эксперимент','smooth')
            self.label('Старый регулятор выключен по умолчанию. Не включайте его для первичной проверки треска. Синхронизация прошивки — отдельный пункт «Штатный Яндекс».',90)
            for p in self.peers():
                r=p.get('media',{}).get('continuous_pcm') or {}
                self.label(p['device']['name']+'\nОчередь ПК: '+str(r.get('pc_backlog_ms','—'))+' мс · коррекция '+str(r.get('rate_correction_ppm','—'))+' ppm\nПропуски источника: '+str(r.get('discontinuities',0)),70)
            self.button('Измерить задержки микрофоном','page',{'page':'mic'})
            modes=[('Основной · WAV/PCM · 10 мс',{'playback_mode':'main'}),('Экспериментальный · PCM · 1 мс',{'playback_mode':'experimental'}),('Резервный MP3',{'playback_mode':'fallback','transport':'mp3','latency_profile':'fast'}),('Резервный HLS',{'playback_mode':'fallback','transport':'hls','latency_profile':'balanced'})]
            self.button(next((a for a,b in modes if b==self.mode),'Основной PCM'),'pick',{'key':'mode','options':modes,'back':'stream'})
            self.label('Для смены режима сначала остановите звук. Пресеты каналов работают только в PCM. Независимые проигрыватели колонок могут иметь разный внутренний буфер.',78)
            self.label('Если одновременно приглушаются все колонки, проверьте также Windows → Звук → Связь. Это отдельная системная настройка, не диагностика установленной причины.',84)
            self.button('Открыть «Звук» Windows','send',{'command':'classic_sound','advanced':True})
            self.button('Сохранить диагностику','send',{'command':'export_native','advanced':True})
        elif self.page=='native':
            native=s.get('yandex_native',{});source=native.get('source',{})
            self.label('Штатный механизм Яндекса',36)
            self.label('Здесь Яндекс сам управляет проигрывателями и синхронизацией. Это не три независимых HTTP-потока Advanced.',76)
            self.button('Яндекс Музыка · включить везде','native_music',accent=True,height=50)
            self.label('Остановит захват Windows. Включит музыку Яндекса на всех Станциях дома, включая невыбранные здесь. Это не передача Spotify / YouTube с ПК и не распределение полос 2.1.',112)
            if source.get('mode')=='yandex_music':self.button('Остановить штатную музыку','send',{'command':'stop'})
            self.label('Две Лайт 2 · штатная стереопара',38)
            self.button('Как создать пару в «Доме с Алисой»','send',{'command':'native_help','advanced':True})
            self.label('Пару создаёт приложение Яндекса, не Advanced. На ведомой могут сброситься настройки и сценарии. Миди отдельным басом в эту пару не добавляется.',100)
            self.button('Проверить конфигурацию пары','send',{'command':'native_detect','advanced':True})
            for pair in native.get('pairs',[]):
                self.label(pair['leader_name']+' → '+pair['follower_name']+'\nГлавная / ведомая подтверждены конфигурацией; PCM пока не проверен.',80)
                self.button('Проверить PCM только через главную','native_pair_probe',{'leader_id':pair['leader_id']},height=50)
            if native.get('configuration_age_seconds') is not None and not native.get('pairs'):
                self.label('Подтверждённой пары в ответе нет. Программа не угадывает главную по имени или IP.',70)
            self.label('Наблюдения прошивки · только чтение',36)
            for row in native.get('peers',[]):
                obs=row.get('observation',{});state=row.get('state',{})
                self.label(row['name']+'\n'+('Играет: '+state.get('title','') if state.get('playing') else 'Нет свежего подтверждения воспроизведения')+'\nАудиочасы: '+('поле присутствует' if obs.get('clock_fingerprint') else 'не переданы в Glagol'),90)
            self.button('Сохранить диагностику штатного режима','send',{'command':'export_native','advanced':True})
            self.label('Поля часов и статус Playing не доказывают акустическую синхронность. Во время штатной музыки PCM/микрофон не захватываются.',92)
        elif self.page=='events':
            for op in reversed(s.get('operations',{}).get('history',[])):
                self.label(op.get('time','')+' · '+op['label']+' · '+op['status']+'\n'+op.get('detail',''),88)
            for entry in reversed(s.get('logs',[])[-12:]):self.label(entry['time']+' · '+entry['text'],76)
        self.total=self.y+20
        return self._disable_busy()
