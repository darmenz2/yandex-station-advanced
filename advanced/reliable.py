"""Observable, idempotent group setup and continuous PCM ownership.

The normal path preserves source samples; tempo correction is experimental. Speaker DAC time is unknown;
no byte counter is presented as an acoustic synchronisation measurement.
"""
from __future__ import annotations
import asyncio
import contextlib
import copy
import json
from collections import defaultdict, deque
import time
from .desktop import DesktopController
from .native_mode import NativeMode
from .operations import Operations
from .spatial import config, preset21
from .steady_stream import Timeline
from station_bridge.protocol import BridgeError, audio_play

SETUPS={'all','single','stereo','bass21','midbass'}

def validate_complete(cfg, devices):
    """Never turn an unassigned connected speaker silently into role=off."""
    cfg=config(cfg)
    if not cfg['enabled']:return cfg
    missing=[d['name'] for d in devices if d['id'] not in cfg['speakers']]
    if missing:raise BridgeError('Не назначены каналы: '+', '.join(missing)+'. Откройте «Настроить систему» и примените всю схему.')
    if not any(cfg['speakers'][d['id']]['role']!='off' for d in devices):
        raise BridgeError('Все выбранные колонки выключены в назначениях каналов.')
    return cfg

def setup_config(data, devices):
    mode=data.get('mode','all')
    if mode not in SETUPS:raise BridgeError('Выберите схему системы.')
    ids={d['id'] for d in devices}
    if not ids:raise BridgeError('Выберите хотя бы одну колонку.')
    if mode=='single' and len(ids)!=1:raise BridgeError('Для одной колонки оставьте один флажок.')
    if mode in ('all','single'):return config({'enabled':False})
    roles=data.get('roles') or {}
    required=('left','right') if mode=='stereo' else ('left','bass','right')
    chosen=[roles.get(x) for x in required]
    if len(set(chosen))!=len(required) or any(x not in ids for x in chosen) or set(chosen)!=ids:
        raise BridgeError('Назначьте '+str(len(required))+' разные выбранные колонки на все позиции. Лишние флажки снимите.')
    if mode=='stereo':
        return config({'enabled':True,'speakers':{roles['left']:{'role':'left'},roles['right']:{'role':'right'}}})
    value=preset21(roles['left'],roles['bass'],roles['right'])
    value['crossover_hz']=400. if mode=='midbass' else 120.
    value['headroom_db']=-3.   # fixed safety margin, never a time-varying AGC
    return config(value)

