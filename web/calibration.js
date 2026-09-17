'use strict';
const el=id=>document.getElementById(id), fragment=new URLSearchParams(location.hash.slice(1));
if(fragment.get('key')){sessionStorage.setItem('station-bridge-key',fragment.get('key'));history.replaceState(null,'',location.pathname);}
const bridgeKey=sessionStorage.getItem('station-bridge-key')||'';
for(const [id,path] of [['home','/'],['player','/local-player.html']])el(id).href=path+'#key='+encodeURIComponent(bridgeKey);
let runId=0,active=false,ctx=null,micStream=null,worklet=null,worker=null,sourceNode=null;
let chunks=[],nodes=[],lease='',heartbeat=0,baseline=null,current=null,exportData=null,starting=false,micSignature='',captureId='';
const pending=new Map();let sequence=0;
const setStatus=text=>{el('status').textContent=text;};
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function api(command,values={}){
 const r=await fetch('/api/action',{signal:AbortSignal.timeout(10000),method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Bridge-Key':bridgeKey},body:JSON.stringify({command,...values})});const d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка приложения');return d;
}
async function state(){const r=await fetch('/api/state',{signal:AbortSignal.timeout(10000),headers:{'X-Bridge-Key':bridgeKey},cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Откройте страницу из основной панели.');return d;}
function buttons(on){active=on;el('fromMeasure')?.setAttribute('aria-disabled',on);for(const id of ['permit','measure','tune','mic','path','apply'])el(id).disabled=on;el('cancel').disabled=!on;el('guard').disabled=on;el('save').disabled=on||!current||current.summary.valid<4||current.mode!=='station';}
async function chooseMic(){
 if(starting||active)return;starting=true;el('permit').disabled=true;
 let media=null;const generation=runId;
 try{media=await navigator.mediaDevices.getUserMedia({audio:true,video:false});
   if(generation!==runId)return;
   const devices=await navigator.mediaDevices.enumerateDevices();const previous=el('mic').value;
   el('mic').replaceChildren(new Option('Микрофон по умолчанию',''));
   for(const d of devices)if(d.kind==='audioinput')el('mic').append(new Option(d.label||'Микрофон',d.deviceId));
   if([...el('mic').options].some(o=>o.value===previous))el('mic').value=previous;
   setStatus('Выберите физический микрофон. Доступ сейчас закрыт; он откроется снова только на время замера.');
 }catch(e){setStatus('Нет доступа к микрофону: '+e.message);}
 finally{media?.getTracks().forEach(t=>t.stop());starting=false;el('permit').disabled=false;}
}
async function cleanup(){
 clearInterval(heartbeat);heartbeat=0;
 for(const n of nodes){try{n.stop();n.disconnect();}catch{}}nodes=[];
 micStream?.getTracks().forEach(t=>t.stop());micStream=null;
 try{worklet?.port.postMessage('stop');worklet?.disconnect();sourceNode?.disconnect();}catch{}
 worklet=null;sourceNode=null;
 const old=ctx;ctx=null;if(old)await old.close().catch(()=>{});
 worker?.terminate();worker=null;for(const waiter of pending.values())waiter.reject(Error('Измерение остановлено'));pending.clear();chunks=[];
 const oldLease=lease;lease='';if(oldLease)await api('calibration_lease',{release:true,lease:oldLease}).catch(()=>{});
 el('micLevel').value=0;
}
async function stop(){++runId;await cleanup();buttons(false);setStatus('Тест остановлен. Микрофон выключен.');}
function readWindow(frame,length){
 const raw=new Float32Array(length);let covered=0;
 for(const c of chunks){const from=Math.max(frame,c.frame),to=Math.min(frame+length,c.frame+c.data.length);if(to>from){raw.set(c.data.subarray(from-c.frame,to-c.frame),from-frame);covered+=to-from;}}
 return {raw,coverage:Math.min(1,covered/length)};
}
function analyze(raw,rate){
 return new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});worker.postMessage({id,raw,rate},[raw.buffer]);});
}
async function openAudio(id){
 const constraints={echoCancellation:false,noiseSuppression:false,autoGainControl:false,channelCount:1};
 if(el('mic').value)constraints.deviceId={exact:el('mic').value};
 const stream=await navigator.mediaDevices.getUserMedia({audio:constraints,video:false});
 if(id!==runId){stream.getTracks().forEach(t=>t.stop());throw Error('Остановлено');}
 micStream=stream;const settings=stream.getAudioTracks()[0].getSettings();
 const signature=JSON.stringify([settings.deviceId,settings.sampleRate,settings.echoCancellation,settings.noiseSuppression,settings.autoGainControl]);
 if(micSignature&&signature!==micSignature){baseline=null;el('reference').textContent='Микрофон или обработка изменились. Эталон сброшен.';}
 micSignature=signature;
 el('micSettings').textContent=`Микрофон активен. Эхоподавление: ${settings.echoCancellation??'не сообщено'}, шумоподавление: ${settings.noiseSuppression??'не сообщено'}, автогромкость: ${settings.autoGainControl??'не сообщено'}. Входная задержка браузера: ${settings.latency!==undefined?Math.round(settings.latency*1000)+' мс':'не сообщена'}.`;
 ctx=new AudioContext({latencyHint:'interactive'});const local=ctx;await local.resume();
 await local.audioWorklet.addModule('/calibration-worklet.js');if(id!==runId)throw Error('Остановлено');
 sourceNode=local.createMediaStreamSource(stream);worklet=new AudioWorkletNode(local,'bridge-mic',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
 worklet.port.onmessage=({data})=>{
   if(id!==runId)return;
   chunks.push(data);const oldest=data.frame-local.sampleRate*5;
   while(chunks.length&&(chunks[0].frame<oldest||chunks.length>1100))chunks.shift();
   let peak=0;for(const v of data.data)peak=Math.max(peak,Math.abs(v));el('micLevel').value=peak;
 };
 // Worklet output is all zeros. Never connect a live microphone to the speakers.
 sourceNode.connect(worklet);worklet.connect(local.destination);
 worker=new Worker('/calibration-worker.js');worker.onmessage=({data})=>{const task=pending.get(data.id);if(task){pending.delete(data.id);data.error?task.reject(Error(data.error)):task.resolve(data.result);}};
 worker.onerror=()=>{for(const w of pending.values())w.reject(Error('Ошибка анализа сигнала'));pending.clear();};
}
async function perform(id,label){
 const rows=[],rate=ctx.sampleRate;
 for(let i=0;i<6;i++){
   if(id!==runId)throw Error('Остановлено');
   const s=await state();if(id!==runId)throw Error('Остановлено');
   if(captureId && s.media?.capture_id!==captureId)throw Error('Трансляция сменилась — повторите замер.');
   setStatus(`${label}. Сигнал ${i+1} из 6. Микрофон активен, не сворачивайте вкладку.`);
   const t=ctx.currentTime+.35,frame=Math.round(t*rate),buffer=ctx.createBuffer(1,Math.round(rate*.08),rate);
   const x=BridgeDSP.chirp(rate);for(let j=0;j<x.length;j++)buffer.getChannelData(0)[j]=x[j]*.075;
   const node=ctx.createBufferSource();node.buffer=buffer;node.connect(ctx.destination);node.start(frame/rate);nodes.push(node);
   // Completion is checked against the audio clock; UI timing doesn't label acoustic arrival.
   while(ctx&&ctx.currentTime<(frame/rate)+2.72){await delay(100);if(id!==runId)throw Error('Остановлено');}
   const sample=readWindow(frame,Math.round(rate*2.65)),row=await analyze(sample.raw,rate);
   if(sample.coverage<.95){row.valid=false;row.coverage=sample.coverage;}
   rows.push(row);renderRow(label,i,row);
   try{node.disconnect();}catch{}nodes=nodes.filter(n=>n!==node);
   await delay(200);if(id!==runId)throw Error('Остановлено');
 }
 return {summary:BridgeDSP.summarize(rows),rows,label};
}
function renderRow(label,i,row){
 const div=document.createElement('div');div.className='result';div.textContent=`${label} · ${i+1}: `+(row.clipped?'перегрузка микрофона':row.ambiguous?'два сильных пути — уберите звук ПК и эхо':row.valid?`${row.delay_ms.toFixed(1)} мс · совпадение ${(row.confidence*100).toFixed(0)}%`:'сигнал не найден / неполный захват');el('rows').append(div);
}
function showResult(result,mode){
 current=result;current.mode=mode;const s=result.summary;
 el('median').textContent=s.median_ms===null?'Не измерено':`${s.median_ms.toFixed(1)} мс`;
 el('spread').textContent=`${s.spread_ms===null?'—':s.spread_ms.toFixed(1)+' мс'} / ${s.missed} из ${s.count}`;
 if(mode==='reference'&&s.valid>=4){baseline=s;el('reference').textContent=`Локальный эталон: ${s.median_ms.toFixed(1)} мс. Действует только для этой вкладки и этого микрофона.`;}
 const extra=mode==='station'&&baseline&&s.valid>=4?s.median_ms-baseline.median_ms:null;
 result.offset_ms=extra!==null&&extra>=0?extra:null;
 el('extra').textContent=extra===null?'Нужен эталон':extra<0?'Перепроверьте эталон':`${extra.toFixed(1)} мс`;
 el('save').disabled=mode!=='station'||s.valid<4;el('export').disabled=false;
 el('advice').textContent=s.valid<4?'Недостаточно достоверных сигналов. Проверьте микрофон, шум, выход Windows и близкие динамики.':s.missed?'Есть недостоверные или потерянные сигналы. Не принимайте результат за точную настройку.':'Серия завершена. Повторите в тех же условиях: единичная серия не доказывает долгую устойчивость.';
}
async function run(tuning){
 if(active||starting)return;starting=true;const id=++runId;buttons(true);current=null;exportData=null;el('export').disabled=true;el('rows').replaceChildren();el('save').disabled=true;
 let originalGuard=null;const mode=el('path').value;const startState=await state().catch(()=>null);
 try{
   if(id!==runId)throw Error('Остановлено');
   captureId=mode==='station' ? startState?.media?.capture_id || '' : '';
   if(mode==='station'&&(!startState?.media?.capture?.running||startState.media.transport!=='pcm'||startState.media.capture.source==='generated_test'))throw Error('Сначала запустите основной или экспериментальный PCM в основной панели.');
   if(mode==='reference'&&startState?.media?.kind==='live')throw Error('Для локального эталона остановите трансляцию в основной панели; затем измерьте звук проводного выхода.');
   if(tuning&&mode!=='station')throw Error('Сравнение буферов предназначено для PCM на Станцию.');
   if(!navigator.mediaDevices||!window.AudioWorkletNode)throw Error('Нужен браузер с микрофоном и AudioWorklet на localhost.');
   lease=(await api('calibration_lease')).lease;
   if(id!==runId)throw Error('Остановлено');
   heartbeat=setInterval(()=>{api('calibration_lease',{lease}).catch(()=>stop());},8000);
   await openAudio(id);if(id!==runId)throw Error('Остановлено');await delay(700);
   if(tuning){
     originalGuard=startState.media.micro_buffer_ms||0;
     const rounds=[];
     const guards=startState.media.capture.actual_block_ms>=5?[0,10,20]:[0,5,10,20];
     for(const guard of guards){
       if(id!==runId)throw Error('Остановлено');await api('micro_buffer',{value:guard,capture_id:captureId});await delay(1500);
       const r=await perform(id,`Запас ${guard} мс`);r.guard_ms=guard;rounds.push(r);showResult(r,mode);
     }
     const usable=rounds.filter(r=>r.summary.valid>=5&&r.summary.missed===0&&r.summary.spread_ms<=30);
     usable.sort((a,b)=>Math.abs(a.summary.median_ms-b.summary.median_ms)>5 ? a.summary.median_ms-b.summary.median_ms : a.guard_ms-b.guard_ms);
     const best=usable[0];
     if(best){el('guard').value=String(best.guard_ms);showResult(best,mode);el('advice').textContent=`Лучший достоверный результат этой короткой серии: запас ${best.guard_ms} мс. Прежний запас восстановлен. Примените рекомендацию кнопкой и проверьте длительную работу. Это не универсальная оптимизация.`;}
     else el('advice').textContent='Надёжная рекомендация не получена. Прежний запас восстанавливается; проверьте акустику и повторите.';
     exportData={type:'micro_buffer_comparison',rounds,baseline,note:'Local acoustic estimate; microphone and room path included; no audio retained.'};
   }else{const r=await perform(id,mode==='station'?'Станция':'Эталон');showResult(r,mode);exportData={type:mode,result:r,baseline,note:'Local acoustic estimate; not an exact hardware-latency measurement.'};}
   setStatus('Замер завершён. Микрофон выключен.');
 }catch(e){if(id===runId)setStatus('Измерение: '+e.message);}
 finally{
   if(originalGuard!==null){await api('micro_buffer',{value:originalGuard,capture_id:captureId}).catch(()=>{});}
   await cleanup();buttons(false);starting=false;el('micSettings').textContent='Микрофон выключен. В памяти остались только числовые результаты.';
 }
}
el('permit').onclick=chooseMic;el('measure').onclick=()=>run(false);el('tune').onclick=()=>run(true);el('cancel').onclick=stop;
el('apply').onclick=()=>api('micro_buffer',{value:Number(el('guard').value),...(captureId?{capture_id:captureId}:{})}).then(()=>setStatus('Запас применён к текущему PCM. Микрофон не включался.')).catch(e=>setStatus(e.message));
el('save').onclick=async()=>{if(!current)return;try{await api('calibration_save',{result:{...current.summary,offset_ms:current.offset_ms}});setStatus('Сохранены только числовые показатели. Аудиозаписи нет.');}catch(e){setStatus(e.message);}};
el('export').onclick=()=>{if(!exportData)return;const blob=new Blob([JSON.stringify(exportData,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='StationBridge-acoustic-test.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
document.addEventListener('visibilitychange',()=>{if(document.hidden&&(active||starting))stop();});window.addEventListener('pagehide',()=>{++runId;micStream?.getTracks().forEach(t=>t.stop());cleanup();});
if(!bridgeKey)setStatus('Откройте эту страницу кнопкой из основной панели Station Bridge.');
