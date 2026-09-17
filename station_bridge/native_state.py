"""Read-only, bounded projection of Yandex AppState protobuf.

Field numbers are from v3rt3p/quasar-protos model_objects.proto (firmware
extraction), not an official external API. This module neither sends private
protocol messages nor extracts playback URLs, cookies, audio or tokens.
"""
from __future__ import annotations
import base64
import hashlib
import math
import struct


def wire(raw: bytes) -> dict:
    if len(raw) > 131072:
        raise ValueError('Protobuf too large')
    out = {}; offset = 0; count = 0
    def varint():
        nonlocal offset
        value = 0
        for shift in range(0,70,7):
            if offset >= len(raw):raise ValueError('Truncated varint')
            byte=raw[offset];offset+=1;value|=(byte&127)<<shift
            if not byte&128:
                if value > 0xffffffffffffffff:raise ValueError('Varint overflow')
                return value
        raise ValueError('Long varint')
    while offset < len(raw):
        count += 1
        if count > 1024:raise ValueError('Too many fields')
        tag=varint();field,kind=tag>>3,tag&7
        if not field or field>0x1fffffff:raise ValueError('Invalid field')
        if kind==0:value=varint()
        elif kind in (1,2,5):
            size=varint() if kind==2 else 8 if kind==1 else 4
            if size>len(raw)-offset:raise ValueError('Truncated field')
            value=raw[offset:offset+size];offset+=size
        else:raise ValueError('Unsupported wire type')
        out[field]=(kind,value)
    return out


def nested(fields, number):
    value=fields.get(number)
    return wire(value[1]) if value and value[0]==2 else {}


def observe_app_state(message: dict) -> dict:
    """Absence of these fields means UNKNOWN, never synchronised or failed."""
    extra=message.get('extra')
    if not isinstance(extra,dict):return {}
    value=extra.get('appState')
    if not isinstance(value,str) or len(value)>180000:return {}
    try:
        app=wire(base64.b64decode(value,validate=True))
        event=nested(app,6)  # AppState.audio_player_state
        audio=nested(event,3)  # AudioClientEvent.audio
        if not event:return {}
        result={'source':'firmware_app_state_read_only'}
        for key,field,where in [('player_state',2,event),('last_play_timestamp',5,event),
                               ('format_enum',4,audio),('basetime_ns',13,audio),
                               ('position_ns',14,audio),('input_stream_rate',15,audio),
                               ('position_ms',28,audio),('position_time_point',29,audio),
                               ('channels_count',36,audio)]:
            v=where.get(field)
            if v and v[0]==0:result[key]=v[1]
        flag=event.get(17)
        if flag and flag[0]==0:result['has_audio_focus']=bool(flag[1])
        speed=event.get(12)
        if speed and speed[0]==1:
            number=struct.unpack('<d',speed[1])[0]
            if math.isfinite(number):result['reported_playback_speed']=number
        clock=audio.get(23)
        if clock and clock[0]==2 and clock[1]:
            result['clock_fingerprint']=hashlib.sha256(clock[1]).hexdigest()[:16]
        context=nested(audio,30)
        for key,field in [('multiroom_mode',1),('stereo_pair_role',3)]:
            v=context.get(field)
            if v and v[0]==2:
                role=v[1].decode('utf-8','replace').lower()
                result[key]=role if role in ('master','slave','leader','follower','none','standalone','stand_alone') else 'unrecognized'
        result['clock_control_available']=False
        return result
    except (ValueError,TypeError,struct.error):return {}


def stereo_configuration(item: dict) -> dict:
    """Only documented-by-community config keys; never infer role from model/IP."""
    cfg=item.get('config')
    if not isinstance(cfg,dict):return {}
    pair=cfg.get('stereo_pair')
    if not isinstance(pair,dict):return {}
    role=pair.get('role');channel=pair.get('channel');partner=pair.get('partnerDeviceId')
    if role not in ('leader','follower') or channel not in ('left','right'):
        return {}
    if not isinstance(partner,str) or not 1<=len(partner)<=256:return {}
    return {'role':role,'channel':channel,'partner_id':partner}


def configured_pairs(devices: list[dict]) -> list[dict]:
    by_id={d['id']:d for d in devices if isinstance(d,dict) and d.get('id')}
    result=[]
    for ident,leader in by_id.items():
        pair=leader.get('native_stereo') or {}
        if pair.get('role')!='leader':continue
        follower=by_id.get(pair.get('partner_id'),{})
        other=follower.get('native_stereo') or {}
        if other.get('role')!='follower' or other.get('partner_id')!=ident:continue
        if pair.get('channel')==other.get('channel'):continue
        result.append({'leader_id':ident,'follower_id':follower['id'],
                       'leader_name':leader.get('name',ident),'follower_name':follower.get('name',follower['id']),
                       'leader_channel':pair['channel'],'verified_by':'reciprocal_cloud_configuration',
                       'pcm_supported':None,'acoustic_sync_verified':False})
    return result
