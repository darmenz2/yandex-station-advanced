"""Native flyout orchestration and explicit Advanced menu actions."""
from __future__ import annotations
import asyncio
import contextlib
import copy
import json
from pathlib import Path
import time
from .group import GroupController
from .spatial import config, preset21, Processor, LAYOUTS, MASKS
from station_bridge.protocol import BridgeError

class DesktopController(GroupController):
    def __init__(self,directory):
        super().__init__(directory)
        try:self.spatial=config(self.settings.data.get('spatial'))
        except BridgeError:self.spatial=config()
        self.room_task=None;self.room_plan=None;self.room_state={'status':'idle','progress':0};self.mics=[]
        # No timed URL replacement by default: it audibly cuts active music.
        # Preserve an explicit new-style user choice; old four-minute workaround remains optional.
        self.maintenance.interval=int(self.settings.data.get('desktop_refresh_seconds',0))
        self.settings.data['refresh_interval_seconds']=self.maintenance.interval
        self.device_choice=self.settings.data.get('desktop_capture_choice',{})
        self.pending_setup_id=''
    async def _watch(self):
        while True:
            await asyncio.sleep(2);self.last_ui_seen=time.monotonic()
            if self.room_task and not self.room_task.done():continue
            if self.media.kind!='live' or not self.media.capture:continue
            stats=self.media.capture.stats();elapsed=time.monotonic()-self.media.started_at
            active=any(p.media.last_request and time.monotonic()-p.media.last_request<35 for p in self.all_peers())
            reason=''
            if stats.get('source')=='generated_test' and elapsed>30:reason='Тест завершён.'
            elif not stats.get('running'):reason=stats.get('error') or 'Захват остановился.'
            elif elapsed>45 and not active:reason='Колонки перестали забирать поток; захват остановлен.'
            if reason and not self.action_lock.locked():
                async with self.action_lock:await self.stop_playback();self.log(reason)
    def configure_readers(self):
        c=self.media.capture
        for p in self.all_peers():
            ident=p.device['id']
            if self.room_plan:
                from .calibration import ProbeProcessor
                p.media.processor_factory=lambda cap,ident=ident:ProbeProcessor(cap,self.room_plan,ident)
            elif c and c.transport=='pcm':
                cfg=copy.deepcopy(self.spatial)
                # Force validation before sending commands, not during HTTP request.
                Processor(c.rate,c.channels,cfg,ident)
                p.media.processor_factory=lambda cap,ident=ident,cfg=cfg:Processor(cap.rate,cap.channels,cfg,ident)
            else:p.media.processor_factory=None
    async def send_audio(self,url,title,hls=False):
        self.configure_readers()
        return await super().send_audio(url,title,hls)
    async def start_live_playback(self,data,*,synthetic=False):
        data=dict(data)
        if not synthetic and self.device_choice and not self.settings.data.get('desktop_endpoint_id'):
            data.update(self.device_choice)
        if self.spatial['enabled']:
            if data.get('playback_mode')=='fallback':raise BridgeError('Разделение каналов работает в PCM. Выберите основной или экспериментальный PCM.')
            # The channel order MUST be explicit, no automatic guesses for multichannel.
            selected=self.endpoint_state.get('selected') or {}
            mix=selected.get('mix_format') or {}
            layout=self.spatial['layout']
            if layout!='stereo' and mix.get('channel_mask')!=MASKS[layout]:
                raise BridgeError('Маска каналов Windows не подтверждает '+layout+'. Настройте выбранный выход 5.1/7.1, затем обновите устройства.')
        self.media.allow_multichannel=bool(self.spatial['enabled'])
        # Source-validation happens before replacing a currently playing stream.
        return await super().start_live_playback(data,synthetic=synthetic)
    async def advanced_action(self,command,data):
        if command=='room_cancel':
            if self.room_task and not self.room_task.done():self.room_task.cancel()
            return {}
        if self.room_task and not self.room_task.done() and command not in ('native_refresh','master_volume','master_mute'):
            raise BridgeError('Сначала завершите или отмените измерение микрофоном.')
        if command not in ('spatial_save','preset21','room_start','room_apply','mic_list','set_capture','refresh_policy','export_native','prepare_output'):
            return await super().advanced_action(command,data)
        async with self.action_lock:
            if command=='spatial_save':
                if self.media.kind=='live':raise BridgeError('Остановите передачу перед изменением каналов.')
                value=config(data.get('config'))
                self.spatial=value;self.settings.data['spatial']=value;self.settings.save();return {}
            if command=='preset21':
                if self.media.kind=='live':raise BridgeError('Остановите передачу перед выбором 2.1.')
                value=preset21(data.get('left'),data.get('bass'),data.get('right'))
                ids={p.device['id'] for p in self.all_peers()}
                if not set(value['speakers']).issubset(ids):raise BridgeError('Сначала подключите все три колонки.')
                self.spatial=value;self.settings.data['spatial']=value;self.settings.save();return {}
            if command=='set_capture':
                if self.media.kind=='live':raise BridgeError('Остановите передачу перед сменой источника.')
                row=next((x for x in self.outputs if x['key']==data.get('key')),None)
                if not row:raise BridgeError('Аудиовыход не найден.')
                self.device_choice={'device_key':row['key'],'device_index':row['index']}
                self.settings.data['desktop_capture_choice']=self.device_choice
                self.settings.data['desktop_endpoint_id']='';self.settings.save();return {}
            if command=='refresh_policy':
                seconds=data.get('seconds')
                if isinstance(seconds,bool) or seconds not in (0,120,240,480,600):raise BridgeError('Неверный интервал.')
                self.maintenance.interval=seconds;self.settings.data['desktop_refresh_seconds']=seconds
                self.settings.save();return {}
            if command=='mic_list':
                from .calibration import microphones
                self.mics=await asyncio.to_thread(microphones);return {'microphones':self.mics}
            if command=='room_start':
                if data.get('confirmed') is not True:raise BridgeError('Подтвердите временное включение микрофона.')
                if not self.all_peers():raise BridgeError('Подключите колонки.')
                if len(self.all_peers())>16:raise BridgeError('Максимум 16 колонок.')
                mic=next((m for m in self.mics if m['index']==data.get('index')),None)
                if not mic:raise BridgeError('Выберите микрофон после обновления списка.')
                from .calibration import run
                self.room_task=asyncio.create_task(run(self,copy.deepcopy(mic)));return {}
            if command=='room_apply':
                from .calibration import signature
                result=self.room_state.get('result') or {}
                if not result.get('valid') or self.room_state.get('signature')!=signature(self):raise BridgeError('Замер недостоверен или состав/настройки изменились. Повторите измерение.')
                if not self.spatial['enabled']:raise BridgeError('Сначала включите и сохраните роли каналов, затем повторите замер.')
                if self.media.kind=='live':raise BridgeError('Остановите передачу перед применением задержек.')
                value=copy.deepcopy(self.spatial)
                for r in result['rows']:
                    value['speakers'].setdefault(r['id'],{'role':'stereo'})['delay_ms']=r['correction_ms']
                self.spatial=config(value);self.settings.data['spatial']=self.spatial
                self.settings.data['room_calibration']=result;self.settings.save();return {}
            if command=='prepare_output':
                if data.get('confirmed') is not True:raise BridgeError('Нужно согласие на установку отдельного драйвера и переименование выхода.')
                if self.media.kind=='live':raise BridgeError('Остановите звук перед настройкой выхода.')
                from .native_helper import rename_endpoint,install_driver
                rows=[x for x in self.endpoint_state.get('endpoints',[]) if x.get('virtual_cable')]
                ident=data.get('id') or self.settings.data.get('desktop_endpoint_id')
                selected=next((x for x in rows if x['id']==ident),None)
                if not selected and len(rows)==1:selected=rows[0]
                if len(rows)>1 and not selected:raise BridgeError('Найдено несколько виртуальных кабелей. Сначала выберите нужный выход.')
                if selected:
                    self.settings.data['desktop_endpoint_id']=selected['id'];self.settings.save()
                    if selected['name']!='Яндекс станция':await asyncio.to_thread(rename_endpoint,selected['id'])
                    else:await self.bind_endpoint(selected['id'])
                    return {'stage':'renaming'}
                await asyncio.to_thread(install_driver);return {'stage':'vendor_wizard'}
            if command=='export_native':
                # Deliberately excludes secrets, QR, full URLs and stored settings.
                result={'application':'Yandex Station Advanced','version':__import__('advanced').__version__,
                    'spatial':self.spatial,'room_calibration':self.room_state.get('result'),
                    'peers':[{'name':p.device['name'],'state':p.glagol.safe_state(),'media':p.media.stats(),
                              'processing':getattr(p.media,'last_processor',None).stats() if getattr(p.media,'last_processor',None) else None} for p in self.all_peers()],
                    'previous_live':list(self.media.live_history),'continuity':self.continuity_snapshot()}
                path=self.settings.directory/('YSA-diagnostic-'+time.strftime('%Y%m%d-%H%M%S')+'.json')
                path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                self.log('Диагностика сохранена: '+str(path));return {'path':str(path)}
        return {}
    async def action(self,command,data):
        if self.room_task and not self.room_task.done():
            if command in ('stop','shutdown','disconnect'):
                self.room_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):await self.room_task
            elif command not in ('heartbeat',):raise BridgeError('Идёт микрофонная проверка. Сначала отмените её.')
        return await super().action(command,data)
    def native_snapshot(self):
        result=self.snapshot();result['desktop']=self.advanced_snapshot()
        result['spatial']=copy.deepcopy(self.spatial);result['room']=copy.deepcopy(self.room_state)
        result['microphones']=copy.deepcopy(self.mics)
        result['qr_matrix']=self.qr_matrix if self.qr_status=='waiting' else None
        return result
    async def close(self):
        if self.room_task and not self.room_task.done():
            self.room_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.room_task
        await super().close()
