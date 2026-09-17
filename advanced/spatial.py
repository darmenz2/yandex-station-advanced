"""Validated per-speaker PCM routing. Stereo bass management is not an LFE track.

The native engine processes interleaved S16LE to stereo S16LE without allocations.
Crossover is Linkwitz-Riley 4 (two Butterworth biquads), computed at source rate.
Each HTTP reader owns its filter/delay state. No filter history is shared by peers.
"""
from __future__ import annotations
import array
import ctypes as C
from dataclasses import dataclass
import math
import os
from pathlib import Path
from station_bridge.protocol import BridgeError

ROLES = {'stereo':'Стерео целиком', 'left':'Левый', 'right':'Правый',
         'bass':'Бас из L+R', 'center':'Центр', 'lfe':'LFE',
         'side_left':'Боковой левый', 'side_right':'Боковой правый',
         'rear_left':'Задний левый', 'rear_right':'Задний правый', 'off':'Не играет'}
LAYOUTS = {'stereo':('left','right'), '5.1-back':('left','right','center','lfe','rear_left','rear_right'),
           '5.1-side':('left','right','center','lfe','side_left','side_right'),
           '7.1':('left','right','center','lfe','rear_left','rear_right','side_left','side_right')}
MASKS={'stereo':3, '5.1-back':0x3f,'5.1-side':0x60f,'7.1':0x63f}

def number(v, lo, hi, name):
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not lo<=v<=hi:
        raise BridgeError(f'{name}: допустимо {lo}–{hi}.')
    return float(v)

def config(raw=None):
    raw=raw or {}
    if not isinstance(raw,dict):raise BridgeError('Некорректная конфигурация каналов.')
    layout=raw.get('layout','stereo')
    if layout not in LAYOUTS:raise BridgeError('Неизвестная схема входных каналов.')
    enabled=raw.get('enabled',False)
    if not isinstance(enabled,bool):raise BridgeError('Некорректное включение каналов.')
    cross=number(raw.get('crossover_hz',120),40,1500,'Раздел баса / нижней середины, Гц')
    rows=raw.get('speakers',{})
    if not isinstance(rows,dict) or len(rows)>16:raise BridgeError('Не более 16 назначений.')
    out={}
    for ident,r in rows.items():
        if not isinstance(ident,str) or not 1<=len(ident)<=256 or not isinstance(r,dict):raise BridgeError('Некорректное назначение колонки.')
        role=r.get('role','stereo')
        if role not in ROLES:raise BridgeError('Неизвестная роль колонки.')
        if role not in ('stereo','bass','off') and role not in LAYOUTS[layout]:
            raise BridgeError('Роль «'+ROLES[role]+'» отсутствует во входном '+layout+'. Стерео не превращается в настоящий 5.1.')
        invert=r.get('invert',False)
        if not isinstance(invert,bool):raise BridgeError('Некорректная полярность.')
        out[ident]={'role':role,'gain_db':number(r.get('gain_db',0),-24,6,'Уровень, дБ'),
                    'delay_ms':number(r.get('delay_ms',0),0,1000,'Задержка, мс'),'invert':invert}
    return {'enabled':enabled,'layout':layout,'crossover_hz':cross,'headroom_db':number(raw.get('headroom_db',0),-12,0,'Запас уровня, дБ'),'speakers':out}

def preset21(left,bass,right):
    if len({left,bass,right})!=3 or not all(isinstance(x,str) and x for x in (left,bass,right)):
        raise BridgeError('Для 2.1 выберите три разные колонки: Лайт слева, Миди, Лайт справа.')
    return config({'enabled':True,'layout':'stereo','crossover_hz':120,
        'speakers':{left:{'role':'left'},bass:{'role':'bass','gain_db':-3},right:{'role':'right'}}})

def coefficients(hz,rate,high=False):
    w=2*math.pi*hz/rate;c=math.cos(w);s=math.sin(w);a=s/math.sqrt(2);den=1+a
    b=((1+c)/2,-(1+c),(1+c)/2) if high else ((1-c)/2,1-c,(1-c)/2)
    return [*(x/den for x in b),-2*c/den,(1-a)/den]