class ReliableController(NativeMode, DesktopController):
    def __init__(self,directory):
        super().__init__(directory)
        self.operations=Operations()
        self.pending_setup=None
        self.desired_devices=copy.deepcopy(self.settings.data.get('desktop_group',[]))
        self.connection_errors={};self.play_errors={}
        self._start_signature=None
        self._replace_group=False
        self.timeline=None
        self.media.continuous_pcm=True
        # Migrate the unverified per-HTTP tempo servo out of the normal path.
        if self.settings.data.get('pcm_policy_revision') != 5:
            self.settings.data['smooth_pcm_enabled'] = False
            self.settings.data['pcm_policy_revision'] = 5
            self.settings.save()
        self.smooth_enabled=bool(self.settings.data.get('smooth_pcm_enabled',False))
        raw_reserve = self.settings.data.get('pcm_reserve_ms', 120)
        self.pcm_reserve_ms = raw_reserve if raw_reserve in (80,120,200,300) else 120
        self._repairs=defaultdict(lambda:deque(maxlen=3))
        self._repair_active=set()
        self._last_setup=copy.deepcopy(self.settings.data.get('desktop_setup',{}))

    def stage(self,text):
        self.operations.stage(text)
        self.log(text)

    async def action(self,command,data):
        # STOP is actionable even during a slow search/connect/start.
        if command in ('stop','shutdown','disconnect') and self.operations.task and not self.operations.task.done():
            await self.operations.close()
        if command=='heartbeat':return await super().action(command,data)
        return await self.operations.run(command,data,lambda:super(ReliableController,self).action(command,data))

    async def advanced_action(self,command,data):
        if command=='room_cancel':return await super().advanced_action(command,data)
        return await self.operations.run(command,data,lambda:self._advanced(command,data))

    async def _advanced(self,command,data):
        if command in ('apply_setup','confirm_setup'):
            if self.room_task and not self.room_task.done():raise BridgeError('Сначала отмените микрофонный замер.')
            async with self.action_lock:
                return await self.apply_setup(data if command=='apply_setup' else None,data.get('accepted'))
        if command=='cancel_group_trust':
            self.pending_setup=None
        if command=='pcm_reserve':
            value=data.get('ms')
            if isinstance(value,bool) or value not in (80,120,200,300):raise BridgeError('Выберите 80, 120, 200 или 300 мс.')
            if self.media.kind=='live':raise BridgeError('Для смены резерва сначала остановите передачу.')
            self.pcm_reserve_ms=value;self.settings.data['pcm_reserve_ms']=value;self.settings.save()
            return {'message':f'Резерв PCM: {value} мс. Он накапливается при запуске и выдаётся Станции; это не замер её буфера.'}
        if command=='smooth_policy':
            if self.media.kind=='live':raise BridgeError('Для смены механизма чтения сначала остановите передачу.')
            value=data.get('enabled')
            if not isinstance(value,bool):raise BridgeError('Нужен флажок плавной коррекции.')
            self.smooth_enabled=value;self.settings.data['smooth_pcm_enabled']=value;self.settings.save()
            for p in self.all_peers():
                p.media.smooth_enabled=value
                if p.media.steady_reader and hasattr(p.media.steady_reader,'servo'):p.media.steady_reader.servo.enabled=value
            return {'message':'Плавная коррекция очереди ПК '+('включена.' if value else 'выключена.')}
        if command=='peer_retry':
            async with self.action_lock:
                return await self.retry_peer(str(data.get('id','')),explicit=True)
        if command=='spatial_save':
            validate_complete(data.get('config'),[p.device for p in self.all_peers()])
        if command=='room_start' and self.health()['selected']!=self.health()['connected']:
            raise BridgeError('Для замера сначала подключите все выбранные колонки или измените состав системы.')
        if command=='classic_sound':
            from .native_helper import shell_open
            import os
            from pathlib import Path
            await asyncio.to_thread(shell_open,Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/control.exe','mmsys.cpl')
            return {'message':'Открыт «Звук» Windows. Вкладка «Связь» позволяет проверить общее приглушение; программа ничего не меняла.'}
        if command=='prepare_output':self.stage('Открываем мастер Windows. При запросе UAC подтвердите отдельное окно.')
        result=await super().advanced_action(command,data)
        if command=='room_start' and self.room_task:
            self.operations.stage('Последовательный замер. Прогресс показан в разделе микрофона.')
            await self.room_task
            if self.room_state.get('status')=='error':raise BridgeError(self.room_state.get('error','Ошибка микрофонного замера.'))
            result['message']='Замер завершён. Результат и кнопка применения находятся в разделе микрофона.'
        if command=='prepare_output':
            result['message']='Мастер VB-CABLE открыт. Завершите его; затем обновите устройства.' if result.get('stage')=='vendor_wizard' else 'Запрошено переименование. Подтвердите UAC и нажмите «Обновить устройства».'
        if command=='export_native' and result.get('path'):
            from pathlib import Path
            p=Path(result['path']);report=json.loads(p.read_text(encoding='utf-8'))
            report['operations']=self.operations.snapshot();report['group_health']=self.health()
            report['timeline']=self.timeline.snapshot() if self.timeline else None
            report['setup']=self._last_setup
            report['yandex_native']=self.native_mode_snapshot()
            p.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            result['message']='Диагностика сохранена: '+str(p)
        return result

    async def apply_setup(self,data,accepted=None):
        if data is None:
            if self.pending_setup is None:raise BridgeError('Нет ожидающей настройки. Выберите схему заново.')
            data=copy.deepcopy(self.pending_setup)
        raw=data.get('devices')
        if not isinstance(raw,list) or not 1<=len(raw)<=16:raise BridgeError('Выберите от одной до 16 колонок.')
        devices=[self.validate_device(d) for d in raw]
        if len({d['id'] for d in devices})!=len(devices):raise BridgeError('Одна колонка не может занимать две позиции.')
        value=setup_config(data,devices)
        if len(devices)>1 and time.monotonic()-self.native_loaded_at<120:
            from station_bridge.native_state import configured_pairs
            chosen={d['id'] for d in devices}
            for pair in configured_pairs(self.native_inventory):
                if pair['leader_id'] in chosen and pair['follower_id'] in chosen:
                    raise BridgeError('В выборе есть штатная стереопара. Не посылайте два независимых потока её участникам. Используйте «Штатный Яндекс» → испытание пары, либо сначала явно разъедините пару в «Доме с Алисой».')
        intended={'mode':data.get('mode','all'),'roles':copy.deepcopy(data.get('roles',{}))}
        same_assignments=(self._last_setup==intended and set(value['speakers'])==set(self.spatial['speakers']) and
                          all(value['speakers'][i]['role']==self.spatial['speakers'][i]['role'] for i in value['speakers']))
        if same_assignments:value=copy.deepcopy(self.spatial)  # retain explicit calibration/gain on repeated Apply
        launch=data.get('launch',{'playback_mode':'main'})
        if not isinstance(launch,dict) or launch.get('playback_mode','main') not in ('main','experimental','fallback'):
            raise BridgeError('Некорректный режим передачи.')
        if value['enabled'] and launch.get('playback_mode')=='fallback':raise BridgeError('Разделение каналов требует PCM.')
        self.pending_setup=copy.deepcopy(data)
        self.stage('Проверяем состав и сертификаты: '+str(len(devices))+' колонок…')
        self._replace_group=True
        try:result=await self.connect_group(devices,accepted,data.get('overrides'))
        finally:self._replace_group=False
        if result.get('trust_required'):return result
        failures=[r for r in result.get('results',[]) if not r.get('connected')]
        if failures:
            raise BridgeError('Не подключены все выбранные колонки. Ни одна новая передача не запущена; смотрите состояния и повторите настройку.')
        unchanged=(value==self.spatial and self.media.kind=='live' and self._launch_key(launch,False)==self._start_signature)
        if not unchanged and self.media.kind=='live':await self.stop_playback()
        self.spatial=value;self.settings.data['spatial']=copy.deepcopy(value)
        self._last_setup={'mode':data.get('mode','all'),'roles':copy.deepcopy(data.get('roles',{}))}
        self.settings.data['desktop_setup']=self._last_setup;self.settings.save()
        self.pending_setup=None
        self.stage('Все колонки подключены. Запускаем PCM и ожидаем аудиозапросы…')
        result=await self.start_live_playback(launch)
        if result.get('idempotent'):return result
        return {**result,'message':'Схема применена. Команды отправлены; состояния ниже показывают, кто действительно забирает звук.'}

    async def connect_group(self,raw_devices,accepted=None,overrides=None):
        if isinstance(raw_devices,list) and raw_devices:
            devices=[self.validate_device(d) for d in raw_devices]
            existing=self.all_peers()
            keys=lambda rows:{(d['id'],d['host'],d['port'],d['platform']) for d in rows}
            if len(devices)==len(existing) and keys(devices)==keys([p.device for p in existing]) and all(p.glagol.connected for p in existing) and not overrides:
                self.desired_devices=copy.deepcopy(devices);self.group_trust={}
                return {'idempotent':True,'results':[{'id':p.device['id'],'connected':True} for p in existing],
                        'message':'Эти колонки уже подключены. Соединения сохранены.'}
            self.desired_devices=copy.deepcopy(devices)
        result=await super().connect_group(raw_devices,accepted,overrides)
        self.connection_errors={r['id']:r.get('error','Ошибка подключения') for r in result.get('results',[]) if not r.get('connected')}
        return result

    def _launch_key(self,data,synthetic):
        # Compare intent rather than unstable numeric WASAPI enumeration positions.
        mode=data.get('playback_mode','main')
        key={'mode':mode,'synthetic':synthetic,'endpoint':self.settings.data.get('desktop_endpoint_id'),
             'source':self.device_choice or data.get('device_key') or data.get('device_index'),
             'spatial':self.spatial,'guard':data.get('micro_buffer_ms',0), 'pcm_reserve_ms':self.pcm_reserve_ms, 'legacy_servo':self.smooth_enabled}
        if mode=='fallback':key.update(transport=data.get('transport','mp3'),profile=data.get('latency_profile','fast'))
        return json.dumps(key,sort_keys=True,ensure_ascii=False)

    async def start_live_playback(self,data,*,synthetic=False):
        if not self.room_plan:
            validate_complete(self.spatial,[p.device for p in self.all_peers()])
        if not self.glagol or not self.glagol.connected:raise BridgeError('Сначала подключите колонки через «Настроить систему».')
        key=self._launch_key(data,synthetic)
        if self.media.kind=='live' and self.media.capture and self.media.capture.stats().get('running'):
            if key==self._start_signature:
                return {'idempotent':True,'message':'Передача уже работает с этими настройками. Повторного запуска нет.'}
            if not synthetic:raise BridgeError('Передача уже запущена с другими настройками. Сначала остановите её или примените всю схему через мастер.')
        self.media.continuous_pcm=True
        self.play_errors={}
        result=await super().start_live_playback(data,synthetic=synthetic)
        self._start_signature=key
        return result

    async def stop_playback(self):
        if hasattr(self,'timeline') and self.timeline:
            self.timeline.cancel();self.timeline=None
        await super().stop_playback()
        self._start_signature=None

    async def send_audio(self,url,title,hls=False):
        capture=self.media.capture
        self.configure_readers()
        use=bool(capture and capture.transport=='pcm')
        if self.timeline:self.timeline.cancel()
        self.timeline=Timeline(capture.live_ring,capture.frames,[p.device['id'] for p in self.all_peers()],
                               target_ms=self.pcm_reserve_ms) if use else None
        for p in self.all_peers():
            p.media.shared_timeline=self.timeline;p.media.timeline_id=p.device['id']
            p.media.smooth_enabled=self.smooth_enabled
        result=await super().send_audio(url,title,hls)
        for p,row in zip(self.all_peers(),result.get('group_results',[])):
            if row.get('error'):self.play_errors[p.device['id']]=row['error']
        return result

    async def soft_resync(self,reason='manual'):
        if self.timeline and self.media.kind=='live':
            if not self.smooth_enabled:
                return {'idempotent':True, 'message':'Непрерывный PCM уже читает источник по порядку. URL и соединения не сбрасывались; пропуска музыки и скрытого изменения темпа не было. Буфер самой Станции эта кнопка не измеряет.'}
            count=0
            for p in self.all_peers():
                if p.media.steady_reader:p.media.steady_reader.request_live();count+=1
            return {'message':f'Экспериментальный регулятор активен для {count} потоков; не синхронизация ЦАП.'}
        return await super().soft_resync(reason)

    async def retry_peer(self,ident,explicit=False):
        p=next((p for p in self.all_peers() if p.device['id']==ident),None)
        if not p or not p.glagol.connected:raise BridgeError('Колонка не подключена. Повторите настройку системы.')
        if self.media.kind!='live' or not self.media.capture:raise BridgeError('Сначала запустите передачу.')
        if p.media._live_tasks:return {'idempotent':True,'message':'Колонка уже читает поток. Её соединение не сброшено.'}
        if p.media.peer_policy.pending(p.media.token):raise BridgeError('Сначала подтвердите сетевой адрес этой колонки.')
        result=await p.glagol.send(audio_play(p.media.current_url(),p.media.title,p.media.transport=='hls'))
        self.play_errors.pop(ident,None)
        p.media.event('single_peer_retry',automatic=not explicit)
        return {**result,'message':'Повторная команда отправлена только этой колонке; остальные не остановлены.'}

    async def continuity_step(self,now=None):
        if not self.timeline or self.room_plan:return await super().continuity_step(now)
        now=time.monotonic() if now is None else now
        # Continuous PCM never rotates URLs after silence or on a timer.
        if self.action_lock.locked() or self.operations.snapshot()['busy']:return
        for p in self.all_peers():
            if p.media._live_tasks or not p.media.last_close_at:continue
            if p.media.last_close_reason not in ('http_write_timeout','peer_disconnected'):continue
            s=p.glagol.safe_state()
            if not (p.glagol.connected and s.get('playing') and s.get('alice')=='IDLE' and
                    s.get('state_age_seconds',99)<10 and s.get('title')==p.media.title):continue
            ident=p.device['id'];q=self._repairs[ident]
            if q and now-q[-1]<10:continue
            if len(q)==3 and now-q[0]<60:continue
            if now-p.media.last_close_at<2:continue
            q.append(now)
            async with self.action_lock:
                with contextlib.suppress(BridgeError):await self.retry_peer(ident)

    def health(self):
        rows=[];now=time.monotonic();peers={p.device['id']:p for p in self.all_peers()}
        ordered={d['id']:d for d in self.desired_devices}
        ordered.update({i:p.device for i,p in peers.items()})
        for ident,d in ordered.items():
            p=peers.get(ident);status='disconnected';label='Не подключена';error=self.connection_errors.get(ident,'')
            receiving=False;state={};stats={}
            if p:
                state=p.glagol.safe_state();stats=p.media.stats()
                if self.native_source.get('mode')=='yandex_music':
                    status='native_music'
                    label='Яндекс сообщает: играет' if state.get('playing') and state.get('state_age_seconds',99)<10 else 'Штатный запрос отправлен · ожидаем состояние'
                elif not p.glagol.connected:label='Соединение Glagol потеряно'
                elif self.spatial['enabled'] and ident not in self.spatial['speakers']:status='unassigned';label='Канал не назначен'
                elif self.spatial['enabled'] and self.spatial['speakers'][ident]['role']=='off':status='off';label='Выключена в схеме'
                elif stats.get('pending_audio_peer'):status='needs_access';label='Нужно разрешить адрес аудио'
                elif p.media.waiting_for_group:status='group_wait';label='Ожидание остальных колонок'
                elif p.media._live_tasks and now-p.media.last_request<2:
                    status='receiving';label='Получает PCM' if p.media.transport=='pcm' else 'Получает аудио';receiving=True
                elif state.get('alice') not in (None,'IDLE'):status='alice';label='Алиса / голосовой режим'
                elif self.media.kind=='live' and ident in self.play_errors:status='error';label='Команда не выполнена';error=self.play_errors[ident]
                elif self.media.kind=='live' and now-p.media.started_at<15:status='waiting_http';label='Команда отправлена · ждём аудиозапрос'
                elif self.media.kind=='live' and not state.get('playing') and state.get('state_age_seconds',99)<10:status='paused';label='Станция на паузе · автоматический запуск запрещён'
                elif self.media.kind=='live':status='stalled';label='Поток не читается · повторите для этой колонки'
                else:status='ready';label='Подключена · остановлена'
            if error:label+=' · '+error
            rows.append({'id':ident,'name':d.get('name','Станция'),'status':status,'label':label,
                         'receiving':receiving,'error':error,'acoustic_sync_verified':False})
        return {'rows':rows,'selected':len(ordered),'connected':sum(p.glagol.connected for p in peers.values()),
                'receiving':sum(r['receiving'] for r in rows),'note':'Получает аудио — не подтверждение слышимого звука или точной синхронизации.'}

    def native_snapshot(self):
        result=super().native_snapshot()
        result['operations']=self.operations.snapshot();result['group_health']=self.health()
        result['setup']=copy.deepcopy(self._last_setup)
        result['yandex_native']=self.native_mode_snapshot()
        result['pending_setup']=bool(self.pending_setup)
        result['smooth']={'enabled':self.smooth_enabled,'timeline':self.timeline.snapshot() if self.timeline else None,
                          'speaker_clock_available':False, 'pcm_reserve_ms':self.pcm_reserve_ms,
                          'reader_policy':'experimental_servo' if self.smooth_enabled else 'source_sample_fifo'}
        for r in result['desktop']['peers']:
            r['health']=next((x for x in result['group_health']['rows'] if x['id']==r['device']['id']),{})
        return result

    async def close(self):
        await self.operations.close()
        await super().close()
