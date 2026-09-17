"""Native Win32/GDI tray flyout, not a browser page or WebView.

All window operations stay on the notification-area thread. Audio and network
commands are dispatched to asyncio; rendering never opens a mic or starts audio.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import copy
import threading
from .flyout_model import BG,CARD,TEXT,MUTED,YELLOW,LINE
from .reliable_model import Model

class NativeFlyout:
    def __init__(self,dispatch,instance):
        self.dispatch=dispatch;self.instance=instance;self.lock=threading.Lock();self.pending={};self.notices=[]
        self.hwnd=None;self.visible=False;self.modal=False;self.scale=1.;self.height=660;self.width=430;self.focus=-1;self.drag=None
        self.nodes=[];self.resources=[]
        self.model=Model(dispatch,self.confirm)
        self._init_api();self._create()
    def _init_api(self):
        self.u=C.WinDLL('user32',use_last_error=True);self.g=C.WinDLL('gdi32',use_last_error=True)
        def p(lib,n,r,a):f=getattr(lib,n);f.restype=r;f.argtypes=a;return f
        u,g=self.u,self.g;P=C.c_void_p;I=C.c_int;U=C.c_uint
        self.WNDPROC=C.WINFUNCTYPE(C.c_ssize_t,W.HWND,U,W.WPARAM,W.LPARAM)
        class WC(C.Structure):_fields_=[('style',U),('proc',self.WNDPROC),('cls',I),('wnd',I),('instance',P),('icon',P),('cursor',P),('brush',P),('menu',W.LPCWSTR),('name',W.LPCWSTR)]
        class Paint(C.Structure):_fields_=[('dc',P),('erase',I),('rect',W.RECT),('restore',I),('incremental',I),('reserved',C.c_ubyte*32)]
        class Monitor(C.Structure):_fields_=[('size',W.DWORD),('monitor',W.RECT),('work',W.RECT),('flags',W.DWORD)]
        self.WC,self.Paint,self.Monitor=WC,Paint,Monitor
        p(u,'RegisterClassW',W.ATOM,[C.POINTER(WC)]);p(u,'CreateWindowExW',P,[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,I,I,I,I,P,P,P,P])
        p(u,'DefWindowProcW',C.c_ssize_t,[P,U,W.WPARAM,W.LPARAM]);p(u,'ShowWindow',I,[P,I]);p(u,'SetWindowPos',I,[P,P,I,I,I,I,U])
        p(u,'DestroyWindow',I,[P]);p(u,'SetForegroundWindow',I,[P]);p(u,'SetFocus',P,[P]);p(u,'SetCapture',P,[P]);p(u,'ReleaseCapture',I,[])
        p(u,'InvalidateRect',I,[P,P,I]);p(u,'SetTimer',C.c_size_t,[P,C.c_size_t,U,P]);p(u,'KillTimer',I,[P,C.c_size_t])
        p(u,'PostMessageW',I,[P,U,W.WPARAM,W.LPARAM]);p(u,'GetCursorPos',I,[C.POINTER(W.POINT)])
        p(u,'MonitorFromPoint',P,[W.POINT,W.DWORD]);p(u,'GetMonitorInfoW',I,[P,C.POINTER(Monitor)])
        p(u,'GetClientRect',I,[P,C.POINTER(W.RECT)]);p(u,'MessageBoxW',I,[P,W.LPCWSTR,W.LPCWSTR,U])
        p(u,'BeginPaint',P,[P,C.POINTER(Paint)]);p(u,'EndPaint',I,[P,C.POINTER(Paint)])
        p(u,'DrawTextW',I,[P,W.LPCWSTR,I,C.POINTER(W.RECT),U]);p(u,'FillRect',I,[P,C.POINTER(W.RECT),P])
        p(u,'LoadCursorW',P,[P,P]);p(u,'GetKeyState',C.c_short,[I])
        p(g,'CreateCompatibleDC',P,[P]);p(g,'CreateCompatibleBitmap',P,[P,I,I]);p(g,'SelectObject',P,[P,P]);p(g,'DeleteDC',I,[P]);p(g,'DeleteObject',I,[P]);p(g,'BitBlt',I,[P,I,I,I,I,P,I,I,W.DWORD])
        p(g,'CreateSolidBrush',P,[W.DWORD]);p(g,'SetTextColor',W.DWORD,[P,W.DWORD]);p(g,'SetBkMode',I,[P,I])
        p(g,'CreateFontW',P,[I,I,I,I,I,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.LPCWSTR])
        p(g,'RoundRect',I,[P,I,I,I,I,I,I]);p(g,'CreatePen',P,[I,I,W.DWORD]);p(g,'SaveDC',I,[P]);p(g,'RestoreDC',I,[P,I]);p(g,'IntersectClipRect',I,[P,I,I,I,I])
    def _create(self):
        @self.WNDPROC
        def proc(h,m,w,l):
            try:return self._proc(h,m,w,l)
            except Exception as exc:
                self.model.toast('Меню: '+type(exc).__name__);return self.u.DefWindowProcW(h,m,w,l)
        self.proc=proc
        klass=self.WC(3,proc,0,0,self.instance,None,self.u.LoadCursorW(None,C.c_void_p(32512)),None,None,'YSA.NativeFlyout')
        if not self.u.RegisterClassW(C.byref(klass)):raise C.WinError(C.get_last_error())
        # WS_EX_TOOLWINDOW | WS_EX_TOPMOST; WS_POPUP | WS_BORDER. No taskbar button.
        self.hwnd=self.u.CreateWindowExW(0x88,klass.name,'Яндекс станция · Advanced menu',0x80800000,0,0,430,660,None,None,self.instance,None)
        if not self.hwnd:raise C.WinError(C.get_last_error())
        self.u.SetTimer(self.hwnd,1,250,None)
    def update(self,state):
        with self.lock:self.pending=copy.deepcopy(state)
    def toast(self,text):
        with self.lock:self.notices.append(str(text))
        if self.hwnd:self.u.PostMessageW(self.hwnd,0x8002,0,0)
    def show(self):
        if not self.hwnd:return
        point=W.POINT();self.u.GetCursorPos(C.byref(point));monitor=self.u.MonitorFromPoint(point,2)
        info=self.Monitor();info.size=C.sizeof(info);self.u.GetMonitorInfoW(monitor,C.byref(info));work=info.work
        try:
            dll=C.WinDLL('shcore');dpi=C.c_uint();dy=C.c_uint()
            dll.GetDpiForMonitor.argtypes=[C.c_void_p,C.c_int,C.POINTER(C.c_uint),C.POINTER(C.c_uint)]
            if dll.GetDpiForMonitor(monitor,0,C.byref(dpi),C.byref(dy))==0:self.scale=max(1,min(3,dpi.value/96))
        except (OSError,AttributeError):pass
        self.width=min(430,int((work.right-work.left-16)/self.scale));self.height=min(700,int((work.bottom-work.top-16)/self.scale))
        width=int(self.width*self.scale);height=int(self.height*self.scale)
        x=max(work.left+8,min(point.x-width,work.right-width-8));y=max(work.top+8,min(point.y-height,work.bottom-height-8))
        self.visible=True;self.u.SetWindowPos(self.hwnd,C.c_void_p(-1),x,y,width,height,0x40)
        self.u.ShowWindow(self.hwnd,5);self.u.SetForegroundWindow(self.hwnd);self.u.SetFocus(self.hwnd);self.repaint()
    def hide(self):
        if self.modal:return
        room=self.model.state.get('room',{})
        if room.get('status') in ('starting','measuring','analyzing'):self.dispatch('advanced',{'command':'room_cancel'})
        self.visible=False;self.u.ShowWindow(self.hwnd,0)
    def destroy(self):
        if self.hwnd:self.u.KillTimer(self.hwnd,1);self.u.DestroyWindow(self.hwnd);self.hwnd=None
    def confirm(self,text):
        self.modal=True
        try:return self.u.MessageBoxW(self.hwnd,text,'Яндекс станция',0x24|0x100)==6
        finally:self.modal=False
    def repaint(self):
        if self.hwnd:self.u.InvalidateRect(self.hwnd,None,False)
    def _xy(self,l):return C.c_short(l&65535).value/self.scale,C.c_short((l>>16)&65535).value/self.scale
    def _hit(self,x,y):
        for i,n in enumerate(self.nodes):
            ny=n.y if n.kind=='header' or n.y<78 else n.y-self.model.scroll
            if n.action and n.x<=x<n.x+n.w and ny<=y<ny+n.h and (n.y<78 or y>=78):return i,n
        return -1,None
    def _slider(self,n,x):
        return n.minimum+(n.maximum-n.minimum)*max(0,min(1,(x-n.x-8)/(n.w-16)))
    def _proc(self,h,m,w,l):
        if m==0x14:return 1
        if m==0xf:self._paint();return 0
        if m in (0x113,0x8002):
            with self.lock:state=self.pending;notices,self.notices=self.notices,[]
            self.model.update(state)
            for n in notices:self.model.toast(n)
            if self.visible:self.repaint()
            return 0
        if m==0x6 and (w&65535)==0:self.hide();return 0
        if m==0x10:self.hide();return 0
        if m==0x100:
            if w==27:self.hide();return 0
            actions=[i for i,n in enumerate(self.nodes) if n.action]
            if w==9 and actions:
                direction=-1 if self.u.GetKeyState(16)<0 else 1
                self.focus=actions[((actions.index(self.focus) if self.focus in actions else -1)+direction)%len(actions)]
                n=self.nodes[self.focus];self.model.scroll=max(0,min(n.y-90,max(0,self.model.total-self.height)));self.repaint();return 0
            if w in (13,32) and self.focus in actions:
                n=self.nodes[self.focus];self.model.act(n.action,n.data,n.value if n.kind=='slider' else None);self.repaint();return 0
            if w in (37,39) and self.focus in actions and self.nodes[self.focus].kind=='slider':
                n=self.nodes[self.focus];v=max(n.minimum,min(n.maximum,n.value+(n.maximum-n.minimum)*(.01 if w==39 else -.01)))
                self.model.act(n.action,n.data,v);self.repaint();return 0
        if m==0x20a:
            delta=C.c_short((w>>16)&65535).value
            self.model.scroll=max(0,min(max(0,self.model.total-self.height),self.model.scroll-delta/120*55));self.repaint();return 0
        if m==0x201:
            x,y=self._xy(l);i,n=self._hit(x,y);self.focus=i
            if n and n.kind=='slider':self.drag=(n,self._slider(n,x));self.u.SetCapture(h);self.repaint()
            return 0
        if m==0x200 and self.drag:
            x,y=self._xy(l);n,v=self.drag;self.drag=(n,self._slider(n,x));self.repaint();return 0
        if m==0x202:
            x,y=self._xy(l)
            if self.drag:
                n,v=self.drag;self.drag=None;self.u.ReleaseCapture();self.model.act(n.action,n.data,v)
            else:
                i,n=self._hit(x,y)
                if n:self.model.act(n.action,n.data)
            self.repaint();return 0
        return self.u.DefWindowProcW(h,m,w,l)
    def _color(self,value):
        h=value.lstrip('#');r,g,b=[int(h[i:i+2],16) for i in (0,2,4)];return r|(g<<8)|(b<<16)
    def rect(self,dc,x,y,w,h,color):
        r=W.RECT(round(x*self.scale),round(y*self.scale),round((x+w)*self.scale),round((y+h)*self.scale));brush=self.g.CreateSolidBrush(self._color(color))
        self.u.FillRect(dc,C.byref(r),brush);self.g.DeleteObject(brush)
    def text(self,dc,text,x,y,w,h,color,size=14,bold=False):
        font=self.g.CreateFontW(-round(size*self.scale),0,0,0,600 if bold else 400,0,0,0,1,0,0,5,0,'Segoe UI')
        old=self.g.SelectObject(dc,font);self.g.SetBkMode(dc,1);self.g.SetTextColor(dc,self._color(color))
        r=W.RECT(round(x*self.scale),round(y*self.scale),round((x+w)*self.scale),round((y+h)*self.scale))
        self.u.DrawTextW(dc,str(text),-1,C.byref(r),0x10|0x800|0x20000) # wrap, no prefix, ellipsis
        self.g.SelectObject(dc,old);self.g.DeleteObject(font)
    def _paint(self):
        if not self.hwnd:return
        ps=self.Paint();target=self.u.BeginPaint(self.hwnd,C.byref(ps));r=W.RECT();self.u.GetClientRect(self.hwnd,C.byref(r))
        dc=self.g.CreateCompatibleDC(target);bitmap=self.g.CreateCompatibleBitmap(target,r.right,r.bottom);old=self.g.SelectObject(dc,bitmap)
        try:
            self.rect(dc,0,0,self.width,self.height,BG)
            self.nodes=self.model.build(self.width);self.model.scroll=max(0,min(self.model.scroll,max(0,self.model.total-self.height)))
            for i,n in enumerate(self.nodes):
                y=n.y if n.y<78 else n.y-self.model.scroll
                if y+n.h<=78 and n.y>=78 or y>=self.height:continue
                saved=self.g.SaveDC(dc)
                if n.y>=78:self.g.IntersectClipRect(dc,0,round(78*self.scale),r.right,r.bottom)
                if n.kind=='header':self.text(dc,n.text,n.x,y,n.w,30,TEXT,21,True);self.text(dc,'ADVANCED',n.x,y+30,n.w,14,YELLOW,10,True)
                elif n.kind=='label':self.text(dc,n.text,n.x,y,n.w,n.h,MUTED,13)
                elif n.kind=='button':
                    self.rect(dc,n.x,y,n.w,n.h,YELLOW if n.accent else CARD)
                    if i==self.focus:self.rect(dc,n.x,y,3,n.h,YELLOW)
                    self.text(dc,n.text,n.x+12,y+10,n.w-24,n.h-10,BG if n.accent else TEXT if n.action else MUTED,12 if n.small else 14,n.accent)
                elif n.kind=='slider':
                    value=self.drag[1] if self.drag and self.drag[0].action==n.action and self.drag[0].data==n.data else n.value
                    label=f'{round(value*100)}%' if n.maximum==1 else f'{value:.1f}'
                    self.text(dc,n.text+'  '+label,n.x,y,n.w,24,TEXT,13)
                    q=max(0,min(1,(value-n.minimum)/(n.maximum-n.minimum)))
                    self.rect(dc,n.x+8,y+40,n.w-16,4,LINE);self.rect(dc,n.x+8,y+40,(n.w-16)*q,4,YELLOW)
                    self.rect(dc,n.x+3+(n.w-16)*q,y+35,10,14,YELLOW)
                elif n.kind=='qr':
                    matrix=n.data['matrix'];length=len(matrix);module=max(1,int(n.w*self.scale/length));physical=module*length
                    x=round(n.x*self.scale)+(round(n.w*self.scale)-physical)//2;top=round(y*self.scale)
                    self.rect(dc,n.x,y,n.w,n.h,'#ffffff')
                    brush=self.g.CreateSolidBrush(0)
                    for j,row in enumerate(matrix):
                        for k,v in enumerate(row):
                            if v:
                                rr=W.RECT(x+k*module,top+j*module,x+(k+1)*module,top+(j+1)*module);self.u.FillRect(dc,C.byref(rr),brush)
                    self.g.DeleteObject(brush)
                self.g.RestoreDC(dc,saved)
            if self.model.total>self.height:
                ratio=(self.height-78)/(self.model.total-78);bar=max(25,(self.height-78)*ratio);top=78+(self.height-78-bar)*self.model.scroll/max(1,self.model.total-self.height)
                self.rect(dc,self.width-5,top,3,bar,LINE)
            self.g.BitBlt(target,0,0,r.right,r.bottom,dc,0,0,0xcc0020)
        finally:
            self.g.SelectObject(dc,old);self.g.DeleteObject(bitmap);self.g.DeleteDC(dc);self.u.EndPaint(self.hwnd,C.byref(ps))
