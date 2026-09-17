import asyncio
import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import aiohttp
from aiohttp import web
import pytest
from station_bridge.app import Controller, make_ui_app
from station_bridge.media import MediaServer
from station_bridge.protocol import BridgeError, GlagolClient, audio_play
from station_bridge.security import get_fingerprint


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


async def test_media_range_head_and_isolation(tmp_path):
    logs=[]; media=MediaServer(tmp_path/'cache',logs.append)
    port=free_port()
    await media.bind('127.0.0.1','127.0.0.1',port)
    try:
        await media.new_resource('test','test')
        data=bytes(range(256))*100
        path=media.directory/'audio.mp3'; path.write_bytes(data)
        url=media.url(path.name)
        async with aiohttp.ClientSession() as session:
            async with session.head(url) as r:
                assert r.status == 200
                assert r.headers['Content-Length'] == str(len(data))
                assert not await r.read()
                assert media.requests == 0
            async with session.get(url,headers={'Range':'bytes=123-456'}) as r:
                assert r.status == 206
                assert r.headers['Content-Range'] == f'bytes 123-456/{len(data)}'
                assert await r.read() == data[123:457]
                assert r.headers.get('Transfer-Encoding') is None
            async with session.get(url) as r:
                assert await r.read() == data
                assert r.headers['Content-Type'] == 'audio/mpeg'
            assert media.requests == 2
            for route in ['/settings.json',f'/m/{media.token}/settings.json', '/m/invalid/audio.mp3', f'/m/{media.token}/..%2F..%2Fsettings.json']:
                async with session.get(f'http://127.0.0.1:{port}'+route) as r:
                    assert r.status == 404
            old=url
            await media.stop()
            async with session.get(old) as r:
                assert r.status == 404
            await media.new_resource('test','test')
            (media.directory/'audio.wav').symlink_to(path)  # non-existing and still must reject
            async with session.get(media.url('audio.wav')) as r:
                assert r.status == 404
    finally: await media.close()


async def test_media_forbids_unselected_peer(tmp_path):
    media=MediaServer(tmp_path/'cache',lambda msg:None)
    await media.bind('127.0.0.1', '192.168.1.64', free_port())
    try:
        await media.test_resource()
        class Request:
            remote = '192.168.1.99'
            match_info = {'token': media.token, 'name': 'audio.wav'}
            method = 'GET'
        with pytest.raises(web.HTTPForbidden):
            await media.handle(Request())
        assert media.requests == 0
    finally:
        await media.close()


async def test_local_ui_security_and_static(tmp_path):
    controller=Controller(tmp_path/'settings')
    await controller.start()
    port=free_port(); secret='LOCAL_UI_KEY'
    app=make_ui_app(controller,secret,port)
    runner=web.AppRunner(app)
    await runner.setup(); await web.TCPSite(runner,'127.0.0.1',port).start()
    base=f'http://127.0.0.1:{port}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base+'/') as r:
                html=await r.text()
                assert 'Station Bridge' in html
                assert secret not in html
                assert r.headers['X-Frame-Options']=='DENY'
            async with session.get(base+'/api/state') as r:
                assert r.status == 401
            async with session.get(base+'/api/state',headers={'X-Bridge-Key':secret,'Origin':'https://evil.example'}) as r:
                assert r.status == 403
            async with session.get(base+'/api/state',headers={'X-Bridge-Key':secret,'Host':'evil.example'}) as r:
                assert r.status == 403
            async with session.get(base+'/api/state',headers={'X-Bridge-Key':secret}) as r:
                assert r.status == 200
                obj=await r.json(); assert obj['auth']['logged_in'] is False
                raw=json.dumps(obj); assert 'x_token' not in raw and 'music_token' not in raw
            async with session.post(base+'/api/action',json={'command':'test'},headers={'X-Bridge-Key':secret}) as r:
                assert r.status == 400
                assert 'подключ' in (await r.json())['error']
            async with session.get(base+'/api/diagnostic',headers={'X-Bridge-Key':secret}) as r:
                report=await r.text()
                assert 'x_token' not in report and secret not in report
            for resource in ['/app.js','/style.css']:
                async with session.get(base+resource) as r:
                    assert r.status==200
            async with session.get(base+'/main.py') as r: assert r.status==404
    finally:
        await runner.cleanup(); await controller.close()


def certificate(tmp_path):
    cryptography=pytest.importorskip('cryptography')
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Mock Station')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
          .not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
              x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256()))
    pem=tmp_path/'cert.pem'; priv=tmp_path/'key.pem'
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(pem,priv)
    fp=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return ctx,fp


async def test_glagol_tls_fingerprint_and_immediate_ack(tmp_path):
    ctx,fp=certificate(tmp_path); received=[]; logs=[]
    async def handler(request):
        ws=web.WebSocketResponse(); await ws.prepare(request)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT: continue
            data=json.loads(msg.data); received.append(data)
            status='DENIED' if data['payload'].get('command')=='bad' else 'SUCCESS'
            # Reply immediately; catches the classic set-waiter-after-send race.
            await ws.send_json({'requestId':data['id'],'status':status,'state':{
                'playing':False,'volume':.2,'aliceState':'IDLE','playerState':{'title':'Mock'}}})
        return ws
    app=web.Application(); app.router.add_get('/',handler)
    runner=web.AppRunner(app); await runner.setup()
    port=free_port(); await web.TCPSite(runner,'127.0.0.1',port,ssl_context=ctx).start()
    async with aiohttp.ClientSession() as session:
        gl=GlagolClient(session,lambda state:None,logs.append)
        try:
            assert await get_fingerprint('127.0.0.1',port)==fp
            await gl.connect('127.0.0.1',port,'TEST_CONVERSATION_TOKEN',fp)
            result=await gl.send(audio_play('http://192.168.1.2/audio.mp3'))
            assert result['acknowledged']
            assert not gl.waiters
            assert received[-1]['conversationToken']=='TEST_CONVERSATION_TOKEN'
            assert 'TEST_CONVERSATION_TOKEN' not in json.dumps(gl.safe_state())
            with pytest.raises(BridgeError): await gl.send({'command':'bad'})
            assert not gl.waiters
            await gl.close()
            with pytest.raises(BridgeError): await gl.connect('127.0.0.1',port,'TEST_CONVERSATION_TOKEN','00'*32)
        finally: await gl.close()
    await runner.cleanup()
