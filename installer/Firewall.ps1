$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
  $root=Split-Path $PSScriptRoot -Parent
  $python=Join-Path $root '.runtime\pythonw.exe'
  if(-not (Test-Path -LiteralPath $python)){throw 'Private Python runtime missing.'}
  $settings=Get-Content (Join-Path $env:LOCALAPPDATA 'YandexStationAdvanced\settings.json') -Raw | ConvertFrom-Json
  $peers=@($settings.desktop_firewall_peers)
  if(-not $peers.Count -or $peers.Count -gt 16){throw 'Connect your speaker group first.'}
  function LocalIP([string]$text){
    $ip=$null
    if(-not [Net.IPAddress]::TryParse($text,[ref]$ip) -or $ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork){throw 'Invalid IP address.'}
    $b=$ip.GetAddressBytes()
    if(-not ($b[0] -eq 10 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or ($b[0] -eq 192 -and $b[1] -eq 168) -or ($b[0] -eq 169 -and $b[1] -eq 254))){throw 'Only local network addresses are allowed.'}
    return $text
  }
  $checked=@()
  foreach($p in $peers){
    $hostIp=LocalIP $p.host; $remote=LocalIP $p.station; $port=[int]$p.port
    if($port -lt 8808 -or $port -gt 8823){throw 'Unexpected audio port.'}
    $addresses=@($remote)
    # Explicit NAT approvals only, bound to this speaker and local interface.
    if($settings.audio_peer_approvals -and $settings.pins){
      foreach($entry in $settings.audio_peer_approvals.PSObject.Properties){
        $r=$entry.Value
        $pinProperty=$settings.pins.PSObject.Properties[$entry.Name]
        $pin=if($pinProperty){[string]$pinProperty.Value}else{''}
        if($pin -and [string]$r.fingerprint -eq $pin -and $r.station_ip -eq $remote -and $r.audio_host -eq $hostIp){$addresses+=LocalIP $r.peer}
      }
    }
    $checked+=@{hostIp=$hostIp;remote=$addresses;port=$port}
  }
  $text=($checked|ForEach-Object{"$($_.hostIp):$($_.port) <- $($_.remote -join ', ')"}) -join "`n"
  if([Windows.Forms.MessageBox]::Show("Create inbound audio rules for this executable, profile Private only?`n$python`n`n$text`n`nNo router ports or public network rules will be added.",'Yandex Station Advanced firewall','YesNo','Question') -ne 'Yes'){exit 0}
  Get-NetFirewallRule -Group 'YandexStationAdvanced' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  foreach($r in $checked){New-NetFirewallRule -DisplayName ("Yandex Station Advanced audio "+$r.port) -Group 'YandexStationAdvanced' -Direction Inbound -Action Allow -Profile Private -Program $python -Protocol TCP -LocalAddress $r.hostIp -LocalPort $r.port -RemoteAddress $r.remote | Out-Null}
  New-NetFirewallRule -DisplayName 'Yandex Station Advanced mDNS' -Group 'YandexStationAdvanced' -Direction Inbound -Action Allow -Profile Private -Program $python -Protocol UDP -LocalPort 5353 -RemoteAddress LocalSubnet | Out-Null
  [Windows.Forms.MessageBox]::Show('Rules created. If new speakers/NAT peers are approved later, run this helper again. Rules are not opened on Public networks.','Yandex Station Advanced') | Out-Null
}catch{[Windows.Forms.MessageBox]::Show($_.Exception.Message,'Firewall helper stopped','OK','Error') | Out-Null;exit 1}
