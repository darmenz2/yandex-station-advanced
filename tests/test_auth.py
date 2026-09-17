import json
import pytest
from station_bridge.auth import YandexAuth
from station_bridge.protocol import BridgeError


class Reply:
    def __init__(self, body, status=200): self.body, self.status = body, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def text(self): return self.body if isinstance(self.body,str) else json.dumps(self.body)
    async def read(self): return (await self.text()).encode()


class Session:
    def __init__(self, replies): self.replies=list(replies); self.calls=[]; self.cookie_jar=[]
    def request(self, method, url, **kwargs):
        self.calls.append((method,url,kwargs.copy()))
        return self.replies.pop(0)
    def get(self, url, **kwargs): return self.request('GET',url,**kwargs)
    def post(self, url, **kwargs): return self.request('POST',url,**kwargs)


async def test_glagol_refresh_preserves_device_parameters():
    session=Session([Reply({},403),Reply({'access_token':'REFRESHED'}),Reply({'status':'ok','token':'DEVICE'})])
    auth=YandexAuth(session); auth.x_token='X'; auth.music_token='OLD'
    assert await auth.device_token('DEVICE_ID','REAL_PLATFORM')=='DEVICE'
    assert session.calls[0][2]['params']==session.calls[2][2]['params']=={'device_id':'DEVICE_ID','platform':'REAL_PLATFORM'}
    assert session.calls[0][2]['headers']['Authorization']=='OAuth OLD'
    assert session.calls[2][2]['headers']['Authorization']=='OAuth REFRESHED'


async def test_account_devices_normalization():
    session=Session([Reply({'status':'ok','devices':[
        {'id':'a','platform':'actual_platform','name':'Миди'},
        {'quasar_info':{'device_id':'b','platform':'other'},'name':'Кухня'},
        {'name':'not_a_speaker'}]})])
    auth=YandexAuth(session); auth.music_token='M'
    devices=await auth.devices()
    assert [d['id'] for d in devices]==['a','b']
    assert devices[0]['platform']=='actual_platform'
    assert all(d['host']=='' for d in devices)


async def test_import_failure_rolls_back_tokens():
    session=Session([Reply({},403)])
    auth=YandexAuth(session); auth.music_token='PREVIOUS'
    with pytest.raises(BridgeError): await auth.import_token('REJECTED','music')
    assert auth.music_token=='PREVIOUS'


async def test_qr_session_need_not_return_json():
    session=Session([Reply({'state':'otp_auth_finished','trackId':'T'}),Reply('<html>OK</html>'),
                     Reply({'access_token':'FROM_QR'})])
    auth=YandexAuth(session)
    async def check(token,kind):
        assert token=='FROM_QR' and kind=='x'
    auth.import_token=check
    assert await auth.poll_qr() is True


async def test_qr_pending_does_not_request_token():
    session=Session([Reply({'state':'pending'})]); auth=YandexAuth(session)
    assert await auth.poll_qr() is False
    assert len(session.calls)==1


async def test_auth_http_errors_never_echo_tokens():
    session=Session([Reply({'error':'SECRET_TOKEN'},401)])
    auth=YandexAuth(session)
    with pytest.raises(BridgeError) as caught:
        await auth.request('GET','https://example.test')
    assert 'SECRET_TOKEN' not in str(caught.value)
