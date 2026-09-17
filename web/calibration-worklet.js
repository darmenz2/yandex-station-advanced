/* Microphone samples remain inside this page. Output is always zero: no feedback. */
class BridgeMicProcessor extends AudioWorkletProcessor {
  constructor(){super();this.chunk=new Float32Array(1024);this.used=0;this.first=0;this.active=true;
    this.port.onmessage=e=>{if(e.data==='stop')this.active=false;};}
  process(inputs,outputs){
    for(const output of outputs)for(const channel of output)channel.fill(0);
    if(!this.active)return false;
    const input=inputs[0]?.[0];if(!input)return true;
    for(let i=0;i<input.length;i++){
      if(!this.used)this.first=currentFrame+i;
      this.chunk[this.used++]=input[i];
      if(this.used===this.chunk.length){const data=this.chunk;this.port.postMessage({frame:this.first,data},[data.buffer]);this.chunk=new Float32Array(1024);this.used=0;}
    }
    return true;
  }
}
registerProcessor('bridge-mic',BridgeMicProcessor);
