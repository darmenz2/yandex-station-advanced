/* Original allocation-free DSP, MIT. ABI uses fixed-width Win64-compatible types. */
#if defined(_WIN32)
#define API __declspec(dllexport)
int _fltused = 0;
#else
#define API __attribute__((visibility("default")))
#endif
typedef unsigned int u32; typedef unsigned long long u64; typedef signed short i16;
typedef struct {double matrix[16],coef[10],history[8],gain; int channels,filter_kind;u64 clipped;} State;
API int ysa_process(State *s,const i16 *in,i16 *out,u32 frames){
 if(!s||!in||!out||s->channels<1||s->channels>8||frames>192000)return -1;
 for(u32 n=0;n<frames;n++)for(int c=0;c<2;c++){
  double v=0;for(int j=0;j<s->channels;j++)v+=in[n*s->channels+j]*s->matrix[c*8+j];
  if(s->filter_kind){
   double *a=s->coef+(s->filter_kind==2?5:0);
   for(int stage=0;stage<2;stage++){
    double *h=s->history+c*4+stage*2;
    double y=a[0]*v+h[0];h[0]=a[1]*v-a[3]*y+h[1];h[1]=a[2]*v-a[4]*y;v=y;
   }
  }
  v*=s->gain;
  if(v>32767){v=32767;s->clipped++;}else if(v<-32768){v=-32768;s->clipped++;}
  out[n*2+c]=(i16)(v>=0?v+.5:v-.5);
 }
 return 0;
}
/* Normalized squared cross-correlation. Score = r^2, accepts polarity inversion.
   Reference has zero mean; local DC rejected via moving energy. */
API int ysa_correlate(const float *x,u32 n,const float *ref,u32 m,double *score){
 if(!x||!ref||!score||m<16||n<m||n>2000000)return -1;
 double re=0,sum=0,energy=0,best=0;int pos=-1;
 for(u32 j=0;j<m;j++){re+=(double)ref[j]*ref[j];sum+=x[j];energy+=(double)x[j]*x[j];}
 if(re<1e-18){*score=0;return -1;}
 for(u32 i=0;i<=n-m;i++){
  double e=energy-sum*sum/m;
  if(e>1e-12){double dot=0;for(u32 j=0;j<m;j++)dot+=(double)x[i+j]*ref[j];
   double v=dot*dot/(e*re);if(v>best){best=v;pos=(int)i;}}
  if(i<n-m){double a=x[i],b=x[i+m];sum+=b-a;energy+=b*b-a*a;}
 }
 *score=best;return pos;
}

/* 16-tap polyphase fractional reader. Window coefficients are generated once
   by the Python host; all state belongs to one reader. No channel mixing. */
API int ysa_resample(const i16 *in,u32 count,i16 *out,u32 wanted,u32 channels,
                    double start,double ratio0,double ratio1,const double *table,double *end){
 if(!in||!out||!table||!end||channels<1||channels>8||count>200000||wanted>19200||
    start<0||ratio0<.99||ratio0>1.01||ratio1<.99||ratio1>1.01)return -1;
 double pos=start;u32 n=0;
 for(;n<wanted;n++){
  int at=(int)pos;if(at<7||at+8>=(int)count)break;
  double frac=(pos-at)*256.;int phase=(int)frac;double weight=frac-phase;
  for(u32 c=0;c<channels;c++){
   double v=0;for(int k=0;k<16;k++){
    double a=table[phase*16+k],b=table[(phase+1)*16+k];
    v+=in[(at-7+k)*channels+c]*(a+(b-a)*weight);
   }
   if(v>32767)v=32767;if(v<-32768)v=-32768;
   out[n*channels+c]=(i16)(v>=0?v+.5:v-.5);
  }
  pos+=ratio0+(ratio1-ratio0)*(double)(n+1)/(double)wanted;
 }
 *end=pos;return (int)n;
}
