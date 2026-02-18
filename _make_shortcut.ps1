$projDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projDir = $projDir + "\"

$desktop = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("$desktop\JJP Asset Decryptor.lnk")
$sc.TargetPath = "wscript.exe"
$sc.Arguments = "`"${projDir}launch.vbs`""
$sc.WorkingDirectory = $projDir
$sc.IconLocation = "${projDir}jjp_decryptor\icon.ico"
$sc.Description = "JJP Asset Decryptor"
$sc.Save()
Write-Host "Shortcut created at: $desktop\JJP Asset Decryptor.lnk"
