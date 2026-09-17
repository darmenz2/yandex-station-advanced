"""Observable command ownership. Repeated in-flight commands join the same task.

Values (including QR/auth data) are not put into operation history. Distinct
state-changing commands are rejected while one is running, not silently queued.
"""
from __future__ import annotations
import asyncio
from collections import deque
import hashlib
import json
import time
from station_bridge.protocol import BridgeError

LABELS = {
 'native_detect':'Чтение штатной конфигурации Яндекса',
 'native_music':'Запуск штатного мультирума Яндекса',
 'native_pair_probe':'Испытание PCM на штатной паре',
 'native_help':'Инструкция по штатной стереопаре',
 'pcm_reserve':'Выбор защитного резерва PCM',
 'scan':'Поиск колонок', 'qr':'Получение QR-кода', 'live':'Запуск передачи',
 'start':'Запуск передачи', 'stop':'Остановка звука', 'disconnect':'Отключение колонок',
 'shutdown':'Завершение приложения', 'connect_group':'Подключение выбранных колонок',
 'apply_setup':'Настройка и запуск системы', 'confirm_setup':'Подтверждение подключения',
 'preset21':'Назначение всех каналов', 'spatial_save':'Сохранение каналов',
 'room_start':'Подготовка микрофонного замера', 'room_apply':'Применение калибровки',
 'prepare_output':'Подготовка аудиовыхода Windows', 'native_refresh':'Обновление аудиовыходов',
 'peer_approve':'Разрешение аудио через маршрутизатор', 'resync':'Плавное приближение к живому звуку',
 'peer_retry':'Повтор только для выбранной колонки', 'bind_endpoint':'Выбор аудиовыхода',
 'set_capture':'Выбор источника звука', 'test':'Проверка звука', 'test_live':'Проверка потока',
 'export_native':'Сохранение диагностики', 'refresh_policy':'Сохранение режима устойчивости',
 'smooth_policy':'Сохранение плавной коррекции', 'reset_spatial':'Обычное воспроизведение',
 'classic_sound':'Открытие параметров звука Windows', 'logout':'Выход из аккаунта', 'mic_list':'Поиск микрофонов'}
PASSIVE={'heartbeat','master_volume','master_mute','station_volume','room_cancel'}

class Operations:
    def __init__(self):
        self.active=None
        self.task=None
        self.key=None
        self.serial=0
        self.history=deque(maxlen=12)
        self.last=None
    def stage(self,text):
        if self.active:self.active['detail']=text
    def snapshot(self):
        active=dict(self.active) if self.active else None
        if active:active['elapsed_seconds']=round(time.monotonic()-active.pop('_started'),1)
        return {'busy':bool(active),'active':active,'last':self.last,'history':list(self.history)}
    async def run(self,name,data,fn):
        if name in PASSIVE:return await fn()
        raw=json.dumps([name,data],ensure_ascii=False,sort_keys=True,default=str)
        key=hashlib.sha256(raw.encode()).hexdigest()
        if self.task and not self.task.done():
            if key==self.key:return await asyncio.shield(self.task)
            raise BridgeError('Сейчас: '+self.active['label']+'. Дождитесь результата — повторная операция не поставлена в очередь.')
        self.serial+=1
        self.active={'id':self.serial,'command':name,'label':LABELS.get(name,'Выполнение команды'),
                     'detail':'Начато…','status':'running','_started':time.monotonic()}
        self.key=key
        async def execute():
            try:
                result=await fn()
                needs_trust=bool(isinstance(result,dict) and result.get('trust_required'))
                self.active.update(status='needs_confirmation' if needs_trust else 'done',
                                   detail=(result.get('message') if isinstance(result,dict) else None) or
                                   ('Нужно подтвердить сертификаты своих колонок.' if needs_trust else 'Готово.'))
                return result
            except asyncio.CancelledError:
                self.active.update(status='cancelled',detail='Операция отменена.')
                raise
            except Exception as exc:
                self.active.update(status='error',detail=str(exc) if isinstance(exc,BridgeError) else type(exc).__name__)
                raise
            finally:
                item=dict(self.active);item['elapsed_seconds']=round(time.monotonic()-item.pop('_started'),1)
                item['time']=time.strftime('%H:%M:%S');self.last=item;self.history.append(item);self.active=None
        self.task=asyncio.create_task(execute())
        return await asyncio.shield(self.task)
    async def close(self):
        task=self.task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
