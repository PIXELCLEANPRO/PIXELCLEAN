; Script de Inno Setup para "PixelClean"
; Empaqueta la build de PyInstaller (dist\PixelClean) en un instalador standalone.

#define MyAppName "PixelClean"
#define MyAppVersion "1.12"
#define MyAppExeName "PixelClean.exe"
#define MyAppPublisher "PixelClean"

[Setup]
AppId={{8F2C9E1A-4B6D-4E1F-9C3A-2D7B5F0A61C4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=PixelClean_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; Sin admin: se instala solo para el usuario actual, en su carpeta de perfil.
; Esto es lo que permite que la auto-actualizacion sea 100% silenciosa --
; con PrivilegesRequired=admin, Windows siempre muestra el dialogo de UAC
; (incluso con /VERYSILENT) y eso es lo que trababa la actualizacion automatica.
PrivilegesRequired=lowest
CloseApplications=force
RestartApplications=no

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; GroupDescription: "Accesos directos:"; Flags: unchecked

[Files]
Source: "dist\PixelClean\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir {#MyAppName}"; Flags: nowait postinstall
