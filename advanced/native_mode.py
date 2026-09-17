"""Explicit delegation to Yandex, kept separate from software PCM distribution.

This is not a reimplementation of the firmware clock protocol. Normal native
music uses the supported voice command via existing sendText. The PCM pair
probe targets only the leader of an already configured reciprocal stereo pair;
compatibility with forwarding a local WAV remains an on-device test.
"""
from __future__ import annotations
import copy
import time
from station_bridge.protocol import BridgeError
from station_bridge.native_state import configured_pairs
from .spatial import config


class NativeMode:
    def __init__(self, directory):
        super().__init__(directory)
        self.native_inventory=[]
        self.native_loaded_at=0.
        self.native_source={'mode':'off','status':'idle'}

    async def detect_native(self):
        if not self.auth.logged_in:raise BridgeError('Сначала войдите в аккаунт владельца колонок.')
        devices=await self.auth.devices()
        self.native_inventory=copy.deepcopy(devices)
        self.native_loaded_at=time.monotonic()
        count=len(configured_pairs(devices))
        return {'message':f'Подтверждено штатных стереопар: {count}. Отсутствие полей не доказывает, что пары нет; роли не угадываются.'}

    async def stop_native_music(self):
        if self.native_source.get('mode')!='yandex_music':return
        p=next((p for p in self.all_peers() if p.device['id']==self.native_source.get('control_id')),None)
        try:
            if not p or not p.glagol.connected:raise BridgeError('Нет связи с главной колонкой.')
            await p.glagol.send({'command':'sendText','text':'останови музыку везде'})
            self.native_source={'mode':'off','status':'idle'}
        except BridgeError:
            warning='Остановка штатной музыки не подтверждена. Скажите «Алиса, останови музыку везде»; локальное приложение закроется независимо от сети.'
            self.log(warning)
            self.native_source={'mode':'off','status':'stop_unconfirmed','warning':warning}

    async def stop_playback(self):
        await self.stop_native_music()
        await super().stop_playback()
        if self.native_source.get('status')!='stop_unconfirmed':self.native_source={'mode':'off','status':'idle'}

    async def start_live_playback(self,data,*,synthetic=False):
        if self.native_source.get('mode')=='yandex_music':
            raise BridgeError('Сначала остановите штатную музыку Яндекса. Затем запустите звук Windows.')
        return await super().start_live_playback(data,synthetic=synthetic)

    async def advanced_action(self,command,data):
        if command in ('native_music','native_pair_probe') and self.room_task and not self.room_task.done():
            raise BridgeError('Сначала отмените микрофонный замер.')
        if command=='native_detect':return await self.detect_native()
        if command=='native_music':
            if data.get('confirmed') is not True:raise BridgeError('Подтвердите: звук Windows остановится, а музыка Яндекса включится на всех Станциях дома, не только отмеченных здесь.')
            p=next((p for p in self.all_peers() if p.glagol.connected),None)
            if not p:raise BridgeError('Сначала подключите хотя бы одну Станцию.')
            if self.native_source.get('mode')=='yandex_music':
                state=p.glagol.safe_state()
                fresh_playing=state.get('playing') and state.get('state_age_seconds',99)<10 and state.get('title')!='Звук компьютера'
                if time.monotonic()-self.native_source.get('requested_at',0)<2 or fresh_playing:
                    return {'idempotent':True,'message':'Запрос штатного режима уже отправлен; повторного старта нет.'}
            await self.stop_playback()
            answer=await p.glagol.send({'command':'sendText','text':'включи музыку везде'})
            self.native_source={'mode':'yandex_music','status':'requested','control_id':p.device['id'],
                                'command_acknowledged':bool(answer.get('acknowledged')),'requested_at':time.monotonic()}
            return {'message':'Яндексу отправлена команда «включи музыку везде». Источник — Яндекс Музыка, не Windows. Синхронизацией занимается прошивка; слышимый результат проверьте на колонках.'}
        if command=='native_pair_probe':
            if data.get('confirmed') is not True:raise BridgeError('Подтвердите испытание PCM на уже созданной штатной паре.')
            await self.detect_native()
            pair=next((r for r in configured_pairs(self.native_inventory) if r['leader_id']==data.get('leader_id')),None)
            if not pair:raise BridgeError('В актуальной конфигурации аккаунта не подтверждена эта пара. Создайте её в «Доме с Алисой» и обновите сведения; приложение само пары не создаёт.')
            if self.media.kind=='live' and self.native_source.get('mode')=='pair_pcm_probe' and self.native_source.get('leader_id')==pair['leader_id']:
                return {'idempotent':True,'message':'Испытание этой пары уже запущено; URL не заменён.'}
            leader=next((p.device for p in self.all_peers() if p.device['id']==pair['leader_id'] and p.glagol.connected),None)
            if not leader:raise BridgeError('Главная колонка пары не подключена. Подключите её через «Одна колонка»; адрес и сертификат не угадываются.')
            await self.stop_playback()
            result=await self.connect_group([leader])
            if result.get('trust_required'):return {**result,'message':'Сертификат главной колонки изменился. Подтвердите его через настройку одной колонки и повторите испытание.'}
            if not all(r.get('connected') for r in result.get('results',[])):raise BridgeError('Не подключена главная колонка.')
            self.spatial=config({'enabled':False})
            self.settings.data['spatial']=copy.deepcopy(self.spatial)
            self._last_setup={'mode':'single','roles':{}}
            self.settings.data['desktop_setup']=copy.deepcopy(self._last_setup);self.settings.save()
            result=await self.start_live_playback({'playback_mode':'main'})
            self.native_source={'mode':'pair_pcm_probe','status':'forwarding_unverified',**pair}
            return {**result,'message':'Полный стереопоток отправлен ТОЛЬКО главной колонке. Ведомой команды не отправляются. Это испытание совместимости локального WAV со штатной парой; если играет только одна, автоматически отдельный поток второй не запускается.'}
        if command=='native_help':
            import webbrowser
            webbrowser.open('https://alice.yandex.ru/support/ru/station/settings/stereopair')
            return {'message':'Открыта официальная инструкция. Важно: создание пары может сбросить настройки и сценарии ведомой колонки; приложение не делает этого автоматически.'}
        return await super().advanced_action(command,data)

    def native_mode_snapshot(self):
        peers=[]
        for p in self.all_peers():
            observation=copy.deepcopy(getattr(p.glagol,'native_observation',{}))
            peers.append({'name':p.device.get('name','Станция'),'id':p.device['id'],
                          'state':p.glagol.safe_state(), 'observation':observation,
                          'observations':copy.deepcopy(list(getattr(p.glagol,'native_history',[])))})
        return {'source':copy.deepcopy(self.native_source),
                'pairs':configured_pairs(self.native_inventory), 'peers':peers,
                'configuration_age_seconds':round(time.monotonic()-self.native_loaded_at,1) if self.native_loaded_at else None,
                'note':'Native cloud music != Windows capture. Pair configuration and acknowledgements do not prove local PCM forwarding or acoustic synchronization.'}
