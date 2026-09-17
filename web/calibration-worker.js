importScripts('/calibration-dsp.js');
self.onmessage=({data})=>{
  try{self.postMessage({id:data.id,result:BridgeDSP.detect(data.raw,data.rate)});}
  catch(error){self.postMessage({id:data.id,error:error.message});}
};
