/* Native Windows GUI installer. PowerShell runs as a hidden child process.
   This window owns consent, progress, cancellation, retry and final launch. */
#include "winmini.h"
#ifndef REMOVE_ONLY
#include "payload.inc"
#endif
#define CHILD 0x50000000
#define TAB 0x00010000
#define C_PATH 101
#define C_BROWSE 102
#define C_DESKTOP 103
#define C_AUTO 104
#define C_IMPORT 105
#define C_GO 201
#define C_CANCEL 202
#define C_LOG 203
#define C_LAUNCH 204
#define C_DETAILS 205
static HINSTANCE instance;
static HWND window,title,subtitle,pathEdit,browse,desk,autostart,importOld,pathLabel,explain,footnote;
static HWND go,cancel,logButton,launch,details,status,progress,resultbox,versionlabel,line;
static HFONT font,large,small,mono;
static HBRUSH white;
static int scale=100,phase=0,successful=0,cancelled=0,expanded=0,removing=0;
static DWORD processExit;
static HANDLE process=0,setupMutex=0;
static WCHAR target[1200],work[1200],logPath[1400],fallbackLog[1400],selfPath[1200],sourceRoot[1200];
static WCHAR cmd[7000],buffer[22000],lastStatus[1600],script[1600],payloadPath[1600],systemPS[1400],scratch[1600];
static char raw[65536];
static int px(int n){return n*scale/100;}
static HWND control(LPCWSTR cls,LPCWSTR text,DWORD style,int id,int x,int y,int w,int h){
    HWND c=CreateWindowExW(!lstrcmpW(cls,LIT("EDIT"))?0x200:0,cls,text,CHILD|style,px(x),px(y),px(w),px(h),window,(HMENU)(UINT_PTR)id,instance,0);
    SendMessageW(c,0x30,(WPARAM)font,1);return c;
}
static void setfont(HWND h,HFONT f){SendMessageW(h,0x30,(WPARAM)f,1);}
static BOOL write_bytes(LPCWSTR name,const void *data,DWORD size){
    HANDLE h=CreateFileW(name,0x40000000,1,0,2,0x80,0);if(h==INVALID_HANDLE)return 0;
    DWORD written=0;BOOL ok=WriteFile(h,data,size,&written,0);CloseHandle(h);return ok&&written==size;
}
static int read_text(LPCWSTR name,WCHAR *text,int cap,BOOL tail){
    HANDLE h=CreateFileW(name,0x80000000,7,0,3,0x80,0);if(h==INVALID_HANDLE){text[0]=0;return 0;}
    DWORD size=GetFileSize(h,0),got=0;
    /* Installer output is capped per GUI read; a complete log is on disk. */
    if(tail&&size>sizeof(raw)-1){SetFilePointerEx(h,(long long)size-(sizeof(raw)-1),0,0);size=sizeof(raw)-1;}
    else if(size>sizeof(raw)-1)size=sizeof(raw)-1;
    if(!ReadFile(h,raw,size,&got,0)){CloseHandle(h);text[0]=0;return 0;}CloseHandle(h);
    int start=0;if(tail&&got>18000)start=(int)got-18000;
    if(got>=3&&(BYTE)raw[0]==0xef&&(BYTE)raw[1]==0xbb&&(BYTE)raw[2]==0xbf)start=3;
    int n=MultiByteToWideChar(65001,0,raw+start,(int)got-start,text,cap-1);if(n<0)n=0;text[n]=0;return n;
}
static BOOL makework(void){
    static WCHAR tmp[1000];GetTempPathW(1000,tmp);
    if(!GetTempFileNameW(tmp,LIT("YSA"),0,work))return 0;
    DeleteFileW(work);return CreateDirectoryW(work,0);
}
static void quote(WCHAR*d,int cap,LPCWSTR s){wcat(d,cap,LIT("\""));wcat(d,cap,s);wcat(d,cap,LIT("\""));}
static void process_error(LPCWSTR message){
    phase=3;successful=0;
    HWND opts[]={pathEdit,browse,desk,autostart,importOld,pathLabel,explain,footnote};
    for(int i=0;i<8;i++)ShowWindow(opts[i],0);
    ShowWindow(status,5);ShowWindow(progress,0);ShowWindow(resultbox,5);SetWindowTextW(resultbox,message);
    SetWindowTextW(title,removing?LIT("Удаление не завершено"):LIT("Установка не завершена"));SetWindowTextW(status,LIT("Подробная причина — ниже. Повторная загрузка всей среды не обязательна."));
    SetWindowTextW(logButton,LIT("Журнал"));SetWindowTextW(go,LIT("Повторить"));EnableWindow(go,1);SetWindowTextW(cancel,LIT("Закрыть"));EnableWindow(cancel,1);ShowWindow(logButton,5);
}
static void show_progress(void){
    phase=1;successful=0;cancelled=0;lastStatus[0]=0;
    HWND opts[]={pathEdit,browse,desk,autostart,importOld,pathLabel,explain,footnote};
    for(int i=0;i<8;i++)ShowWindow(opts[i],0);
    ShowWindow(resultbox,0);ShowWindow(launch,0);ShowWindow(progress,5);ShowWindow(status,5);ShowWindow(logButton,5);ShowWindow(details,5);
    SetWindowTextW(title,removing?LIT("Удаление приложения"):LIT("Установка приложения"));
    SetWindowTextW(subtitle,LIT("Все шаги выполняются в этом окне. Консоль не требуется."));
    SetWindowTextW(status,LIT("Подготовка…"));SendMessageW(progress,0x402,0,0);
    SetWindowTextW(go,removing?LIT("Удаление…"):LIT("Установка…"));EnableWindow(go,0);
    SetWindowTextW(cancel,LIT("Отмена"));EnableWindow(cancel,1);
    expanded=0;SetWindowTextW(logButton,LIT("Скрыть лог"));
    SetWindowTextW(details,LIT("Подробный журнал появится после запуска проверки."));
}
static void refresh_log(void){
    if(read_text(logPath,buffer,22000,1)==0)read_text(fallbackLog,buffer,22000,1);
    if(buffer[0]){SetWindowTextW(details,buffer);SendMessageW(details,0xB1,(WPARAM)-1,(LPARAM)-1);SendMessageW(details,0xB7,0,0);}
}
static void start_job(void){
    if(phase==0&&!removing)GetWindowTextW(pathEdit,target,1200);
    if(!removing&&(wlen(target)<5||wlen(target)>180||target[1]!=':'||target[2]!='\\')){
        MessageBoxW(window,LIT("Выберите отдельную локальную папку приложения. Максимальная длина пути — 180 символов."),LIT("Папка установки"),0x30);return;
    }
    if(!work[0]&&!makework()){MessageBoxW(window,LIT("Не удалось создать временную папку установщика."),LIT("Ошибка"),0x10);return;}
    join(scratch,1600,work,LIT("result.txt"));DeleteFileW(scratch);join(scratch,1600,work,LIT("cancel.request"));DeleteFileW(scratch);join(scratch,1600,work,LIT("status.txt"));DeleteFileW(scratch);
    join(script,1600,work,removing?LIT("UninstallGui.ps1"):LIT("GuiInstall.ps1"));
#ifndef REMOVE_ONLY
    if(!removing){
        join(payloadPath,1600,work,LIT("payload.zip"));
        if(!write_bytes(payloadPath,embedded_payload,sizeof(embedded_payload))||!write_bytes(script,embedded_script,sizeof(embedded_script))){process_error(LIT("Не удалось подготовить встроенный пакет. Проверьте доступ к временной папке."));return;}
    }
#endif
    GetEnvironmentVariableW(LIT("SystemRoot"),systemPS,1200);wcat(systemPS,1400,LIT("\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"));
    cmd[0]=0;quote(cmd,7000,systemPS);wcat(cmd,7000,LIT(" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "));quote(cmd,7000,script);
    wcat(cmd,7000,LIT(" -Target "));quote(cmd,7000,target);wcat(cmd,7000,LIT(" -Work "));quote(cmd,7000,work);
    if(!removing){
        wcat(cmd,7000,LIT(" -PayloadZip "));quote(cmd,7000,payloadPath);
        wcat(cmd,7000,SendMessageW(desk,0xF0,0,0)==1?LIT(" -DesktopShortcut 1"):LIT(" -DesktopShortcut 0"));
        wcat(cmd,7000,SendMessageW(autostart,0xF0,0,0)==1?LIT(" -AutoStart 1"):LIT(" -AutoStart 0"));
        wcat(cmd,7000,SendMessageW(importOld,0xF0,0,0)==1?LIT(" -ImportLegacy 1"):LIT(" -ImportLegacy 0"));
    }
    STARTUPINFOW si;PROCESS_INFORMATION pi;SECURITY_ATTRIBUTES sa;
    zero(&si,sizeof(si));zero(&pi,sizeof(pi));zero(&sa,sizeof(sa));si.cb=sizeof(si);sa.length=sizeof(sa);sa.inherit=1;
    join(fallbackLog,1400,work,LIT("process.log"));
    HANDLE output=CreateFileW(fallbackLog,0x40000000,7,&sa,2,0x80,0);
    HANDLE input=CreateFileW(LIT("NUL"),0x80000000,3,&sa,3,0x80,0);
    if(output==INVALID_HANDLE||input==INVALID_HANDLE){if(output!=INVALID_HANDLE)CloseHandle(output);if(input!=INVALID_HANDLE)CloseHandle(input);process_error(LIT("Не удалось открыть журнал установки."));return;}
    si.flags=0x101;si.show=0;si.output=output;si.error=output;si.input=input;
    show_progress();
    BOOL ok=CreateProcessW(systemPS,cmd,0,0,1,0x08000000,0,work,&si,&pi);
    CloseHandle(output);CloseHandle(input);
    if(!ok){process_error(LIT("Windows не запустила установочный сценарий. Проверьте доступ и политику PowerShell. Не отключайте системную защиту; на управляемом компьютере обратитесь к администратору."));return;}
    CloseHandle(pi.thread);process=pi.process;SetTimer(window,1,250,0);
}
static void job_finished(void){
    KillTimer(window,1);GetExitCodeProcess(process,&processExit);CloseHandle(process);process=0;refresh_log();
    join(scratch,1600,work,LIT("result.txt"));read_text(scratch,buffer,22000,0);
    int nl=0;while(buffer[nl]&&buffer[nl]!='\n')nl++;
    if(buffer[nl])buffer[nl++]=0;
    if(!lstrcmpW(buffer,LIT("SUCCESS"))&&processExit==0){
        phase=2;successful=1;SetWindowTextW(logButton,LIT("Журнал"));SetWindowTextW(title,removing?LIT("Приложение удалено"):LIT("Готово — приложение установлено"));
        SetWindowTextW(subtitle,removing?LIT("Настройки и аудиодрайвер сохранены."):LIT("Установка и запуск теперь являются отдельными этапами."));
        SetWindowTextW(status,LIT("Все этапы успешно завершены."));SendMessageW(progress,0x402,100,0);
        SetWindowTextW(resultbox,removing?LIT("Настройки, сохранённый вход, VB-CABLE и правила брандмауэра не удалялись."):LIT("Среда Python проверена, ярлыки созданы.\r\n\r\nЗакройте старую Station Bridge перед передачей звука: аудиопорты общие. Панель Advanced использует отдельный порт.\r\n\r\nВиртуальный выход VB-CABLE можно установить из настроек приложения."));
        ShowWindow(details,0);ShowWindow(resultbox,5);if(!removing){ShowWindow(launch,5);SendMessageW(launch,0xF1,1,0);}
        SetWindowTextW(go,LIT("Готово"));EnableWindow(go,1);ShowWindow(cancel,0);return;
    }
    if(!lstrcmpW(buffer,LIT("CANCELLED"))){
        process_error(buffer+nl);SetWindowTextW(title,removing?LIT("Удаление отменено"):LIT("Установка отменена"));SetWindowTextW(status,LIT("Готовые компоненты сохранены. Можно повторить установку."));return;
    }
    if(!lstrcmpW(buffer,LIT("ERROR"))&&buffer[nl])process_error(buffer+nl);
    else process_error(LIT("Установочный сценарий не завершился штатно. Откройте журнал: он содержит точную причину. Это не означает автоматически ошибку интернета. Приложение не запускалось."));
    ShowWindow(details,0);
}
static void ask_cancel(void){
    if(phase==1&&process){
        if(cancelled)return;
        if(MessageBoxW(window,LIT("Отменить установку после текущего безопасного шага?\r\n\r\nТекущая загрузка или установка библиотеки сначала завершится. Уже загруженные компоненты останутся для повторного запуска."),LIT("Отмена"),0x24)==6){
            join(scratch,1600,work,LIT("cancel.request"));write_bytes(scratch,"cancel",6);cancelled=1;EnableWindow(cancel,0);SetWindowTextW(cancel,LIT("Отмена…"));
        }
        return;
    }
    DestroyWindow(window);
}
static void browse_folder(void){
    static WCHAR display[1024],chosen[1024];BROWSEINFOW bi;zero(&bi,sizeof(bi));
    bi.owner=window;bi.name=display;bi.title=LIT("Выберите папку. При необходимости будет добавлена YandexStationAdvanced.");bi.flags=0x41;
    void *pidl=SHBrowseForFolderW(&bi);
    if(pidl){if(SHGetPathFromIDListW(pidl,chosen)){
        int i=wlen(chosen);while(i&&chosen[i-1]!='\\')i--;
        if(lstrcmpiW(chosen+i,LIT("YandexStationAdvanced")))wcat(chosen,1024,LIT("\\YandexStationAdvanced"));
        SetWindowTextW(pathEdit,chosen);
    }CoTaskMemFree(pidl);}
}
static void build_controls(void){
    title=control(LIT("STATIC"),removing?LIT("Удаление Yandex Station Advanced"):LIT("Yandex Station Advanced"),0,0,28,24,728,34);setfont(title,large);
    subtitle=control(LIT("STATIC"),removing?LIT("Удаляются только файлы приложения и частный Python."):LIT("Установка для текущего пользователя Windows · 2.0.0-preview.5"),0,0,30,68,730,32);
    pathLabel=control(LIT("STATIC"),LIT("Папка приложения"),0,0,30,124,710,22);
    pathEdit=control(LIT("EDIT"),target,TAB|0x80, C_PATH,30,150,611,31);SendMessageW(pathEdit,0xC5,180,0);
    browse=control(LIT("BUTTON"),LIT("Обзор…"),TAB,C_BROWSE,652,149,118,33);
    desk=control(LIT("BUTTON"),LIT("Создать ярлык на рабочем столе"),TAB|3,C_DESKTOP,30,211,720,28);SendMessageW(desk,0xF1,1,0);
    autostart=control(LIT("BUTTON"),LIT("Запускать значок приложения при входе в Windows"),TAB|3,C_AUTO,30,249,720,28);
    importOld=control(LIT("BUTTON"),LIT("Перенести настройки и сохранённый вход из Station Bridge"),TAB|3,C_IMPORT,30,287,736,28);
    /* Keep a previously enabled autostart choice. Never enable it on a new install. */
    SHGetFolderPathW(0,7,0,0,scratch);wcat(scratch,1600,LIT("\\Yandex Station Advanced.lnk"));if(exists(scratch))SendMessageW(autostart,0xF1,1,0);
    GetEnvironmentVariableW(LIT("LOCALAPPDATA"),scratch,1200);wcat(scratch,1600,LIT("\\YandexStationBridge\\settings.json"));if(!exists(scratch))EnableWindow(importOld,0);
    explain=control(LIT("STATIC"),removing?LIT("Настройки, сохранённый вход и отдельный драйвер VB-CABLE будут оставлены. Другие приложения не завершаются принудительно."):LIT("Уже установленные библиотеки будут проверены и использованы повторно. При первой установке Python и компоненты загружаются с python.org и PyPI.\r\n\r\nVB-CABLE — отдельный драйвер. Его оригинальный мастер запускается из настроек приложения, только после вашего согласия."),0,0,30,343,730,126);
    footnote=control(LIT("STATIC"),LIT("Завершите Advanced перед обновлением. Старая Station Bridge не удаляется.\r\nЗапись звука не включается установщиком или автоматически при входе в Windows."),0,0,30,480,730,62);setfont(footnote,small);
    status=control(LIT("STATIC"),LIT("Подготовка…"),0,0,30,124,736,42);
    progress=control(LIT("msctls_progress32"),LIT(""),0,0,30,173,738,18);SendMessageW(progress,0x406,0,100);
    details=control(LIT("EDIT"),LIT(""),0x4|0x40|0x800|0x200000,0,30,222,738,306);setfont(details,mono);
    resultbox=control(LIT("EDIT"),LIT(""),0x4|0x800|0x200000,0,30,222,738,235);
    launch=control(LIT("BUTTON"),LIT("Запустить Yandex Station Advanced"),TAB|3,C_LAUNCH,30,479,720,28);
    line=control(LIT("STATIC"),LIT(""),0x10,0,0,552,800,2);
    versionlabel=control(LIT("STATIC"),LIT("Неофициальный проект · Windows x64"),0,0,30,578,324,22);setfont(versionlabel,small);
    logButton=control(LIT("BUTTON"),LIT("Журнал"),TAB,C_LOG,378,566,110,34);
    cancel=control(LIT("BUTTON"),LIT("Отмена"),TAB,C_CANCEL,501,566,121,34);
    go=control(LIT("BUTTON"),removing?LIT("Удалить"):LIT("Установить"),TAB|1,C_GO,635,566,133,34);
    ShowWindow(status,0);ShowWindow(progress,0);ShowWindow(details,0);ShowWindow(resultbox,0);ShowWindow(launch,0);ShowWindow(logButton,0);
    if(removing){ShowWindow(pathEdit,0);ShowWindow(browse,0);ShowWindow(pathLabel,0);ShowWindow(desk,0);ShowWindow(autostart,0);ShowWindow(importOld,0);SetWindowTextW(footnote,target);}
    SetFocus(go);
}
static LRESULT proc(HWND h,UINT msg,WPARAM wp,LPARAM lp){
    if(msg==1){window=h;build_controls();return 0;}
    if(msg==0x111){int id=(int)(wp&0xffff);
        if(id==C_GO){
            if(phase==2){
                if(successful&&!removing&&SendMessageW(launch,0xF0,0,0)==1){
                    if(setupMutex){CloseHandle(setupMutex);setupMutex=0;}
                    join(scratch,1600,target,LIT("YandexStationAdvanced.exe"));
                    if(ShellExecuteW(window,LIT("open"),scratch,0,target,1)<=32){MessageBoxW(window,LIT("Приложение установлено, но Windows не выполнила запуск. Используйте ярлык после проверки доступа."),LIT("Запуск приложения"),0x30);}
                }
                DestroyWindow(window);
            }else start_job();return 0;
        }
        if(id==C_CANCEL){ask_cancel();return 0;}
        if(id==C_BROWSE){browse_folder();return 0;}
        if(id==C_LOG){
            if(phase==1){expanded=!expanded;ShowWindow(details,expanded?0:5);SetWindowTextW(logButton,expanded?LIT("Показать лог"):LIT("Скрыть лог"));}
            else {LPCWSTR p=exists(logPath)?logPath:fallbackLog;ShellExecuteW(window,LIT("open"),p,0,0,1);}return 0;
        }
    }
    if(msg==0x113&&process){
        join(scratch,1600,work,LIT("status.txt"));
        if(read_text(scratch,buffer,22000,0)>0){
            int i=0,percent=0;while(buffer[i]>='0'&&buffer[i]<='9'){percent=percent*10+buffer[i]-'0';i++;}
            if(buffer[i]=='\n'&&percent>=0&&percent<=100){SendMessageW(progress,0x402,percent,0);if(lstrcmpW(lastStatus,buffer+i+1)){wcop(lastStatus,1600,buffer+i+1);SetWindowTextW(status,lastStatus);}}
        }
        refresh_log();if(WaitForSingleObject(process,0)==0)job_finished();return 0;
    }
    if(msg==0x138){SetBkMode((HDC)wp,1);SetTextColor((HDC)wp,0x302B27);SetBkColor((HDC)wp,0xFFFFFF);return (LRESULT)white;}
    if(msg==0x10){ask_cancel();return 0;}
    if(msg==2){PostQuitMessage(0);return 0;}
    return DefWindowProcW(h,msg,wp,lp);
}
#ifdef REMOVE_ONLY
static void init_uninstall(void){
    int argc=0;LPWSTR *argv=CommandLineToArgvW(GetCommandLineW(),&argc);
    if(argc==3&&!lstrcmpW(argv[1],LIT("--remove-target"))){wcop(target,1200,argv[2]);wcop(work,1200,selfPath);parent(work);LocalFree(argv);return;}
    if(argv)LocalFree(argv);
    wcop(target,1200,selfPath);parent(target);wcop(sourceRoot,1200,target);
    join(scratch,1600,target,LIT(".ysa-owned-install"));if(!exists(scratch)){MessageBoxW(0,LIT("Папка установленного приложения не найдена."),LIT("Удаление"),0x10);ExitProcess(1);}
    if(!makework())ExitProcess(1);
    join(script,1600,sourceRoot,LIT("installer\\UninstallGui.ps1"));join(scratch,1600,work,LIT("UninstallGui.ps1"));
    if(!CopyFileW(script,scratch,0))ExitProcess(1);
    join(script,1600,work,LIT("Uninstall.exe"));if(!CopyFileW(selfPath,script,0))ExitProcess(1);
    cmd[0]=0;wcat(cmd,7000,LIT("--remove-target "));quote(cmd,7000,target);
    if(ShellExecuteW(0,LIT("open"),script,cmd,work,1)<=32)ExitProcess(1);
    ExitProcess(0);
}
#endif
void entry(void){
    instance=GetModuleHandleW(0);GetModuleFileNameW(0,selfPath,1200);CoInitializeEx(0,2);
#ifdef REMOVE_ONLY
    removing=1;init_uninstall();
#else
    GetEnvironmentVariableW(LIT("LOCALAPPDATA"),target,1000);wcat(target,1200,LIT("\\Programs\\YandexStationAdvanced"));
#endif
    setupMutex=CreateMutexW(0,0,LIT("Local\\YandexStationAdvanced.Setup"));
    if(!setupMutex||GetLastError()==183){MessageBoxW(0,LIT("Установщик уже открыт. Переключитесь на его окно."),LIT("Yandex Station Advanced"),0x40);ExitProcess(0);}
    GetEnvironmentVariableW(LIT("LOCALAPPDATA"),logPath,1200);wcat(logPath,1400,LIT("\\YandexStationAdvanced\\install.log"));
    INITCOMMONCONTROLSEX ic={sizeof(ic),0x20};InitCommonControlsEx(&ic);
    HDC dc=GetDC(0);int dpi=GetDeviceCaps(dc,88);ReleaseDC(0,dc);if(dpi>0)scale=dpi*100/96;
    int maxx=GetSystemMetrics(0)-40,maxy=GetSystemMetrics(1)-80;if(px(820)>maxx)scale=maxx*100/820;if(px(660)>maxy)scale=maxy*100/660;if(scale<75)scale=75;
    font=CreateFontW(-px(16),0,0,0,400,0,0,0,1,0,0,5,0,LIT("Segoe UI"));
    small=CreateFontW(-px(13),0,0,0,400,0,0,0,1,0,0,5,0,LIT("Segoe UI"));
    large=CreateFontW(-px(27),0,0,0,600,0,0,0,1,0,0,5,0,LIT("Segoe UI"));
    mono=CreateFontW(-px(12),0,0,0,400,0,0,0,1,0,0,5,0,LIT("Consolas"));white=CreateSolidBrush(0xFFFFFF);
    WNDCLASSW cls;zero(&cls,sizeof(cls));cls.proc=proc;cls.instance=instance;cls.background=white;cls.name=LIT("YSA.GuiSetup.2");cls.cursor=LoadCursorW(0,(LPCWSTR)(UINT_PTR)32512);cls.icon=LoadIconW(instance,(LPCWSTR)(UINT_PTR)1);
    if(!RegisterClassW(&cls))ExitProcess(1);
    RECT r={0,0,px(800),px(622)};DWORD style=0x00CA0000;AdjustWindowRectEx(&r,style,0,0);
    int w=r.right-r.left,ht=r.bottom-r.top;
    window=CreateWindowExW(0,cls.name,removing?LIT("Удаление · Yandex Station Advanced"):LIT("Установка · Yandex Station Advanced"),style,(GetSystemMetrics(0)-w)/2,(GetSystemMetrics(1)-ht)/2,w,ht,0,0,instance,0);
    if(!window)ExitProcess(1);ShowWindow(window,5);UpdateWindow(window);
    MSG msg;while(GetMessageW(&msg,0,0,0)>0){if(!IsDialogMessageW(window,&msg)){TranslateMessage(&msg);DispatchMessageW(&msg);}}
    if(setupMutex)CloseHandle(setupMutex);DeleteObject(font);DeleteObject(small);DeleteObject(large);DeleteObject(mono);DeleteObject(white);CoUninitialize();ExitProcess(0);
}
