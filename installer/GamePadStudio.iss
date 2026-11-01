#define AppVersion "2.2.0"
[Setup]
AppId={{A430C3D2-9278-48A9-BA24-15C59D0A0120}
AppName=GamePad Studio
AppVersion={#AppVersion}
AppPublisher=GamePad Studio Project
DefaultDirName={localappdata}\Programs\GamePadStudio
DefaultGroupName=GamePad Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=yes
WizardStyle=modern
OutputDir=..\artifacts
OutputBaseFilename=GamePadStudio-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\GamePadStudio.exe
SetupIconFile=..\gamepadstudio\assets\studio.ico
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "zhcn"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startup"; Description: "登录 Windows 后自动启动后台映射"; GroupDescription: "后台运行："; Flags: unchecked
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
; The recursive copy below is what delivers gamepad output: it carries
; _internal\gamepadstudio\native\ViGEmClient.dll (the ViGEm user-mode client, which
; the ViGEmBus driver package does NOT ship) plus its MIT notice in licenses\.
; Do not narrow this wildcard or add Excludes for *.dll - that silently disables
; the virtual XInput pad while the UI still offers 手柄输出.
Source: "..\dist\GamePadStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Keep shortcuts created under the original product name on the current binary.
Source: "..\dist\GamePadStudio\GamePadStudio.exe"; DestDir: "{app}"; DestName: "DualSenseStudio.exe"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\GamePad Studio"; Filename: "{app}\GamePadStudio.exe"; IconFilename: "{app}\_internal\gamepadstudio\assets\studio.ico"
Name: "{autodesktop}\GamePad Studio"; Filename: "{app}\GamePadStudio.exe"; IconFilename: "{app}\_internal\gamepadstudio\assets\studio.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "DualSenseStudioAgent"; Flags: deletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "GamePadStudioAgent"; ValueData: """{app}\GamePadStudio.exe"" --agent"; Tasks: startup; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "GamePadStudioAgent"; Flags: deletevalue; Check: not WizardIsTaskSelected('startup')

[Run]
Filename: "{app}\GamePadStudio.exe"; Description: "打开 GamePad Studio"; Flags: nowait postinstall skipifsilent

[Code]
procedure StopInstalledApp();
var
  ResultCode: Integer;
  ExePath: String;
begin
  ExePath := ExpandConstant('{app}\GamePadStudio.exe');
  if FileExists(ExePath) then begin
    Exec(ExePath, '--exit-ui', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExePath, '--agent-command stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(500);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopInstalledApp();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopInstalledApp();
  Result := True;
end;
