'use strict';
const el=id=>document.getElementById(id),hash=new URLSearchParams(location.hash.slice(1));
if(hash.get('key')){sessionStorage.setItem('station-bridge-key',hash.get('key'));history.replaceState(null,'',location.pathname);}
const key=sessionStorage.getItem('station-bridge-key')||'';
for(const [id,path] of [['home','/'],['calibration','/calibration.html']])el(id).href=path+'#key='+encodeURIComponent(key);
const audio=el('audioLead'),video=el('picture');video.muted=true;audio.volume=.5;
let blobURL='',generation=0,loaded=false,active=false,starting=false,base=0,offset=.35,lease='',timer=0,raf=0,aligning=false,wallStart=0;
const status=text=>{el('status').textContent=text;};
async function api(command,values={}){const r=await fetch('/api/action',{signal:AbortSignal.timeout(10000),method:'POST',headers:{'Content-Type':'application/json','X-Bridge-Key':key},body:JSON.stringify({command,...values}),cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Нет соединения с программой.');return d;}
async function state(){const r=await fetch('/api/state',{headers:{'X-Bridge-Key':key},cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Откройте страницу из основной панели.');return d;}
function controls(){el('play').disabled=!loaded||active||starting;el('pause').disabled=!active&&!starting;el('rewind').disabled=!loaded;el('position').disabled=!loaded||starting;el('offset').disabled=active||starting;el('file').disabled=starting;el('fromMeasure').disabled=active||starting;}
async function pause(message='Пауза. Уже переданный звук может ещё доигрывать в Станции.'){
 ++generation;active=false;starting=false;cancelAnimationFrame(raf);clearInterval(timer);timer=0;audio.pause();video.pause();video.playbackRate=1;aligning=false;
 const old=lease;lease='';if(old)await api('calibration_lease',{lease:old,release:true}).catch(()=>{});controls();status(message);
}
function metadata(target){return new Promise((resolve,reject)=>{
 if(target.readyState>=1){resolve();return;}
 let timer;const done=()=>{clearTimeout(timer);target.removeEventListener('loadedmetadata',ok);target.removeEventListener('error',bad);};
 const ok=()=>{done();resolve();},bad=()=>{done();reject(Error('Браузер не смог открыть кодек этого файла. Попробуйте MP4/H.264 с AAC или WebM.'));};
 target.addEventListener('loadedmetadata',ok);target.addEventListener('error',bad);timer=setTimeout(()=>{done();reject(Error('Не получены данные видео. Выберите другой файл.'));},15000);
});}
el('file').addEventListener('change',async()=>{
 await pause('Открываем локальный файл…');const id=generation;loaded=false;controls();const f=el('file').files[0];if(!f)return;
 if(blobURL)URL.revokeObjectURL(blobURL);blobURL=URL.createObjectURL(f);audio.src=blobURL;video.src=blobURL;audio.load();video.load();
 try{await Promise.all([metadata(audio),metadata(video)]);if(id!==generation)return;
 if(!Number.isFinite(audio.duration)||audio.duration<=0)throw Error('Нужен конечный локальный видеофайл.');
 loaded=true;el('position').max=String(audio.duration);el('position').value='0';status('Файл готов. PCM-трансляция уже должна быть включена в основной панели.');controls();
 }catch(e){if(id===generation)status(e.message);}
});
async function play(){
 if(!loaded||starting||active)return;const ms=Number(el('offset').value);if(!Number.isFinite(ms)||ms<0||ms>2500){status('Компенсация: от 0 до 2500 мс.');return;}
 starting=true;controls();const id=++generation;
 try{const s=await state();if(id!==generation)return;if(s.media?.kind!=='live'||s.media?.transport!=='pcm'||!s.media?.capture?.running||s.media.capture.source==='generated_test')throw Error('Запустите обычную PCM-трансляцию звука Windows, не тестовый тон.');
 const grant=await api('calibration_lease',{purpose:'local_video'});if(id!==generation){await api('calibration_lease',{release:true,lease:grant.lease});return;}
 lease=grant.lease;offset=ms/1000;base=Math.max(0,Math.min(Number(el('position').value),audio.duration-.02));
 audio.currentTime=base;video.currentTime=base;video.playbackRate=1;
 await audio.play();if(id!==generation){audio.pause();return;}
 wallStart=performance.now();active=true;starting=false;controls();status(`Аудио идёт первым; картинка начинается на ${ms} мс позже. Автосброс временно приостановлен.`);
 timer=setInterval(()=>{api('calibration_lease',{lease}).catch(e=>pause(e.message));},8000);tick();
 }catch(e){if(id===generation)await pause(e.message);}
 finally{if(id===generation){starting=false;controls();}}
}
function tick(){
 if(!active)return;
 const target=Math.max(base,audio.currentTime-offset),ready=audio.currentTime>=base+offset||(audio.ended&&performance.now()-wallStart>=offset*1000);
 if(ready&&video.paused&&!video.ended&&!aligning&&target<video.duration-.005){aligning=true;video.currentTime=target;video.play().catch(e=>pause('Не удалось запустить картинку: '+e.message)).finally(()=>{aligning=false;});}
 if(ready&&!video.paused&&!audio.ended){const drift=target-video.currentTime;
 if(Math.abs(drift)>.2)video.currentTime=target;
 else video.playbackRate=Math.abs(drift)>.03?Math.max(.97,Math.min(1.03,1+drift*.2)):1;
 el('drift').textContent=`Сдвиг двух локальных дорожек: ${((audio.currentTime-video.currentTime)*1000).toFixed(0)} мс. Это не замер динамика.`;
 }
 const t=video.currentTime;el('position').value=String(t);el('positionText').textContent=`${t.toFixed(1)} / ${audio.duration.toFixed(1)} с`;
 if(audio.ended&&video.currentTime>=video.duration-.06){pause('Воспроизведение завершено.');return;}
 // Let the muted video finish its last offset milliseconds after audio ended.
 if(audio.ended){video.playbackRate=1;el('drift').textContent='Завершаем последний фрагмент картинки.';}
 raf=requestAnimationFrame(tick);
}
el('play').onclick=play;el('pause').onclick=()=>pause();
el('rewind').onclick=async()=>{await pause('Позиция сброшена. Нажмите «Воспроизводить».');audio.currentTime=video.currentTime=0;el('position').value='0';};
el('position').addEventListener('change',async()=>{const value=Number(el('position').value);await pause('Новая позиция выбрана. Нажмите «Воспроизводить».');audio.currentTime=video.currentTime=value;});
el('volume').oninput=()=>{audio.volume=Number(el('volume').value);};
el('fromMeasure').onclick=async()=>{try{const s=await state(),m=s.calibration_result;if(!m)throw Error('Сначала сохраните результат микрофонного теста.');const value=m.offset_ms??m.median_ms;el('offset').value=String(Math.round(Math.min(2500,value)));el('measureNote').textContent=m.offset_ms!==null?'Взята добавочная задержка к локальному эталону. Уточните синхронизацию на слух и по картинке.':'Взят полный акустический замер; он включает микрофон и может завышать нужную компенсацию. Уточните вручную.';}catch(e){status(e.message);}};
for(const target of [audio,video])target.addEventListener('error',()=>{if(active||starting)pause('Ошибка декодирования локального файла. Выберите совместимый формат.');});
// Video buffering pauses both tracks, rather than silently losing A/V sync.
video.addEventListener('waiting',()=>{if(active&&!aligning&&audio.currentTime>base+offset+.5)pause('Видео буферизуется. Нажмите «Воспроизводить», когда файл готов.');});
window.addEventListener('pagehide',()=>{pause();if(blobURL)URL.revokeObjectURL(blobURL);});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&(active||starting))pause('Вкладка скрыта — видео поставлено на паузу.');});
if(!key)status('Откройте страницу из основной панели Station Bridge.');
