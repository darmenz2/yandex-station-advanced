#include "winmini.h"
static WCHAR root[2048],local[2048],exe[4096],script[4096],cmd[9000],label[260];
static void fail(LPCWSTR text){MessageBoxW(0,text,LIT("Station Bridge — возврат PCM 1.0.6"),0x10);ExitProcess(1);}
void entry(void){
    HANDLE active=OpenMutexW(0x00100000,0,LIT("Local\\YandexStationAdvanced.Desktop"));
    if(active){CloseHandle(active);fail(LIT("Сначала завершите Advanced через его значок → «Завершить». Резервный запуск не закрывает чужие программы и не меняет их настройки."));}
    DWORD len=GetModuleFileNameW(0,root,2048);if(!len||len>=2048)fail(LIT("Не удалось определить папку запуска."));parent(root);
    join(script,4096,root,LIT("recovery_start.py"));if(!exists(script))fail(LIT("Распакуйте ВЕСЬ ZIP в отдельную папку. Не запускайте EXE из архива и не переносите его отдельно от остальных файлов."));
    join(exe,4096,root,LIT(".runtime\\pythonw.exe"));
    if(!exists(exe)){
        len=GetEnvironmentVariableW(LIT("LOCALAPPDATA"),local,2048);
        if(!len||len>=2048)fail(LIT("Не найдена пользовательская папка Windows."));
        join(exe,4096,local,LIT("Programs\\YandexStationAdvanced\\.runtime\\pythonw.exe"));
    }
    if(!exists(exe)){
        if(MessageBoxW(0,LIT("Не найдена установленная среда Advanced. Выберите существующую папку .runtime, содержащую pythonw.exe. Новые библиотеки скачиваться не будут."),LIT("Python уже установлен в Advanced"),0x41)!=1)ExitProcess(0);
        CoInitializeEx(0,2);
        BROWSEINFOW bi;zero(&bi,sizeof(bi));bi.title=LIT("Выберите существующую папку .runtime с pythonw.exe");bi.flags=0x41;bi.name=label;
        void *pidl=SHBrowseForFolderW(&bi);
        if(!pidl){CoUninitialize();ExitProcess(0);}
        if(!SHGetPathFromIDListW(pidl,local)){CoTaskMemFree(pidl);CoUninitialize();fail(LIT("Выбрана не папка на диске."));}
        CoTaskMemFree(pidl);CoUninitialize();
        join(exe,4096,local,LIT("pythonw.exe"));
        if(!exists(exe))fail(LIT("В выбранной папке нет pythonw.exe. Нужна папка .runtime прежнего приложения."));
    }
    wcop(cmd,9000,LIT("\""));wcat(cmd,9000,exe);wcat(cmd,9000,LIT("\" \""));wcat(cmd,9000,script);wcat(cmd,9000,LIT("\""));
    STARTUPINFOW si;PROCESS_INFORMATION pi;zero(&si,sizeof(si));zero(&pi,sizeof(pi));si.cb=sizeof(si);
    if(!CreateProcessW(exe,cmd,0,0,0,0x08000000,0,root,&si,&pi))fail(LIT("Windows не запустила резервную версию. Не отключайте защиту Windows; проверьте выбранную папку .runtime."));
    CloseHandle(pi.thread);CloseHandle(pi.process);ExitProcess(0);
}
