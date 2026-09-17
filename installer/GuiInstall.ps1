# Invoked only by the native GUI. Installs components; never starts the application.
param(
  [Parameter(Mandatory=$true)][string]$Target,
  [Parameter(Mandatory=$true)][string]$PayloadZip,
  [Parameter(Mandatory=$true)][string]$Work,
  [int]$DesktopShortcut=1,
  [int]$AutoStart=0,
  [int]$ImportLegacy=0
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$Utf8=New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding=$Utf8
$Data=Join-Path $env:LOCALAPPDATA 'YandexStationAdvanced'
$Log=Join-Path $Data 'install.log'
New-Item -ItemType Directory -Path $Data -Force | Out-Null
function Log([string]$text) { [IO.File]::AppendAllText($Log,([DateTime]::Now.ToString('HH:mm:ss')+' '+$text+"`r`n"),$Utf8) }
function Status([int]$percent,[string]$text) {
  $temp=Join-Path $Work 'status.next'
  [IO.File]::WriteAllText($temp,([string]$percent+"`n"+$text),$Utf8)
  Move-Item -LiteralPath $temp -Destination (Join-Path $Work 'status.txt') -Force
  Log $text
}
function CancelCheck {
  if(Test-Path -LiteralPath (Join-Path $Work 'cancel.request')) { throw [OperationCanceledException]::new('Установка отменена. Уже загруженные компоненты сохранены для следующего запуска.') }
}
function Result([string]$kind,[string]$text) {
  [IO.File]::WriteAllText((Join-Path $Work 'result.txt'),($kind+"`n"+$text),$Utf8)
}
function ValidateTarget([string]$path) {
  if($path -notmatch '^[A-Za-z]:\\' -or $path -match '[\r\n"<>|*?]' -or $path.Substring(2).Contains(':')) { throw 'Выберите обычную локальную папку Windows, не сетевой путь.' }
  $root=[IO.Path]::GetFullPath($path).TrimEnd('\')
  if($root.Length -lt 5 -or $root.Length -gt 180) { throw 'Выберите папку приложения с путём не длиннее 180 символов, не корень диска.' }
  foreach($p in @($env:SystemRoot,$env:USERPROFILE,$env:LOCALAPPDATA,$env:APPDATA)) {
    if($p -and $root.Equals($p.TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase)) { throw 'Нельзя устанавливать прямо в эту системную папку. Создайте отдельную папку приложения.' }
  }
  if($root.StartsWith($env:SystemRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Установка внутри папки Windows не разрешена.' }
  # Do not copy into a junction/symlink or overwrite unrelated files.
  $walk=$root
  while($walk){
    if(Test-Path -LiteralPath $walk){
      $item=Get-Item -LiteralPath $walk -Force
      if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Папка установки или её родитель является ссылкой. Выберите обычную папку.'}
    }
    $next=Split-Path -Path $walk -Parent
    if($next -eq $walk){break}; $walk=$next
  }
  if(Test-Path -LiteralPath $root){
    if(-not (Get-Item -LiteralPath $root).PSIsContainer){throw 'Выбранный путь не является папкой.'}
    $items=@(Get-ChildItem -LiteralPath $root -Force)
    if($items.Count){
      $mark=Join-Path $root '.ysa-owned-install'
      if(-not (Test-Path -LiteralPath $mark) -or (Get-Content -LiteralPath $mark -Raw).Trim() -ne 'YandexStationAdvanced-per-user'){throw 'Папка не пустая и не принадлежит этой установке. Выберите другую папку.'}
    }
  }
  return $root
}
function CheckAppStopped {
  try {$m=[Threading.Mutex]::OpenExisting('Local\YandexStationAdvanced.Desktop')}
  catch [Threading.WaitHandleCannotBeOpenedException] {return}
  if($m){$m.Dispose();throw 'Yandex Station Advanced уже работает. Выберите «Завершить» в меню значка рядом с часами, затем нажмите «Повторить» в установщике.'}
}
function Shortcut([string]$path,[string]$args) {
  $s=(New-Object -ComObject WScript.Shell).CreateShortcut($path)
  $s.TargetPath=Join-Path $Target 'YandexStationAdvanced.exe'
  $s.Arguments=$args; $s.WorkingDirectory=$Target
  $s.IconLocation=(Join-Path $Target 'assets\app.ico')+',0'
  $s.Description='Yandex Station Advanced';$s.Save()
}
function RunBootstrap {
  $psi=New-Object Diagnostics.ProcessStartInfo
  $psi.FileName=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
  $psi.Arguments='-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+(Join-Path $Target 'bootstrap.ps1')+'" -InstallOnly'
  $psi.WorkingDirectory=$Target
  $psi.UseShellExecute=$false; $psi.CreateNoWindow=$true
  $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$true
  $psi.StandardOutputEncoding=$Utf8; $psi.StandardErrorEncoding=$Utf8
  $process=New-Object Diagnostics.Process; $process.StartInfo=$psi
  try {
    if(-not $process.Start()){throw 'Не удалось запустить проверку компонентов.'}
    $outTask=$process.StandardOutput.ReadLineAsync(); $errTask=$process.StandardError.ReadLineAsync()
    $outDone=$false; $errDone=$false; $cancelShown=$false
    while(-not $process.HasExited -or -not $outDone -or -not $errDone){
      foreach($kind in @('out','err')){
        $task=if($kind -eq 'out'){$outTask}else{$errTask}
        if($null -ne $task -and $task.IsCompleted){
          $line=$task.GetAwaiter().GetResult()
          if($null -eq $line){if($kind -eq 'out'){$outDone=$true;$outTask=$null}else{$errDone=$true;$errTask=$null}}
          else {
            Log $line
            if(-not $cancelShown){
              if($line -like '*private Python runtime*'){Status 34 'Загрузка и проверка Python с python.org…'}
              elseif($line -like '*Installing pip*'){Status 44 'Подготовка менеджера компонентов…'}
              elseif($line -like '*Checking installed*'){Status 50 'Проверка уже установленных библиотек…'}
              elseif($line -like '*Installing audio,*'){Status 57 'Установка библиотек. Ход загрузки — в журнале ниже.'}
              elseif($line -like '*Downloading*'){Status 65 'Загрузка библиотек с PyPI…'}
              elseif($line -like '*Successfully installed*'){Status 80 'Библиотеки установлены. Контрольная проверка…'}
              elseif($line -like '*no downloads needed*'){Status 85 'Компоненты уже исправны. Повторная загрузка не нужна.'}
            }
            if($kind -eq 'out'){$outTask=$process.StandardOutput.ReadLineAsync()}else{$errTask=$process.StandardError.ReadLineAsync()}
          }
        }
      }
      if(-not $cancelShown -and (Test-Path -LiteralPath (Join-Path $Work 'cancel.request'))){
        $cancelShown=$true;Status 65 'Отмена запрошена. Ждём завершения текущего шага без принудительного обрыва.'
      }
      Start-Sleep -Milliseconds 60
    }
    $process.WaitForExit(); $exitCode=$process.ExitCode
    CancelCheck
    if($exitCode -ne 0){throw ('Проверка или установка компонентов завершилась с кодом '+$exitCode+'. Точная ошибка записана в install.log. Нажмите «Журнал», затем «Повторить» после исправления причины.')}
  } finally {$process.Dispose()}
}
try {
  Log ('=== GUI Setup 2.0.0-preview.5 / '+$Target+' ===')
  Status 3 'Проверка системы и выбранной папки…'
  if(-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64'){throw 'Этот установщик предназначен для Windows x64, не x86 или ARM64.'}
  $Target=ValidateTarget $Target
  CheckAppStopped; CancelCheck
  Status 8 'Проверка целостности встроенного пакета…'
  if((Get-FileHash -LiteralPath $PayloadZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne '__PAYLOAD_SHA256__'){throw 'Контрольная сумма пакета не совпала. Файлы приложения не заменены.'}
  $Stage=Join-Path $Work 'payload'
  if(Test-Path -LiteralPath $Stage){Remove-Item -LiteralPath $Stage -Recurse -Force}
  Expand-Archive -LiteralPath $PayloadZip -DestinationPath $Stage
  $manifest=Get-Content -LiteralPath (Join-Path $Stage 'PAYLOAD_MANIFEST.json') -Raw | ConvertFrom-Json
  if(-not $manifest.files -or $manifest.files.Count -gt 1000){throw 'Некорректный список файлов пакета.'}
  foreach($entry in $manifest.files){
    if($entry.path -match '(^[\\/]|\.\.|:|["<>|*?])' -or $entry.path -like '.runtime*'){throw 'Небезопасный путь в пакете.'}
    $src=[IO.Path]::GetFullPath((Join-Path $Stage $entry.path))
    if(-not $src.StartsWith($Stage+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Файл выходит за границы пакета.'}
    if((Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256){throw ('Проверка файла не пройдена: '+$entry.path)}
  }
  CancelCheck;CheckAppStopped
  Status 20 'Установка файлов приложения. Существующая .runtime сохраняется.'
  New-Item -ItemType Directory -Path $Target -Force | Out-Null
  # Mark ownership before the first file so failed installs remain safely repairable.
  [IO.File]::WriteAllText((Join-Path $Target '.ysa-owned-install'),'YandexStationAdvanced-per-user',[Text.Encoding]::ASCII)
  foreach($entry in $manifest.files){
    $dest=Join-Path $Target $entry.path
    if(Test-Path -LiteralPath $dest){if((Get-Item -LiteralPath $dest -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw ('Нельзя заменять файл-ссылку: '+$entry.path)}}
    New-Item -ItemType Directory -Path (Split-Path $dest -Parent) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Stage $entry.path) -Destination $dest -Force
  }
  Copy-Item -LiteralPath (Join-Path $Stage 'PAYLOAD_MANIFEST.json') -Destination $Target -Force
  CancelCheck;Status 30 'Подготовка частной среды Python. Приложение пока не запускается.'
  RunBootstrap
  CancelCheck;Status 90 'Ярлыки и параметры установки…'
  if(-not (Test-Path -LiteralPath (Join-Path $Target '.runtime\pythonw.exe'))){throw 'После установки отсутствует pythonw.exe. Запуск приложения отменён.'}
  $old=Join-Path $env:LOCALAPPDATA 'YandexStationBridge'
  if($ImportLegacy -eq 1 -and -not (Test-Path (Join-Path $Data 'settings.json')) -and (Test-Path (Join-Path $old 'settings.json'))){
    Copy-Item -LiteralPath (Join-Path $old 'settings.json') -Destination $Data
    if(Test-Path (Join-Path $old 'account.dpapi')){Copy-Item -LiteralPath (Join-Path $old 'account.dpapi') -Destination $Data}
    Log 'Прежние настройки скопированы; оригиналы сохранены.'
  }
  $menu=Join-Path ([Environment]::GetFolderPath('Programs')) 'Yandex Station Advanced'
  New-Item -ItemType Directory -Path $menu -Force | Out-Null
  Shortcut (Join-Path $menu 'Yandex Station Advanced.lnk') ''
  $desktopPath=Join-Path ([Environment]::GetFolderPath('Desktop')) 'Yandex Station Advanced.lnk'
  if($DesktopShortcut -eq 1){Shortcut $desktopPath ''}
  $startupPath=Join-Path ([Environment]::GetFolderPath('Startup')) 'Yandex Station Advanced.lnk'
  if($AutoStart -eq 1){Shortcut $startupPath '--tray'}else{Remove-Item -LiteralPath $startupPath -Force -ErrorAction SilentlyContinue}
  $un=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $menu 'Удалить приложение.lnk'))
  $un.TargetPath=Join-Path $Target 'Uninstall.exe';$un.WorkingDirectory=$Target;$un.Save()
  $reg='HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\YandexStationAdvanced'
  New-Item -Path $reg -Force | Out-Null
  $props=@{DisplayName='Yandex Station Advanced';DisplayVersion='2.0.0-preview.5';Publisher='Yandex Station Advanced project (unofficial)';InstallLocation=$Target;DisplayIcon=(Join-Path $Target 'assets\app.ico');UninstallString=('"'+(Join-Path $Target 'Uninstall.exe')+'"')}
  foreach($item in $props.GetEnumerator()){New-ItemProperty -Path $reg -Name $item.Key -Value $item.Value -PropertyType String -Force | Out-Null}
  foreach($key in @('NoModify','NoRepair')){New-ItemProperty -Path $reg -Name $key -Value 1 -PropertyType DWord -Force | Out-Null}
  # Remove only obsolete installer scripts from our own old package, never settings.
  foreach($name in @('Install.ps1','Uninstall.ps1')){Remove-Item -LiteralPath (Join-Path $Target ('installer\'+$name)) -Force -ErrorAction SilentlyContinue}
  Status 100 'Установка завершена. Теперь приложение можно запустить.'
  Result 'SUCCESS' 'Yandex Station Advanced установлен. Выберите «Запустить приложение» и нажмите «Готово». Драйвер VB-CABLE устанавливается отдельно в настройках.'
  exit 0
} catch [OperationCanceledException] {
  Log $_.Exception.Message;Result 'CANCELLED' $_.Exception.Message;exit 2
} catch {
  $message=$_.Exception.Message
  Log ('ERROR: '+$message);Result 'ERROR' $message;exit 1
}
