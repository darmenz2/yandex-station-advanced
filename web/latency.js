'use strict';
const byId = id => document.getElementById(id);
let ctx=null, nodes=[], timers=[], animation=0, marks=[], run=0, starting=false;
async function stopTest(message='Тест остановлен.') {
  ++run; cancelAnimationFrame(animation); timers.forEach(clearTimeout); timers=[];
  nodes.forEach(n=>{try{n.stop();n.disconnect();}catch(_){}}); nodes=[];
  const old=ctx;ctx=null; if(old) await old.close().catch(()=>{});
  byId('lamp').classList.remove('on'); byId('lamp').textContent='Готово';
  byId('begin').disabled=false;byId('end').disabled=true;byId('status').textContent=message;
}
byId('begin').addEventListener('click',async()=>{
  if(starting||ctx)return;
  starting=true;
  await stopTest('Готовим сигналы…');
  byId('begin').disabled=true;
  const generation=run;
  try {
    const local=new AudioContext({latencyHint:'interactive'});ctx=local;await local.resume();
    if(generation!==run){await local.close().catch(()=>{});if(ctx===local)ctx=null;return;}
    const start=local.currentTime+1;marks=[];
    byId('begin').disabled=true;byId('end').disabled=false;
    for(let i=0;i<5;i++) {
      const t=start+3*i; const oscillator=local.createOscillator();const gain=local.createGain();
      oscillator.type='sine';oscillator.frequency.value=1800;
      gain.gain.setValueAtTime(0,t);gain.gain.linearRampToValueAtTime(.06,t+.002);
      gain.gain.setValueAtTime(.06,t+.028);gain.gain.linearRampToValueAtTime(0,t+.035);
      oscillator.connect(gain);gain.connect(local.destination);oscillator.start(t);oscillator.stop(t+.04);
      nodes.push(oscillator);marks.push(t);
    }
    byId('status').textContent='Пять сигналов. Не меняйте выход Windows и не сворачивайте эту вкладку.';
    byId('clock').textContent=`Частота браузера: ${local.sampleRate} Гц. Оценка baseLatency браузера: ${Math.round((local.baseLatency||0)*1000)} мс; это НЕ задержка Станции. Метка сравнивается с часами AudioContext, а не с фактическим динамиком.`;
    const draw=()=>{
      if(run!==generation||ctx!==local)return;
      // Use the context timeline deliberately: consistent generated-reference
      // timing for A/B testing. Do not label this as measured acoustic latency.
      const now=local.currentTime;const i=marks.findIndex(t=>now>=t&&now<t+.15);
      byId('lamp').classList.toggle('on',i>=0);
      byId('lamp').textContent=i>=0?String(i+1):'·';
      animation=requestAnimationFrame(draw);
    };draw();
    timers.push(setTimeout(()=>stopTest('Пять сигналов переданы. Дождитесь последнего звука Станции и сравните запись.'),16000));
  }catch(error){await stopTest('Не удалось открыть звуковой выход браузера: '+error.message);}
  finally{starting=false;}
});
byId('end').addEventListener('click',()=>stopTest());
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopTest('Вкладка скрыта, тест остановлен.');});
window.addEventListener('pagehide',()=>stopTest());
function calculate(){const fps=Number(byId('fps').value),frames=Number(byId('frames').value);byId('delay').textContent=(fps>0&&fps<=1000&&frames>=0&&frames<=30000)?`≈ ${Math.round(frames*1000/fps)} мс`:'Проверьте значения';}
byId('fps').addEventListener('input',calculate);byId('frames').addEventListener('input',calculate);calculate();
