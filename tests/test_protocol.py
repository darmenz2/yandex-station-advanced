import base64
import json
import time
import pytest
from station_bridge.protocol import (BridgeError, audio_play, envelope, external_command, proto_string, say_text, varint)
from station_bridge.discovery import merge_devices
from station_bridge.security import validate_lan_ip, Settings


def read_varint(data, offset):
    result = shift = 0
    while True:
        b = data[offset]; offset += 1
        result |= (b & 127) << shift
        if b < 128: return result, offset
        shift += 7


def decode_fields(data):
    pos = 0; fields = {}
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        length, pos = read_varint(data, pos)
        assert tag & 7 == 2
        fields[tag >> 3] = data[pos:pos+length].decode('utf-8')
        pos += length
    return fields


@pytest.mark.parametrize('value', [0, 1, 127, 128, 16384, 2**32])
def test_varint(value):
    assert read_varint(varint(value), 0)[0] == value


def test_negative_varint():
    with pytest.raises(ValueError): varint(-1)


@pytest.mark.parametrize('hls,fmt,kind', [(False,'MP3','Track'), (True,'HLS','FmRadio')])
def test_audio_play_protobuf(hls, fmt, kind):
    url = 'http://192.168.1.5:8808/m/ABC/live.m3u8' if hls else 'http://192.168.1.5:8808/m/ABC/audio.wav'
    result = audio_play(url, 'Проверка кириллицы', hls)
    assert result['command'] == 'externalCommandBypass'
    fields = decode_fields(base64.b64decode(result['data']))
    assert fields[1] == 'audio_play'  # no obsolete radio_play
    payload = json.loads(fields[2])
    assert payload['stream']['format'] == fmt
    assert payload['stream']['type'] == kind
    assert payload['stream']['url'] == url
    assert payload['metadata']['title'] == 'Проверка кириллицы'


def test_url_validation():
    for url in ['file:///etc/passwd', 'http://' + 'x'*300]:
        with pytest.raises(BridgeError): audio_play(url)


def test_envelope():
    result = envelope('TEST_SECRET', {'command':'ping'}, 'request-id')
    assert result['id'] == 'request-id'
    assert result['conversationToken'] == 'TEST_SECRET'
    assert abs(result['sentTime'] - time.time()*1000) < 1000


def test_tts_shape():
    payload = say_text('  Привет! ')
    event = payload['serverActionEventPayload']
    assert event['name'] == 'update_form'
    assert event['payload']['form_update']['slots'][0]['value'] == 'Привет!'
    for text in ['', 'x'*1001]:
        with pytest.raises(BridgeError): say_text(text)


@pytest.mark.parametrize('value', ['10.1.2.3','192.168.1.7','172.16.4.3','172.31.255.254','169.254.10.4'])
def test_local_ipv4(value):
    assert validate_lan_ip(value) == value


@pytest.mark.parametrize('value', ['127.0.0.1','8.8.8.8','172.32.0.1','169.255.1.1','0.0.0.0','::1','192.168.4.999','example.org','224.0.0.251'])
def test_reject_non_lan(value):
    with pytest.raises(BridgeError): validate_lan_ip(value)


def test_discovery_merge():
    cloud = [{'id':'a','name':'Миди в кабинете','host':'','port':1961,'platform':'real_platform'},
             {'id':'b','name':'Кухня','host':'','port':1961,'platform':'platform_b'}]
    local = [{'id':'a','name':'local','host':'192.168.1.3','port':1961,'platform':'real_platform'}]
    result = merge_devices(cloud,local)
    assert len(result) == 2
    assert result[0]['name'] == 'Миди в кабинете'
    assert result[0]['host'] == '192.168.1.3'
    assert result[1]['host'] == ''


def test_settings_not_plaintext_credentials(tmp_path):
    cfg = Settings(tmp_path)
    cfg.data['device'] = {'host':'192.168.1.2'}
    cfg.save()
    assert Settings(tmp_path).data['device']['host'] == '192.168.1.2'
    (tmp_path/'settings.json').write_text('[]')
    assert Settings(tmp_path).data == {}
