# Downloads the unmodified vendor package. No driver binaries are redistributed.
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
$work=Join-Path $env:TEMP ('YSA-VBCable-'+[guid]::NewGuid().ToString('N'))
try {
  if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
  $answer=[Windows.Forms.MessageBox]::Show('VB-CABLE is a separate VB-Audio driver with its own donationware license. Download its original installer from vb-audio.com and open the vendor installation wizard? A reboot may be required.','Yandex Station Advanced','YesNo','Question')
  if ($answer -ne 'Yes') { exit 0 }
  New-Item -ItemType Directory -Path $work | Out-Null
  $zip=Join-Path $work 'VBCABLE_Driver_Pack45.zip'
  Invoke-WebRequest -UseBasicParsing -Uri 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip' -OutFile $zip
  Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $work 'vendor')
  $setups=@(Get-ChildItem (Join-Path $work 'vendor') -Recurse -File -Filter 'VBCABLE_Setup_x64.exe')
  if ($setups.Count -ne 1) { throw 'Vendor package layout changed. Use the official vb-audio.com/Cable page. No executable was launched.' }
  $setup=$setups[0].FullName
  $signature=Get-AuthenticodeSignature -LiteralPath $setup
  if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch '(?i)(VB-Audio|Vincent Burel|VB AUDIO|VINCENT,? BUREL)') {
    throw 'The vendor installer did not pass the publisher/signature check. Installation was stopped; do not disable Windows driver protection.'
  }
  Write-Host ('Publisher: '+$signature.SignerCertificate.Subject)
  Write-Host ('Installer SHA-256: '+(Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash)
  # UAC is only for the vendor wizard, not for the streaming app.
  $p=Start-Process -FilePath $setup -WorkingDirectory $setups[0].DirectoryName -Verb RunAs -PassThru -Wait
  [Windows.Forms.MessageBox]::Show('The vendor wizard has closed. Complete any reboot it requested, then refresh Windows audio devices in Yandex Station Advanced. Select CABLE Input, bind it and use the Rename button.','Yandex Station Advanced') | Out-Null
} catch {
  [Windows.Forms.MessageBox]::Show($_.Exception.Message,'VB-CABLE installation stopped','OK','Error') | Out-Null
  exit 1
} finally {
  # Do not remove a running vendor process's working directory. -Wait above has completed.
  if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}
