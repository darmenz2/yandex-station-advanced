/* Local, bounded chirp-matching. No remote services or audio uploads. */
(function(root){
  'use strict';
  function chirp(rate, seconds=.08) {
    const n=Math.round(rate*seconds), x=new Float32Array(n);
    for(let i=0;i<n;i++){
      const t=i/rate, f0=650, slope=(2450-f0)/seconds;
      x[i]=Math.sin(Math.PI*i/(n-1))**2*Math.sin(2*Math.PI*(f0*t+slope*t*t/2));
    }
    return x;
  }
  function down(x, factor){
    const y=new Float32Array(Math.floor(x.length/factor));
    for(let i=0;i<y.length;i++) {let sum=0;for(let j=0;j<factor;j++)sum+=x[i*factor+j];y[i]=sum/factor;}
    return y;
  }
  function percentile(a,p){if(!a.length)return null;const v=a.slice().sort((a,b)=>a-b),position=Math.max(0,Math.min(1,p))*(v.length-1),lo=Math.floor(position),hi=Math.ceil(position);return v[lo]+(v[hi]-v[lo])*(position-lo);}
  function detect(raw, rate, maxSeconds=2.5){
    if(!(rate>=8000&&rate<=192000)||raw.length>rate*4)throw Error('Invalid bounded microphone window');
    const factor=Math.max(1,Math.round(rate/6000)), sampleRate=rate/factor;
    const x=down(raw,factor), ref=down(chirp(rate),factor), n=ref.length;
    const energy=new Float64Array(x.length+1);let refEnergy=0;
    for(const v of ref)refEnergy+=v*v;
    let maxPeak=0,clipped=0;
    for(let i=0;i<raw.length;i++){maxPeak=Math.max(maxPeak,Math.abs(raw[i]));if(Math.abs(raw[i])>.995)clipped++;}
    for(let i=0;i<x.length;i++)energy[i+1]=energy[i]+x[i]*x[i];
    const max=Math.min(x.length-n,Math.floor(maxSeconds*sampleRate)), corr=new Float32Array(Math.max(0,max+1));
    let best=0,bestAt=-1;
    for(let k=0;k<=max;k++){
      const e=energy[k+n]-energy[k];if(e/n<1e-9)continue;
      let dot=0;for(let j=0;j<n;j++)dot+=ref[j]*x[k+j];
      const c=Math.abs(dot)/Math.sqrt(e*refEnergy);corr[k]=c;
      if(c>best){best=c;bestAt=k;}
    }
    const candidates=[];const floor=Math.max(.22,best*.65), separation=Math.round(sampleRate*.030);
    // Strong, well-separated peaks reveal direct-PC leakage or room echoes.
    for(let k=0;k<corr.length;k++)if(corr[k]>=floor){
      const prev=candidates.at(-1);
      if(prev&&k-prev.index<separation){if(corr[k]>prev.score){prev.index=k;prev.score=corr[k];}}
      else candidates.push({index:k,score:corr[k]});
    }
    candidates.sort((a,b)=>b.score-a.score);
    const ok=best>=.22&&bestAt>=0&&maxPeak>.0003;
    return {valid:ok,delay_ms:ok?bestAt/sampleRate*1000:null,confidence:best,
      ambiguous:candidates.length>1,peaks_ms:candidates.slice(0,4).map(p=>+(p.index/sampleRate*1000).toFixed(2)),
      clipped:clipped>rate*.01,peak:maxPeak};
  }
  function summarize(rows){
    const good=rows.filter(x=>x.valid&&!x.ambiguous&&!x.clipped).map(x=>x.delay_ms);
    const median=percentile(good,.5),p95=percentile(good,.95),min=percentile(good,0),max=percentile(good,1);
    return {count:rows.length,valid:good.length,missed:rows.length-good.length,
      median_ms:median,p95_ms:p95,spread_ms:good.length?max-min:null,
      jitter_ms:good.length?p95-median:null};
  }
  const api={chirp,detect,summarize,percentile};root.BridgeDSP=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof self!=='undefined'?self:globalThis);