class State(C.Structure):
    _fields_=[('matrix',C.c_double*16),('coef',C.c_double*10),('history',C.c_double*8),
              ('gain',C.c_double),('channels',C.c_int32),('filter_kind',C.c_int32),
              ('clipped',C.c_uint64)]

_library=None

def library():
    global _library
    if _library is None:
        name='spatial_dsp.dll' if os.name=='nt' else 'spatial_dsp.so'
        path=Path(__file__).resolve().parent/'native'/name
        if not path.is_file():raise BridgeError('Отсутствует модуль каналов. Повторите установку приложения.')
        lib=C.CDLL(str(path))
        lib.ysa_process.argtypes=[C.POINTER(State),C.c_void_p,C.c_void_p,C.c_uint32]
        lib.ysa_process.restype=C.c_int32
        lib.ysa_correlate.argtypes=[C.POINTER(C.c_float),C.c_uint32,C.POINTER(C.c_float),C.c_uint32,C.POINTER(C.c_double)]
        lib.ysa_correlate.restype=C.c_int32
        _library=lib
    return _library

class Processor:
    """One stateful chain per HTTP reader; delay adds silence, never skips beginning."""
    def __init__(self,rate,channels,settings=None,ident=''):
        cfg=config(settings)
        if not 8000<=rate<=192000 or not 1<=channels<=8:raise BridgeError('Неподдерживаемый PCM.')
        layout=cfg['layout'];layout_roles=LAYOUTS[layout]
        if cfg['enabled'] and channels!=len(layout_roles):
            raise BridgeError(f'Схема {layout} требует {len(layout_roles)} каналов, источник отдаёт {channels}. Настройте Windows или выберите стерео.')
        if not cfg['enabled'] and channels>2:raise BridgeError('Для многоканального источника включите роли и задайте точную схему входа.')
        self.state=State();self.state.channels=channels;self.rate=rate;self.input_channels=channels
        row=cfg['speakers'].get(ident,{'role':'off'}) if cfg['enabled'] else {'role':'stereo'}
        role=row['role'];mat=[[0.]*8 for _ in range(2)]
        if role=='stereo':
            mat[0][0]=1;mat[1][min(1,channels-1)]=1
        elif role=='bass':
            # Explicit front L/R bass extraction; this does NOT mix the LFE track.
            for m in mat:m[0]=m[1]=.5
        elif role!='off':
            for m in mat:m[layout_roles.index(role)]=1.
        for i,m in enumerate(mat):
            for j,v in enumerate(m):self.state.matrix[i*8+j]=v
        self.state.gain=10**((row.get('gain_db',0)+cfg.get('headroom_db',0))/20)*(-1 if row.get('invert',False) else 1)
        has_bass=cfg['enabled'] and any(x['role']=='bass' for x in cfg['speakers'].values())
        kind=1 if role=='bass' else 2 if has_bass and role in ('left','right','stereo') else 0
        self.state.filter_kind=kind
        for i,v in enumerate(coefficients(cfg['crossover_hz'],rate,False)+coefficients(cfg['crossover_hz'],rate,True)):self.state.coef[i]=v
        self.delay_bytes=round(rate*row.get('delay_ms',0)/1000)*4
        self.delay=bytearray(self.delay_bytes)
        self.native=library()
        self.bypass=role=='stereo' and channels==2 and kind==0 and self.state.gain==1 and not self.delay_bytes
    def process(self,raw):
        if len(raw)%(2*self.input_channels):raise BridgeError('Неполный PCM-кадр.')
        if self.bypass:return raw
        frames=len(raw)//(2*self.input_channels)
        out=C.create_string_buffer(frames*4)
        if self.native.ysa_process(C.byref(self.state),raw,out,frames)!=0:raise BridgeError('Ошибка обработки каналов.')
        result=out.raw
        if self.delay_bytes:
            self.delay.extend(result);result=bytes(self.delay[:len(result)]);del self.delay[:len(result)]
        return result
    def reset(self):
        for i in range(8):self.state.history[i]=0
        self.delay=bytearray(self.delay_bytes)
    def stats(self):
        return {'clipped_samples':self.state.clipped,'delay_ms':round(self.delay_bytes/4/self.rate*1000,2)}
