#define AppName "SNOW Reminder"
#define AppVersion "1.0.4"
#define AppExe "SNOW Reminder.exe"

[Setup]
AppId={{97025D05-5A75-4FA4-B963-09E53E82D1B3}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=SNOW Reminder
DefaultDirName={localappdata}\Programs\SNOW Reminder
DefaultGroupName=SNOW Reminder
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=SNOW Reminder Setup
UninstallDisplayIcon={app}\{#AppExe}
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no

[Files]
Source: "build\release\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{userstartup}\SNOW Reminder.lnk"

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\SNOW Reminder"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SNOW Reminder"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start SNOW Reminder in the notification area"; Flags: postinstall nowait skipifsilent
