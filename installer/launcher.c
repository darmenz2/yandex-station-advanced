#include "winmini.h"
static WCHAR root[1024],exe[1200],entrypath[1200],cmd[4096];
void entry(void){
    HANDLE setup=OpenMutexW(0x00100000,0,LIT("Local\\YandexStationAdvanced.Setup"));
    if(setup){CloseHandle(setup);MessageBoxW(0,LIT("Сначала завершите установку или обновление приложения."),LIT("Yandex Station Advanced"),0x40);ExitProcess(0);}
    GetModuleFileNameW(0,root,1024);parent(root);
    join(exe,1200,root,LIT(".runtime\\pythonw.exe"));
    join(entrypath,1200,root,LIT("desktop_launcher.pyw"));
    if(!exists(exe)||!exists(entrypath)){
        MessageBoxW(0,LIT("Среда приложения ещё не готова. Повторно запустите графический установщик в эту же папку. Настройки удалять не нужно."),LIT("Yandex Station Advanced"),0x10);
        ExitProcess(1);
    }
    wcop(cmd,4096,LIT("\""));wcat(cmd,4096,exe);wcat(cmd,4096,LIT("\" \""));wcat(cmd,4096,entrypath);wcat(cmd,4096,LIT("\""));
    int argc=0;LPWSTR *argv=CommandLineToArgvW(GetCommandLineW(),&argc);
    if(argc>1&&!lstrcmpW(argv[1],LIT("--tray")))wcat(cmd,4096,LIT(" --tray"));
    if(argv)LocalFree(argv);
    STARTUPINFOW si;PROCESS_INFORMATION pi;zero(&si,sizeof(si));zero(&pi,sizeof(pi));si.cb=sizeof(si);
    if(!CreateProcessW(exe,cmd,0,0,0,0x08000000,0,root,&si,&pi)){
        MessageBoxW(0,LIT("Windows не запустила приложение. Проверьте доступ к папке установки и журнал desktop.log. Не отключайте защиту Windows."),LIT("Yandex Station Advanced"),0x10);ExitProcess(1);
    }
    CloseHandle(pi.thread);CloseHandle(pi.process);ExitProcess(0);
}
