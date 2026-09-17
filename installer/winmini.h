/* Minimal Windows x64 ABI declarations for a freestanding LLVM build.
   No kernel driver, injected code, or third-party runtime is used. */
#ifndef YSA_WINMINI_H
#define YSA_WINMINI_H
#define DLL __declspec(dllimport)
typedef unsigned short WCHAR;
typedef const WCHAR *LPCWSTR;
typedef WCHAR *LPWSTR;
typedef unsigned int DWORD, UINT;
typedef int BOOL, INT;
typedef unsigned short WORD, ATOM;
typedef unsigned char BYTE;
typedef unsigned long long UINT_PTR, SIZE_T, WPARAM;
typedef long long LONG_PTR, LPARAM, LRESULT;
typedef void *HANDLE, *HWND, *HINSTANCE, *HICON, *HCURSOR, *HBRUSH, *HDC, *HFONT, *HMENU;
typedef void *LPVOID;
typedef LRESULT (*WNDPROC)(HWND,UINT,WPARAM,LPARAM);
typedef struct {int x,y;} POINT;
typedef struct {int left,top,right,bottom;} RECT;
typedef struct {DWORD cb;LPWSTR reserved,desktop,title;DWORD x,y,xsize,ysize,xchars,ychars,fill,flags;WORD show,reserved2;BYTE *reserved3;HANDLE input,output,error;} STARTUPINFOW;
typedef struct {HANDLE process,thread;DWORD pid,tid;} PROCESS_INFORMATION;
typedef struct {DWORD length;void *descriptor;BOOL inherit;} SECURITY_ATTRIBUTES;
typedef struct {UINT style;WNDPROC proc;int cls,wnd;HINSTANCE instance;HICON icon;HCURSOR cursor;HBRUSH background;LPCWSTR menu,name;} WNDCLASSW;
typedef struct {HWND hwnd;UINT message;WPARAM wParam;LPARAM lParam;DWORD time;POINT pt;DWORD private_;} MSG;
typedef struct {DWORD cb;DWORD flags;} INITCOMMONCONTROLSEX;
typedef struct {HWND owner;void *root;LPWSTR name;LPCWSTR title;UINT flags;void *callback;LPARAM data;int image;} BROWSEINFOW;
typedef struct {DWORD attr;DWORD t1[2],t2[2],t3[2],sizeHigh,sizeLow,reserved0,reserved1;WCHAR name[260],alternate[14];} WIN32_FIND_DATAW;
DLL HANDLE GetModuleHandleW(LPCWSTR); DLL DWORD GetModuleFileNameW(HANDLE,LPWSTR,DWORD);
DLL LPWSTR GetCommandLineW(void); DLL DWORD GetEnvironmentVariableW(LPCWSTR,LPWSTR,DWORD);
DLL DWORD GetLastError(void); DLL void ExitProcess(UINT); DLL DWORD GetCurrentProcessId(void);
DLL DWORD GetTempPathW(DWORD,LPWSTR); DLL UINT GetTempFileNameW(LPCWSTR,LPCWSTR,UINT,LPWSTR);
DLL BOOL CreateDirectoryW(LPCWSTR,void*); DLL BOOL RemoveDirectoryW(LPCWSTR); DLL BOOL DeleteFileW(LPCWSTR);
DLL BOOL CopyFileW(LPCWSTR,LPCWSTR,BOOL); DLL DWORD GetFileAttributesW(LPCWSTR);
DLL HANDLE CreateFileW(LPCWSTR,DWORD,DWORD,SECURITY_ATTRIBUTES*,DWORD,DWORD,HANDLE);
DLL BOOL ReadFile(HANDLE,void*,DWORD,DWORD*,void*); DLL BOOL WriteFile(HANDLE,const void*,DWORD,DWORD*,void*);
DLL DWORD GetFileSize(HANDLE,DWORD*); DLL BOOL SetFilePointerEx(HANDLE,long long,long long*,DWORD); DLL BOOL CloseHandle(HANDLE);
DLL BOOL CreateProcessW(LPCWSTR,LPWSTR,void*,void*,BOOL,DWORD,void*,LPCWSTR,STARTUPINFOW*,PROCESS_INFORMATION*);
DLL BOOL GetExitCodeProcess(HANDLE,DWORD*); DLL DWORD WaitForSingleObject(HANDLE,DWORD);
DLL HANDLE CreateMutexW(void*,BOOL,LPCWSTR); DLL HANDLE OpenMutexW(DWORD,BOOL,LPCWSTR);
DLL HANDLE LocalAlloc(UINT,SIZE_T); DLL HANDLE LocalFree(HANDLE);
DLL int MultiByteToWideChar(UINT,DWORD,const char*,int,LPWSTR,int);
DLL int lstrlenW(LPCWSTR); DLL int lstrcmpW(LPCWSTR,LPCWSTR); DLL int lstrcmpiW(LPCWSTR,LPCWSTR);
DLL HANDLE FindFirstFileW(LPCWSTR,WIN32_FIND_DATAW*); DLL BOOL FindNextFileW(HANDLE,WIN32_FIND_DATAW*); DLL BOOL FindClose(HANDLE);
DLL ATOM RegisterClassW(const WNDCLASSW*);
DLL HWND CreateWindowExW(DWORD,LPCWSTR,LPCWSTR,DWORD,int,int,int,int,HWND,HMENU,HINSTANCE,void*);
DLL LRESULT DefWindowProcW(HWND,UINT,WPARAM,LPARAM); DLL BOOL DestroyWindow(HWND);
DLL BOOL ShowWindow(HWND,int); DLL BOOL UpdateWindow(HWND); DLL BOOL EnableWindow(HWND,BOOL);
DLL BOOL GetMessageW(MSG*,HWND,UINT,UINT); DLL BOOL TranslateMessage(const MSG*); DLL LRESULT DispatchMessageW(const MSG*);
DLL BOOL IsDialogMessageW(HWND,MSG*); DLL void PostQuitMessage(int);
DLL LRESULT SendMessageW(HWND,UINT,WPARAM,LPARAM); DLL BOOL SetWindowTextW(HWND,LPCWSTR);
DLL int GetWindowTextW(HWND,LPWSTR,int); DLL HWND SetFocus(HWND); DLL int MessageBoxW(HWND,LPCWSTR,LPCWSTR,UINT);
DLL HICON LoadIconW(HINSTANCE,LPCWSTR); DLL HCURSOR LoadCursorW(HINSTANCE,LPCWSTR);
DLL int GetSystemMetrics(int); DLL BOOL AdjustWindowRectEx(RECT*,DWORD,BOOL,DWORD);
DLL HDC GetDC(HWND); DLL int ReleaseDC(HWND,HDC); DLL UINT_PTR SetTimer(HWND,UINT_PTR,UINT,void*); DLL BOOL KillTimer(HWND,UINT_PTR);
DLL int SetBkMode(HDC,int); DLL DWORD SetTextColor(HDC,DWORD); DLL DWORD SetBkColor(HDC,DWORD);
DLL HBRUSH CreateSolidBrush(DWORD); DLL BOOL DeleteObject(HANDLE); DLL int GetDeviceCaps(HDC,int);
DLL HFONT CreateFontW(int,int,int,int,int,DWORD,DWORD,DWORD,DWORD,DWORD,DWORD,DWORD,DWORD,LPCWSTR);
DLL BOOL InitCommonControlsEx(const INITCOMMONCONTROLSEX*);
DLL LONG_PTR ShellExecuteW(HWND,LPCWSTR,LPCWSTR,LPCWSTR,LPCWSTR,int);
DLL LPWSTR *CommandLineToArgvW(LPCWSTR,int*); DLL void *SHBrowseForFolderW(BROWSEINFOW*);
DLL BOOL SHGetPathFromIDListW(void*,LPWSTR); DLL int SHGetFolderPathW(HWND,int,HANDLE,DWORD,LPWSTR);
DLL long CoInitializeEx(void*,DWORD); DLL void CoUninitialize(void); DLL void CoTaskMemFree(void*);
_Static_assert(sizeof(WCHAR)==2, "WCHAR must be UTF-16");
_Static_assert(sizeof(STARTUPINFOW)==104, "STARTUPINFOW x64 ABI");
_Static_assert(sizeof(WNDCLASSW)==72, "WNDCLASSW x64 ABI");
_Static_assert(sizeof(MSG)==48, "MSG x64 ABI");
_Static_assert(sizeof(BROWSEINFOW)==64, "BROWSEINFOW x64 ABI");
#define INVALID_HANDLE ((HANDLE)(LONG_PTR)-1)
#define LIT(x) ((LPCWSTR)L##x)
#define WSTR(x) ((LPWSTR)(x))
static int wlen(LPCWSTR s){return s?lstrlenW(s):0;}
static void wcop(WCHAR *d,int cap,LPCWSTR s){int i=0;if(!s)s=LIT("");while(i<cap-1&&s[i]){d[i]=s[i];i++;}d[i]=0;}
static void wcat(WCHAR *d,int cap,LPCWSTR s){int n=wlen(d);wcop(d+n,cap-n,s);}
static void join(WCHAR *d,int cap,LPCWSTR a,LPCWSTR b){wcop(d,cap,a);if(wlen(d)&&d[wlen(d)-1]!='\\')wcat(d,cap,LIT("\\"));wcat(d,cap,b);}
static void parent(WCHAR *p){int n=wlen(p);while(n>0&&p[n-1]!='\\'&&p[n-1]!='/')n--;if(n>0)p[n-1]=0;}
static BOOL exists(LPCWSTR p){return GetFileAttributesW(p)!=0xffffffff;}
static void zero(void *p,SIZE_T n){volatile BYTE *b=(BYTE*)p;while(n--)*b++=0;}
void *memset(void *p,int v,SIZE_T n){volatile BYTE *b=(BYTE*)p;while(n--)*b++=(BYTE)v;return p;}
void *memcpy(void *d,const void *s,SIZE_T n){BYTE *a=(BYTE*)d;const BYTE*b=(const BYTE*)s;while(n--)*a++=*b++;return d;}
#endif
