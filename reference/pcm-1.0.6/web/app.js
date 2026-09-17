'use strict';
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.get('key')) {
  sessionStorage.setItem('station-bridge-key', fragment.get('key'));
  history.replaceState(null, '', location.pathname);
}
const key = sessionStorage.getItem('station-bridge-key') || '';
let state = null, busy = false, pollBusy = false, lastDevices = '', lastOutputs = '', lastLogs = '';
let lastAdapters = '', networkInitialized = false, lastProbe = ''; 
let qrURL = '', noticeTimer = null, selectedName = 'Яндекс Станция', pollTimer = null;
const write = (id, value) => { $(id).textContent = value; };
const show = (id, yes) => $(id).classList.toggle('hidden', !yes);

function notify(message, error = false, persistent = false) {
  clearTimeout(noticeTimer);
  write('notice', message);
  $('notice').classList.toggle('error', error);
  show('notice', true);
  if (!persistent) noticeTimer = setTimeout(() => show('notice', false), error ? 18000 : 8000);
}
async function request(path, opts = {}) {
  const response = await fetch(path, { ...opts, headers: { 'X-Bridge-Key': key, ...opts.headers }, cache: 'no-store' });
  let result;
  try { result = await response.json(); } catch { throw new Error('Приложение не вернуло JSON. Проверьте окно Start.cmd.'); }
  if (!response.ok) throw new Error(result.error || `Ошибка HTTP ${response.status}`);
  return result;
}
function updateDisabled() {
  const connected = !!state?.station?.connected;
  const loggedIn = !!state?.auth?.logged_in;
  const active = busy || !!state?.busy;
  ['login','scan','connect','disconnect','tokenLogin','logout','refreshOutputs','trustAccept','say','test','live','refreshNetwork','peerApprove','peerRevoke','probeAudio','hlsTest','resync','streamTest'].forEach(id => $(id).disabled = active);
  ['test','live','say','hlsTest','streamTest','resync'].forEach(id => $(id).disabled ||= !connected);
  $('live').disabled ||= !state?.windows || state?.media?.kind === 'live';
  $('probeAudio').disabled ||= !state?.windows || state?.media?.kind === 'live';
  $('refreshOutputs').disabled ||= state?.media?.kind === 'live';
  $('hlsTest').disabled ||= state?.media?.kind === 'live';
  $('streamTest').disabled ||= state?.media?.kind === 'live';
  $('resync').disabled ||= state?.media?.kind !== 'live';
  $('transport').disabled = active || state?.media?.kind === 'live';
  $('latencyProfile').disabled = active || state?.media?.kind === 'live' || $('transport').value === 'hls';
  $('segment').disabled = active || state?.media?.kind === 'live';
  document.querySelectorAll('.probe-choice').forEach(button => button.disabled = active || state?.media?.kind === 'live');
  $('connect').disabled ||= !loggedIn;
  $('peerApprove').disabled ||= !connected || !state?.peer_approval;
  $('peerRevoke').disabled ||= !connected || !state?.media?.approved_audio_peer;
  $('test').disabled ||= state?.media?.kind === 'test' && !state?.media?.requests && state?.media?.elapsed < 15;
  $('disconnect').disabled ||= !connected;
  $('volume').disabled = !connected || active;
  $('file').disabled = !connected || active;
  $('file').parentElement.classList.toggle('disabled', !connected || active);
  // Stop remains available; the server serializes it after any in-flight start.
  $('stop').disabled = !connected && !state?.media?.active;
  $('outputs').disabled = active || state?.media?.kind === 'live';
  $('audioHost').disabled = active;
  $('manualAudioHost').disabled = active;
}
async function command(name, values = {}, message = '') {
  busy = true; updateDisabled();
  if (message) notify(message, false, true);
  try {
    const result = await request('/api/action', { method: 'POST', headers: { 'Content-Type':'application/json' }, body: JSON.stringify({ command:name, ...values }) });
    if (message) show('notice', false);
    await poll();
    return result;
  } catch (err) { notify(err.message, true); throw err; }
  finally { busy = false; updateDisabled(); }
}
function deviceValues() {
  return { name:selectedName, id:$('deviceId').value.trim(), platform:$('platform').value.trim(), host:$('host').value.trim(), port:Number($('port').value) };
}
function fillDevice(device) {
  if (!device) return;
  selectedName = device.name;
  $('deviceId').value = device.id || ''; $('platform').value = device.platform || '';
  $('host').value = device.host || ''; $('port').value = device.port || 1961;
  write('deviceHint', device.host ? `${device.name} · ${device.host}:${device.port}` : 'Колонка найдена без локального адреса. Укажите IP ниже.');
  if (!device.host) $('manual').open = true;
}
function renderDevices(devices) {
  const serialized = JSON.stringify(devices);
  if (serialized === lastDevices) return;
  lastDevices = serialized;
  const previousId = $('deviceId').value, previousIP = $('host').value;
  $('devices').replaceChildren();
  const placeholder = new Option(devices.length ? 'Выберите Станцию' : 'Колонки пока не найдены', '');
  $('devices').append(placeholder);
  let selected = -1;
  devices.forEach((device, index) => {
    $('devices').append(new Option(`${device.name} — ${device.host || 'укажите IP'}`, String(index)));
    if (device.id === previousId && (!previousIP || device.host === previousIP)) selected = index;
  });
  if (selected < 0 && devices.length === 1) selected = 0;
  if (selected >= 0) { $('devices').value = String(selected); fillDevice(devices[selected]); }
}
function renderOutputs(outputs) {
  const serialized = JSON.stringify(outputs);
  if (serialized === lastOutputs) return;
  lastOutputs = serialized;
  const previousKey = $('outputs').selectedOptions[0]?.dataset.key || '';
  $('outputs').replaceChildren(new Option('Выход Windows по умолчанию', ''));
  outputs.forEach(d => {
    const option = new Option(`${d.name}${d.default ? ' · основной' : ''}`, String(d.index));
    option.dataset.key = d.key || '';
    $('outputs').append(option);
  });
  if (previousKey) {
    const matching = [...$('outputs').options].find(o => o.dataset.key === previousKey);
    if (matching) $('outputs').value = matching.value;
    // A disappeared endpoint must not silently fall back to a different index.
    else notify('Ранее выбранный выход исчез. Выберите источник заново перед трансляцией.', true);
  }
}
function renderProbe(probe) {
  const serialized = JSON.stringify(probe);
  if (serialized === lastProbe) return;
  lastProbe = serialized;
  show('probePanel', !!probe?.results?.length);
  $('probeResults').replaceChildren();
  if (!probe?.results?.length) return;
  write('probeSummary', probe.recommended !== null ? 'Найден сигнал. Проверьте выбранный выход ниже.' : 'Сигнал не найден. Включите музыку и проверьте выход приложения в микшере Windows.');
  probe.results.forEach(row => {
    const item = document.createElement('div'); item.className = 'probe-row';
    const text = document.createElement('div');
    const name = document.createElement('strong'); name.textContent = row.name;
    const detail = document.createElement('span');
    detail.textContent = row.error || (row.signal_detected ? `Есть сигнал · пик ${(row.peak_since_start*100).toFixed(1)}% · ${row.non_silent_seconds.toFixed(1)} с звука`
      : row.callback_count ? 'Кадры есть, значимого сигнала нет' : 'Аудиокадры не поступают');
    text.append(name, detail);
    const button = document.createElement('button'); button.className = 'ghost small probe-choice'; button.textContent = 'Выбрать';
    button.addEventListener('click', () => {
      const option = [...$('outputs').options].find(o => o.dataset.key === row.key);
      if (!option) { notify('Список изменился. Повторите поиск выхода.', true); return; }
      $('outputs').value = option.value;
      notify(`Выбран: ${row.name}. Теперь нажмите «Транслировать звук ПК».`);
    });
    item.append(text, button); $('probeResults').append(item);
  });
}
function audioHostValue() {
  return $('audioHost').value === 'manual' ? $('manualAudioHost').value.trim() : $('audioHost').value;
}
function renderNetwork(network) {
  const adapters = network.adapters || [];
  const serialized = JSON.stringify(adapters);
  if (serialized !== lastAdapters) {
    const previous = $('audioHost').value;
    lastAdapters = serialized;
    $('audioHost').replaceChildren(new Option('Автоматически · адрес в сети Станции', ''));
    adapters.forEach(a => $('audioHost').append(new Option(`${a.address}${a.prefix !== null ? '/' + a.prefix : ''} — ${a.name}${a.virtual_hint ? ' · VPN/виртуальный?' : ''}`, a.address)));
    $('audioHost').append(new Option('Ввести IP компьютера вручную…', 'manual'));
    if ([...$('audioHost').options].some(o => o.value === previous)) $('audioHost').value = previous;
  }
  if (!networkInitialized) {
    networkInitialized = true;
    const saved = network.override || '';
    if ([...$('audioHost').options].some(o => o.value === saved)) $('audioHost').value = saved;
    else { $('audioHost').value = 'manual'; $('manualAudioHost').value = saved; }
  }
  show('manualAudioHostLabel', $('audioHost').value === 'manual');
  let hint = 'Выберите адрес Wi-Fi/Ethernet самого компьютера, не IP Станции. Затем нажмите «Подключиться».';
  if (network.audio_host) hint = `Для аудио: ${network.audio_host} · ${network.interface || 'интерфейс'} · ${network.mode === 'manual' ? 'указан вручную' : 'автовыбор'}. Изменения применяются кнопкой «Подключиться».`;
  write('networkHint', hint);
  write('networkStatus', (network.warnings || []).join(' '));
}
function render(next) {
  const initial = !state;
  state = next;
  const station = state.station || {}, media = state.media || {}, account = state.auth || {};
  const capture = media.capture;
  if (initial) {
    $('remember').checked = account.remember;
    if (state.selected?.id) fillDevice(state.selected);
  }
  write('version', state.version);
  show('notWindows', !state.windows);
  write('accountName', account.logged_in ? account.name || 'Вход выполнен' : 'Вход не выполнен');
  show('loginControls', !account.logged_in); show('logout', account.logged_in);
  if (account.qr_status === 'done') show('qrPanel', false);
  if (account.qr_status === 'expired') write('qrState', 'Код истёк. Нажмите «Войти по QR-коду» ещё раз.');
  if (account.qr_status === 'error') write('qrState', 'Вход не завершён. Подробности — в журнале.');
  const connected = !!station.connected;
  write('connectionBadge', connected ? 'Glagol подключён' : 'Не подключено');
  $('connectionBadge').classList.toggle('on', connected);
  renderDevices(state.devices || []); renderOutputs(state.outputs || []); renderProbe(state.audio_probe || {}); renderNetwork(state.network || {});
  write('sConnection', connected ? 'Подключён' : 'Не подключён');
  write('sServer', connected && media.host ? `${media.host}:${media.port}` : 'Не запущен');
  write('sRequests', media.requests ?? 0); write('sSegments', media.segment_requests ?? 0);
  write('nowPlaying', station.playing ? `${station.title || 'Станция сообщает: играет'}${station.subtitle ? ' · ' + station.subtitle : ''}` : connected ? 'Станция подключена. Выберите источник звука.' : 'Ожидаем подключения.');
  let mediaText = 'Подтверждение команды не равно слышимому звуку. Сначала проверьте тестовый сигнал.';
  if (media.active) {
    mediaText = `${media.kind === 'live' ? 'Трансляция ПК' : 'Локальный файл'} · ${media.requests || 0} запросов от колонки`;
    mediaText += ` · всего обращений: ${media.incoming_requests || 0} · HEAD: ${media.head_requests || 0} · отклонено: ${media.rejected_requests || 0}`;
    if (capture) mediaText += ` · ${capture.stream_seconds} с потока · ${capture.captured_seconds} с кадров Windows · ${capture.inserted_silence_seconds} с добавленной тишины · пропусков: ${capture.dropped_blocks}`;
    if (media.last_request_ago !== null) mediaText += ` · последний запрос ${media.last_request_ago} с назад`;
    else mediaText += ' · ожидаем подключения Станции к аудиосерверу';
  }
  write('mediaStatus', mediaText);
  let timing = 'Измерения появятся после запуска. Это не задержка от компьютера до динамика Станции.';
  if (capture) {
    timing = `${{mp3:'Прямой MP3',pcm:'WAV/PCM',hls:'HLS'}[media.transport] || media.transport} · очередь захвата: ${capture.pcm_queue_blocks ?? '—'} блок(а/ов)`;
    if (media.encoder_ready_seconds != null) timing += ` · подготовка потока: ${media.encoder_ready_seconds.toFixed(2)} с`;
    if (capture.encoder_backlog_seconds != null) timing += ` · остаток в кодировщике: ${Math.round(capture.encoder_backlog_seconds * 1000)} мс`;
    if (['mp3','pcm'].includes(media.transport)) {
      timing += ` · передано: ${((media.live_bytes_sent || 0) / 1048576).toFixed(2)} МБ · соединений: ${media.live_connections || 0}`;
      if (media.live_send_lag_ms != null) timing += ` · до свежих блоков: ${media.live_send_lag_ms} мс`;
      timing += ` · отброшено устаревших блоков: ${media.live_skipped_frames || 0}`;
    } else if (media.hls_request_lag_seconds != null) timing += ` · отставание запрошенного HLS-сегмента: ≈${media.hls_request_lag_seconds.toFixed(1)} с`;
    if (capture.encoder_bypassed) timing += ' · кодировщик полностью обходится';
    if (capture.latency_profile) timing += ` · профиль: ${capture.latency_profile === 'fast' ? 'минимум очередей' : 'устойчивый'} · блок: ${capture.requested_block_ms} мс`;
    if (media.live_write_queue_bytes != null) timing += ` · очередь HTTP: ${media.live_write_queue_bytes} байт`;
    timing += ' · Буфер и слышимая задержка самой Станции сюда НЕ входят.';
  }
  write('latencyText', timing);
  const approval = state.peer_approval;
  show('peerPanel', connected && !!approval);
  if (approval) {
    write('peerDescription', `Станция подключена как ${state.selected?.host || 'выбранное устройство'}, ` +
      `но действующий аудиоадрес запросил клиент ${approval.ip}. Так бывает при NAT или сетевом посреднике. ` +
      `Подтверждайте только ожидаемый адрес своего роутера или посредника в доверенной сети.`);
    write('peerApprove', `Разрешить ${approval.ip} и повторить`);
  }
  show('peerAllowed', connected && !!media.approved_audio_peer);
  write('peerAllowedText', media.approved_audio_peer ? `Разрешён аудиопосредник: ${media.approved_audio_peer}` : '');
  const measuredLevel = capture?.source === 'generated_test' ? capture?.level : capture?.input_level;
  $('level').value = measuredLevel || 0;
  write('levelText', capture ? `${((measuredLevel || 0) * 100).toFixed(1)}%` : '—');
  write('captureDevice', capture?.source === 'generated_test' ? 'Источник: генератор тестового тона, без захвата Windows.'
    : capture?.device?.name ? `Захватывается: ${capture.device.name} · ${capture.device.rate} Гц · каналов: ${capture.device.channels}`
    : 'Выбранный источник будет проверен при запуске.');
  show('captureWarning', !!capture?.warning);
  write('captureWarning', capture?.warning || '');
  document.body.classList.toggle('streaming', media.kind === 'live' && capture?.running);
  if (typeof station.volume === 'number' && document.activeElement !== $('volume')) {
    $('volume').value = Math.round(station.volume * 100);
    write('volumeText', Math.round(station.volume * 100) + '%');
  }
  if (state.trust) {
    show('trustPanel', true);
    write('trustTitle', state.trust.changed ? 'Сертификат изменился — проверьте колонку' : 'Первое подключение: проверьте IP');
    write('fingerprint', `${state.trust.device.host}:${state.trust.device.port}\nSHA-256: ${state.trust.fingerprint.match(/.{1,2}/g).join(':')}`);
  } else show('trustPanel', false);
  const logJSON = JSON.stringify(state.logs);
  if (lastLogs !== logJSON) {
    lastLogs = logJSON;
    const atBottom = $('logs').scrollHeight - $('logs').scrollTop - $('logs').clientHeight < 60;
    $('logs').replaceChildren();
    (state.logs || []).forEach(row => {
      const line = document.createElement('div'); line.className = 'log-row';
      const t = document.createElement('span'); t.className = 'log-time'; t.textContent = row.time;
      const m = document.createElement('span'); m.className = 'log-message'; m.textContent = row.text;
      line.append(t,m); $('logs').append(line);
    });
    if (atBottom || initial) $('logs').scrollTop = $('logs').scrollHeight;
  }
  updateDisabled();
}
async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try { render(await request('/api/state')); }
  catch (err) { if (!state) notify(err.message, true, true); else { write('connectionBadge', 'Панель не отвечает'); } }
  finally { pollBusy = false; }
}
// Handler wrappers absorb errors after displaying a safe message.
function click(id, callback) { $(id).addEventListener('click', () => Promise.resolve().then(callback).catch(() => {})); }
click('login', async () => {
  const result = await command('qr', {remember:$('remember').checked}, 'Создаём QR-код на стороне Яндекса…');
  if (qrURL) URL.revokeObjectURL(qrURL);
  qrURL = URL.createObjectURL(new Blob([result.svg], {type:'image/svg+xml'}));
  $('qrImage').src = qrURL; write('qrState', 'Ожидаем подтверждения…'); show('qrPanel', true);
});
click('tokenLogin', async () => { const token = $('token').value; $('token').value=''; await command('token', {token, kind:$('tokenKind').value, remember:$('remember').checked}, 'Проверяем токен…'); });
click('logout', () => command('logout'));
click('scan', () => command('scan', {}, 'Ищем колонки в локальной сети…'));
$('devices').addEventListener('change', () => { const index=$('devices').value; if(index !== '') fillDevice(state.devices[Number(index)]); });
click('connect', () => command('connect', {device:deviceValues(), audio_host:audioHostValue()}, 'Проверяем подключение и сертификат Станции…'));
click('trustAccept', () => command('connect', {device:state.trust.device, fingerprint:state.trust.fingerprint, audio_host:state.trust.audio_host ?? audioHostValue()}, 'Подключаем Glagol…'));
click('trustCancel', async () => { await command('cancel_trust'); notify('Сертификат не подтверждён. Соединение не установлено.'); });
click('disconnect', () => command('disconnect'));
click('refreshOutputs', () => command('outputs'));
click('probeAudio', async () => {
  const result = await command('probe_audio', {}, 'Ищем сигнал на выходах Windows. Музыка на ПК должна продолжать играть…');
  renderOutputs(result.outputs || []);
  renderProbe(result);
  if (result.recommended_key) {
    const option = [...$('outputs').options].find(o => o.dataset.key === result.recommended_key);
    if (option) {
      $('outputs').value = option.value;
      notify(`Выбран выход с сигналом: ${option.text}. Нажмите «Транслировать звук ПК».`);
    }
  } else notify('На проверенных выходах нет сигнала. Проверьте Mute и выход браузера/плеера в микшере Windows.', true, true);
});
click('streamTest', () => command('stream_test', {transport:$('transport').value, latency_profile:$('latencyProfile').value, segment_time:Number($('segment').value)}, 'Готовим тест выбранного режима без захвата Windows…'));
click('resync', () => command('resync', {}, 'Отменяем старый поток и возвращаемся к живому звуку…'));
click('hlsTest', () => command('hls_test', {segment_time:Number($('segment').value)}, 'Готовим тест HLS, без захвата Windows…'));
click('refreshNetwork', () => command('network'));
$('audioHost').addEventListener('change', () => show('manualAudioHostLabel', $('audioHost').value === 'manual'));
click('test', () => command('test', {}, 'Отправляем тестовый сигнал…'));
click('live', () => command('live', {transport:$('transport').value, latency_profile:$('latencyProfile').value, device_index:$('outputs').value, device_key:$('outputs').selectedOptions[0]?.dataset.key || '', segment_time:Number($('segment').value)}, 'Захват включается. Готовим буфер трансляции…'));
click('stop', () => command('stop'));
click('peerApprove', () => {
  const candidate = state?.peer_approval;
  if (!candidate) throw new Error('Запрос устарел. Запустите проверку звука снова.');
  return command('approve_audio_peer', {ip:candidate.ip, challenge:candidate.challenge}, 'Разрешаем один адрес и повторяем воспроизведение…');
});
click('peerRevoke', () => command('revoke_audio_peer'));
click('say', () => command('say', {text:$('speech').value}));
$('volume').addEventListener('input', () => write('volumeText', $('volume').value + '%'));
$('volume').addEventListener('change', () => command('volume', {value:Number($('volume').value)/100}).catch(() => {}));
$('file').addEventListener('change', async () => {
  const file = $('file').files[0]; if(!file) return;
  if (file.size > 256*1024*1024) { notify('Максимальный размер файла — 256 МБ.', true); $('file').value=''; return; }
  busy = true; updateDisabled(); write('fileName', file.name); notify('Подготавливаем файл и отправляем ссылку Станции…', false, true);
  try { const form = new FormData(); form.append('file', file); await request('/api/upload', {method:'POST', body:form}); show('notice',false); await poll(); }
  catch (err) { notify(err.message,true); }
  finally { $('file').value=''; busy=false; updateDisabled(); }
});
click('diagnostic', async () => {
  const report = await request('/api/diagnostic');
  const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)], {type:'application/json'}));
  const a=document.createElement('a'); a.href=url; a.download='StationBridge-diagnostic.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(url),2000);
});
click('exit', async () => { await command('shutdown'); clearInterval(pollTimer); notify('Приложение завершено. Эту вкладку можно закрыть.',false,true); });
function updateTransportHint() {
  const mode = $('transport').value;
  const hints = {
    pcm: 'WAV/PCM: без FFmpeg, сжатия и пересчёта частоты. Моно/стерео, 16 бит, частота выбранного выхода. При 48 кГц стерео — 1,536 Мбит/с. Длительный WAV-поток на вашей прошивке ещё не проверен; при тишине вернитесь к MP3. Это не отключает внутренний буфер Станции.',
    mp3: 'Прямой MP3: рабочий вариант, 320 кбит/с, 48 кГц. Профиль «Минимум очередей» уменьшает запас буфера на ПК; при щелчках вернитесь к устойчивому.',
    hls: 'HLS: резервный совместимый способ, без изменений формата. 1-секундные сегменты; 2 секунды — резерв для нестабильной сети.'
  };
  write('transportHint', hints[mode]);
  $('hlsBuffer').classList.toggle('hidden', mode !== 'hls');
  updateDisabled();
}
try {
  const remembered = localStorage.getItem('station-bridge-transport');
  if (['mp3','hls','pcm'].includes(remembered)) $('transport').value = remembered;
} catch (_) { /* Storage may be restricted by browser policy. */ }
$('transport').addEventListener('change', () => {
  try { localStorage.setItem('station-bridge-transport', $('transport').value); } catch (_) {}
  updateTransportHint();
});
try {
  const profile = localStorage.getItem('station-bridge-latency-profile');
  if (['fast','balanced'].includes(profile)) $('latencyProfile').value = profile;
} catch (_) {}
$('latencyProfile').addEventListener('change', () => {
  try { localStorage.setItem('station-bridge-latency-profile', $('latencyProfile').value); } catch (_) {}
});
updateTransportHint();
if (!key) notify('Откройте панель по ссылке из окна Start.cmd: в этой вкладке нет ключа управления.', true, true);
else { poll(); pollTimer = setInterval(poll, 1500); }
