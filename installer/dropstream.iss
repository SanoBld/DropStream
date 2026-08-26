; DropStream Windows installer (Inno Setup)
; Builds a standard configurable installer (install directory picker, start-menu shortcuts,
; optional desktop icon, optional autostart) around the PyInstaller-built portable exe.
;
; Build locally with: iscc installer\dropstream.iss
; (Requires Inno Setup 6: https://jrsoftware.org/isinfo.php)

#define MyAppName "DropStream"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "DropStream (community fork of Twitch Drops Miner by DevilXD)"
#define MyAppURL "https://github.com/DevilXD/TwitchDropsMiner"
#define MyAppExeName "DropStream.exe"

[Setup]
AppId={{B4B8B7B0-6E43-4E36-9B7B-4D2F6E1C9A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; Lets the user pick the install directory (DirPage=auto shows the page unless /DIR= was passed)
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Portable-friendly: no admin rights required, installs per-user by default
PrivilegesRequired=lowest
OutputBaseFilename=DropStream-Setup
OutputDir=Output
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\icons\pickaxe.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "portuguese"; MessagesFile: "compiler:Languages\Portuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "{cm:AutoStartProgram,{#MyAppName}}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[CustomMessages]
english.AutoStartProgram=Start %1 automatically when Windows starts
french.AutoStartProgram=Démarrer %1 automatiquement au démarrage de Windows
spanish.AutoStartProgram=Iniciar %1 automáticamente al iniciar Windows
portuguese.AutoStartProgram=Iniciar %1 automaticamente ao iniciar o Windows

[Files]
; Everything PyInstaller produced goes into {app}; adjust the source path to match the
; "Create release folder" CI step (a folder named DropStream containing the exe + manual.txt).
Source: "..\DropStream\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --tray"; \
    Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
