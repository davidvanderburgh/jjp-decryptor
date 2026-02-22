; JJP Asset Decryptor — Inno Setup Script
; Compile with: ISCC.exe /DAppVersion=1.0.0 /DPythonDir=build\python /DProjectDir=.. jjp_decryptor.iss
; Or use build.ps1 which handles everything automatically.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#ifndef ProjectDir
  #define ProjectDir ".."
#endif

#ifndef PythonDir
  #define PythonDir "build\python"
#endif

[Setup]
AppId={{E8A3B2F1-5C4D-4E6F-9A8B-1C2D3E4F5A6B}
AppName=JJP Asset Decryptor
AppVersion={#AppVersion}
AppVerName=JJP Asset Decryptor v{#AppVersion}
AppPublisher=David Vanderburgh
AppPublisherURL=https://github.com/davidvanderburgh/jjp-decryptor
AppSupportURL=https://github.com/davidvanderburgh/jjp-decryptor/issues
DefaultDirName={autopf}\JJP Asset Decryptor
DefaultGroupName=JJP Asset Decryptor
OutputBaseFilename=JJP_Asset_Decryptor_Setup_v{#AppVersion}
SetupIconFile={#ProjectDir}\jjp_decryptor\icon.ico
UninstallDisplayIcon={app}\jjp_decryptor\icon.ico
LicenseFile={#ProjectDir}\LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
WizardSizePercent=110
DisableProgramGroupPage=auto
VersionInfoVersion={#AppVersion}.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"
Name: "runprereqs"; Description: "Install prerequisites after setup (WSL2, gcc, partclone, xorriso, usbipd-win)"; GroupDescription: "Prerequisites:"; Flags: unchecked

[Files]
; Bundled Python with tkinter
Source: "{#PythonDir}\*"; DestDir: "{app}\python"; Flags: recursesubdirs ignoreversion

; Application package
Source: "{#ProjectDir}\jjp_decryptor\__init__.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\__main__.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\app.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\gui.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\pipeline.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\config.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\resources.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\wsl.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\updater.py"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion
Source: "{#ProjectDir}\jjp_decryptor\icon.ico"; DestDir: "{app}\jjp_decryptor"; Flags: ignoreversion

; Entry point and launcher
Source: "{#ProjectDir}\JJP Asset Decryptor.pyw"; DestDir: "{app}"; Flags: ignoreversion
Source: "launcher.vbs"; DestDir: "{app}"; Flags: ignoreversion

; Prerequisites installer (can be re-run from Start Menu)
Source: "install_prerequisites.ps1"; DestDir: "{app}"; Flags: ignoreversion

; Documentation
Source: "{#ProjectDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectDir}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start Menu
Name: "{group}\JJP Asset Decryptor"; Filename: "wscript.exe"; Parameters: """{app}\launcher.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\jjp_decryptor\icon.ico"; Comment: "Decrypt and modify JJP pinball game assets"
Name: "{group}\Install Prerequisites"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install_prerequisites.ps1"""; WorkingDir: "{app}"; Comment: "Install WSL2, gcc, partclone, xorriso, usbipd-win"
Name: "{group}\{cm:UninstallProgram,JJP Asset Decryptor}"; Filename: "{uninstallexe}"

; Desktop shortcut (optional)
Name: "{autodesktop}\JJP Asset Decryptor"; Filename: "wscript.exe"; Parameters: """{app}\launcher.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\jjp_decryptor\icon.ico"; Tasks: desktopicon; Comment: "Decrypt and modify JJP pinball game assets"

[Run]
; Run prerequisites installer if the user checked the box
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install_prerequisites.ps1"""; WorkingDir: "{app}"; StatusMsg: "Installing prerequisites..."; Flags: runascurrentuser shellexec waituntilterminated; Tasks: runprereqs

; Offer to launch the app after install
Filename: "wscript.exe"; Parameters: """{app}\launcher.vbs"""; WorkingDir: "{app}"; Description: "Launch JJP Asset Decryptor"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up Python bytecode cache
Type: filesandordirs; Name: "{app}\jjp_decryptor\__pycache__"

[Code]
function InitializeSetup(): Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  if Version.Major < 10 then
  begin
    MsgBox('JJP Asset Decryptor requires Windows 10 or later.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  Result := True;
end;
