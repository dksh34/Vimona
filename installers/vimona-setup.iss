; Inno Setup script for Vimona-Setup.exe

[Setup]
AppId={{7B6E8149-53D2-4F55-96E1-9A3A0E4629B1}
AppName=Vimona
AppVersion=1.0.0
AppPublisher=Vimona Project
DefaultDirName={localappdata}\Programs\Vimona
DefaultGroupName=Vimona
UninstallDisplayIcon={app}\Vimona.exe
SetupIconFile=..\assets\vimona.ico
OutputDir=..\dist
OutputBaseFilename=Vimona-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Vimona\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Vimona"; Filename: "{app}\Vimona.exe"; IconFilename: "{app}\assets\vimona.ico"
Name: "{autodesktop}\Vimona"; Filename: "{app}\Vimona.exe"; Tasks: desktopicon; IconFilename: "{app}\assets\vimona.ico"

[Run]
Filename: "{app}\Vimona.exe"; Description: "{cm:LaunchProgram,Vimona}"; Flags: nowait postinstall skipifsilent
