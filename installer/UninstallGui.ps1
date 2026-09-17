# The native uninstaller copies itself and this script to a temporary directory.
# User settings, saved login, driver and firewall rules are deliberately preserved.
param([Parameter(Mandatory=$true)][string]$Target,[Parameter(Mandatory=$true)][string]$Work)
$ErrorActionPreference='Stop'
$Utf8=New-Object Text.UTF8Encoding($false)
function Result([string]$kind,[string]$text){[IO.File]::WriteAllText((Join-Path $Work 'result.txt'),($kind+"`n"+$text),$Utf8)}
function Status([int]$n,[string]$text){[IO.File]::WriteAllText((Join-Path $Work 'status.txt'),([string]$n+"`n"+$text),$Utf8)}
try {
  $Target=[IO.Path]::GetFullPath($Target).TrimEnd('\')
  if($Target -notmatch '^[A-Za-z]:\\' -or $Target.Length -lt 5){throw 'Неверная папка удаления.'}
  $marker=Join-Path $Target '.ysa-owned-install'
  if(-not (Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne 'YandexStationAdvanced-per-user'){throw 'Метка установленного приложения не найдена. Ничего не удалено.'}
  try {$m=[Threading.Mutex]::OpenExisting('Local\YandexStationAdvanced.Desktop')}
  catch [Threading.WaitHandleCannotBeOpenedException] {$m=$null}
  if($m){$m.Dispose();throw 'Завершите приложение через значок рядом с часами и повторите удаление.'}
  Status 10 'Проверка списка файлов…'
  $manifest=Get-Content -LiteralPath (Join-Path $Target 'PAYLOAD_MANIFEST.json') -Raw | ConvertFrom-Json
  if(-not $manifest.files -or $manifest.files.Count -gt 1000){throw 'Некорректный список файлов. Автоматическое удаление остановлено.'}
  $checked=@()
  foreach($entry in $manifest.files){
    if($entry.path -match '(^[\\/]|\.\.|:|["<>|*?])' -or $entry.path -like '.runtime*'){throw 'Некорректный путь в списке удаления.'}
    $p=[IO.Path]::GetFullPath((Join-Path $Target $entry.path))
    if(-not $p.StartsWith($Target+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Путь вне папки приложения.'}
    $checked+=$p
  }
  # Refuse junctions anywhere in the installation, rather than following them.
  $nodes=@(Get-Item -LiteralPath $Target -Force)+@(Get-ChildItem -LiteralPath $Target -Recurse -Force)
  if(@($nodes|Where-Object{$_.Attributes -band [IO.FileAttributes]::ReparsePoint}).Count){throw 'В папке приложения обнаружена файловая ссылка. Удаление остановлено для защиты внешних файлов.'}
  if(Test-Path -LiteralPath (Join-Path $Work 'cancel.request')){throw [OperationCanceledException]::new('Удаление отменено.')}
  Status 35 'Удаление файлов приложения и частного Python…'
  $runtime=Join-Path $Target '.runtime'
  if(Test-Path -LiteralPath $runtime){Remove-Item -LiteralPath $runtime -Recurse -Force}
  foreach($path in $checked){Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue}
  foreach($extra in @('PAYLOAD_MANIFEST.json','.ysa-owned-install')){Remove-Item -LiteralPath (Join-Path $Target $extra) -Force -ErrorAction SilentlyContinue}
  Status 80 'Удаление ярлыков…'
  foreach($loc in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Startup'))){Remove-Item -LiteralPath (Join-Path $loc 'Yandex Station Advanced.lnk') -Force -ErrorAction SilentlyContinue}
  $menu=Join-Path ([Environment]::GetFolderPath('Programs')) 'Yandex Station Advanced'
  foreach($name in @('Yandex Station Advanced.lnk','Удалить приложение.lnk')){Remove-Item -LiteralPath (Join-Path $menu $name) -Force -ErrorAction SilentlyContinue}
  if(Test-Path $menu){if(-not @(Get-ChildItem -LiteralPath $menu -Force).Count){Remove-Item -LiteralPath $menu}}
  $reg='HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\YandexStationAdvanced'
  if(Test-Path $reg){$installed=(Get-ItemProperty -Path $reg).InstallLocation;if($installed -eq $Target){Remove-Item -Path $reg -Recurse -Force}}
  Get-ChildItem -LiteralPath $Target -Directory -Recurse -Force | Sort-Object { $_.FullName.Length } -Descending | ForEach-Object {
    if(-not @(Get-ChildItem -LiteralPath $_.FullName -Force).Count){Remove-Item -LiteralPath $_.FullName -Force}
  }
  if(-not @(Get-ChildItem -LiteralPath $Target -Force).Count){Remove-Item -LiteralPath $Target}
  Status 100 'Приложение удалено.'
  Result 'SUCCESS' 'Приложение удалено. Настройки, сохранённый вход, VB-CABLE и правила брандмауэра оставлены без изменений.'
  exit 0
} catch [OperationCanceledException]{Result 'CANCELLED' $_.Exception.Message;exit 2}
catch{Result 'ERROR' $_.Exception.Message;exit 1}
