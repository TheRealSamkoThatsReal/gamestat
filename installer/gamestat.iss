; Inno Setup script for gamestat — per-user install (no admin/UAC prompt).
; Version is passed on the command line: ISCC /DMyAppVersion=0.6.0 gamestat.iss

#define MyAppName "gamestat"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppExe "gamestat.exe"
#define MyAppPublisher "TheRealSamkoThatsReal"
#define MyAppURL "https://github.com/TheRealSamkoThatsReal/gamestat"

[Setup]
AppId={{7A2C1B90-3E5D-4F1A-9B7C-9A5E10CAFE60}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
; Per-user install: no administrator rights required.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\gamestat
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
; Paths below are resolved relative to the repo root, not this installer/ dir.
SourceDir=..
OutputDir=installer-out
OutputBaseFilename=gamestat-setup-x64
SetupIconFile=packaging\assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes

[Tasks]
Name: "addtopath"; Description: "Add gamestat to your PATH (for the command line)"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\gamestat"; Filename: "{app}\{#MyAppExe}"; Parameters: "app"; Comment: "Game library dashboard"
Name: "{autodesktop}\gamestat"; Filename: "{app}\{#MyAppExe}"; Parameters: "app"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Parameters: "app"; Description: "Launch gamestat now"; Flags: nowait postinstall skipifsilent

[Registry]
; Append install dir to the per-user PATH (HKCU under lowest privileges).
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; \
  Check: WizardIsTaskSelected('addtopath') and NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  { true only if the install dir isn't already on PATH }
  Result := Pos(';' + Lowercase(Param) + ';', ';' + Lowercase(OrigPath) + ';') = 0;
end;
