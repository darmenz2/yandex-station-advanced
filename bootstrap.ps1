# Downloads an isolated, per-folder Python runtime. No system Python/PATH changes.
param([switch]$InstallOnly, [switch]$Repair)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $Root '.runtime'
$Python = Join-Path $Runtime 'python.exe'
$Site = Join-Path $Runtime 'Lib\site-packages'
$Requirements = Join-Path $Root 'requirements.txt'
$Marker = Join-Path $Runtime 'installed-requirements.sha256'
$CheckScript = Join-Path $Root 'dependency_check.py'
$Version = '3.13.15'
$PythonSha256 = 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf'

function Invoke-PrivatePython([string[]]$PythonArgs) {
    # Explicitly hide every console subprocess, not only its PowerShell parent.
    $info=New-Object Diagnostics.ProcessStartInfo
    $info.FileName=$Python
    $info.Arguments=($PythonArgs | ForEach-Object {'"'+$_+'"'}) -join ' '
    $info.WorkingDirectory=$Root
    $info.UseShellExecute=$false
    $info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true
    $info.RedirectStandardError=$true
    $encoding=New-Object Text.UTF8Encoding($false)
    $info.StandardOutputEncoding=$encoding
    $info.StandardErrorEncoding=$encoding
    $process=New-Object Diagnostics.Process
    $process.StartInfo=$info
    try {
        if(-not $process.Start()){throw 'Could not start private Python.'}
        $outTask=$process.StandardOutput.ReadLineAsync()
        $errTask=$process.StandardError.ReadLineAsync()
        $outDone=$false;$errDone=$false
        while(-not $process.HasExited -or -not $outDone -or -not $errDone){
            foreach($kind in @('out','err')){
                $task=if($kind -eq 'out'){$outTask}else{$errTask}
                if($null -ne $task -and $task.IsCompleted){
                    $line=$task.GetAwaiter().GetResult()
                    if($null -eq $line){
                        if($kind -eq 'out'){$outTask=$null;$outDone=$true}else{$errTask=$null;$errDone=$true}
                    }else{
                        Write-Host $line
                        if($kind -eq 'out'){$outTask=$process.StandardOutput.ReadLineAsync()}else{$errTask=$process.StandardError.ReadLineAsync()}
                    }
                }
            }
            Start-Sleep -Milliseconds 30
        }
        $process.WaitForExit()
        return [int]$process.ExitCode
    }finally{$process.Dispose()}
}
try {
    if (-not (Test-Path -LiteralPath $CheckScript)) {
        throw 'dependency_check.py is missing. Run the graphical Setup again.'
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'Windows x64 is required. This package is not for 32-bit Windows.'
    }
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {
        throw 'This build targets Windows x64, not ARM64.'
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Host 'Installing private Python runtime from python.org...' -ForegroundColor Cyan
        $Staging = Join-Path $Root '.runtime-installing'
        if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }
        New-Item -ItemType Directory -Path $Staging -Force | Out-Null
        $Zip = Join-Path $Staging 'python.zip'
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$Version/python-$Version-embed-amd64.zip" -OutFile $Zip -TimeoutSec 120
        if ((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $PythonSha256) {
            throw 'Python archive checksum mismatch. The downloaded archive will not be executed.'
        }
        $Expanded = Join-Path $Staging 'python'
        Expand-Archive -LiteralPath $Zip -DestinationPath $Expanded -Force
        $Signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $Expanded 'python.exe')
        if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw 'Python executable signature could not be verified. Check Windows root certificates, date and Internet connection; do not disable signature checks.'
        }
        if (Test-Path -LiteralPath $Runtime) { Remove-Item -LiteralPath $Runtime -Recurse -Force }
        Move-Item -LiteralPath $Expanded -Destination $Runtime
        Remove-Item -LiteralPath $Staging -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Site -Force | Out-Null
    # Embedded Python stays isolated; imports are explicit and relative to .runtime.
    $Pth = Join-Path $Runtime 'python313._pth'
    [IO.File]::WriteAllText($Pth, "python313.zip`r`n.`r`nLib\site-packages`r`n..`r`nimport site`r`n", [Text.Encoding]::ASCII)
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PIP_NO_INPUT = '1'
    if (-not (Test-Path -LiteralPath (Join-Path $Site 'pip\__main__.py'))) {
        Write-Host 'Installing pip from PyPI (SHA-256 verified wheel)...' -ForegroundColor Cyan
        $Meta = Invoke-RestMethod -Uri 'https://pypi.org/pypi/pip/json' -TimeoutSec 60
        $Wheel = @($Meta.urls | Where-Object { $_.packagetype -eq 'bdist_wheel' -and $_.filename -like '*-py3-none-any.whl' })[0]
        if (-not $Wheel -or $Wheel.url -notlike 'https://files.pythonhosted.org/*') { throw 'No supported pip wheel found on PyPI.' }
        $PipZip = Join-Path $Runtime 'pip-bootstrap.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $Wheel.url -OutFile $PipZip -TimeoutSec 120
        $Actual = (Get-FileHash -LiteralPath $PipZip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Actual -ne $Wheel.digests.sha256) { Remove-Item -LiteralPath $PipZip -Force; throw 'pip download checksum mismatch.' }
        Expand-Archive -LiteralPath $PipZip -DestinationPath $Site -Force
        Remove-Item -LiteralPath $PipZip -Force
    }
    $Hash = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash
    # A previous launch may have installed everything but failed before writing
    # the marker. Verify actual versions/imports first instead of downloading
    # every dependency again. Never pass Python source through native -c quoting.
    $NeedsInstall = [bool]$Repair
    if (-not $Repair) {
        Write-Host 'Checking installed components...' -ForegroundColor Cyan
        $CheckCode = Invoke-PrivatePython @('-u',$CheckScript,'--requirements',$Requirements)
        $NeedsInstall = ($CheckCode -ne 0)
    }
    if ($NeedsInstall) {
        # Do not leave an old success marker after a failed repair/upgrade.
        if (Test-Path -LiteralPath $Marker) { Remove-Item -LiteralPath $Marker -Force }
        Write-Host 'Installing audio, network and QR components from PyPI...' -ForegroundColor Cyan
        $PipArgs = @('-m','pip','install','--isolated','--index-url','https://pypi.org/simple',
            '--only-binary=:all:','--timeout','30','--retries','2','--upgrade','--no-warn-script-location','--target',$Site,'-r',$Requirements)
        if ($Repair) {
            $PipArgs += @('--force-reinstall','--no-cache-dir')
        } else {
            # Keep this runtime separate from potentially incompatible system pip caches.
            $PipArgs += @('--cache-dir',(Join-Path $Runtime 'pip-cache'))
        }
        $PipCode = Invoke-PrivatePython $PipArgs
        if ($PipCode -ne 0) { throw 'Dependency installation failed. See install.log and retry the graphical Setup.' }
        $CheckCode = Invoke-PrivatePython @('-u',$CheckScript,'--requirements',$Requirements)
        if ($CheckCode -ne 0) { throw 'Dependency self-check failed. See the specific package error above; retry the graphical Setup.' }
    } else {
        Write-Host 'Using installed components; no downloads needed.' -ForegroundColor Green
    }
    [IO.File]::WriteAllText($Marker, $Hash, [Text.Encoding]::ASCII)
    if ($InstallOnly) { Write-Host 'Installation complete.' -ForegroundColor Green; exit 0 }
    Set-Location -LiteralPath $Root
    # Installation has finished. Starting the desktop app is a separate step.
    $DesktopEntry = Join-Path $Root 'advanced_entry.py'
    if (-not (Test-Path -LiteralPath $DesktopEntry)) { throw 'advanced_entry.py is missing.' }
    Start-Process -FilePath (Join-Path $Runtime 'pythonw.exe') -ArgumentList ('"'+$DesktopEntry+'"') -WorkingDirectory $Root
    exit 0
} catch {
    Write-Host "`nERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
