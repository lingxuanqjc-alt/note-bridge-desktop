Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!ifndef STAGE
  !error "STAGE must point to the PyInstaller output directory"
!endif
!ifndef OUTPUT
  !error "OUTPUT must be an absolute installer path"
!endif
!ifndef DELETE_MANIFEST
  !error "DELETE_MANIFEST must be generated from the exact packaged files"
!endif

Name "笔记互迁 ${VERSION}"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\NoteBridge"
InstallDirRegKey HKCU "Software\NoteBridge" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "笔记互迁 · 正式版"
!define MUI_ABORTWARNING
!define MUI_ICON "..\assets\note-bridge.ico"
!define MUI_UNICON "..\assets\note-bridge.ico"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

VIProductVersion "${VERSION}.0"
VIFileVersion "${VERSION}.0"
VIAddVersionKey /LANG=${LANG_SIMPCHINESE} "ProductName" "笔记互迁"
VIAddVersionKey /LANG=${LANG_SIMPCHINESE} "FileDescription" "笔记互迁安装程序"
VIAddVersionKey /LANG=${LANG_SIMPCHINESE} "FileVersion" "${VERSION}.0"
VIAddVersionKey /LANG=${LANG_SIMPCHINESE} "ProductVersion" "${VERSION}.0"
VIAddVersionKey /LANG=${LANG_SIMPCHINESE} "LegalCopyright" "Copyright (c) 2026 Note Bridge contributors"

Function .onInit
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "笔记互迁支持 Windows 10 或 Windows 11。" /SD IDOK
    SetErrorLevel 10
    Quit
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "当前安装包需要 64 位 Windows。" /SD IDOK
    SetErrorLevel 11
    Quit
  ${EndIf}
  SetRegView 32
  ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} $0 == ""
  ${OrIf} $0 == "0.0.0.0"
    ReadRegStr $0 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
  ${If} $0 == ""
  ${OrIf} $0 == "0.0.0.0"
    MessageBox MB_YESNO|MB_ICONINFORMATION "需要 Microsoft Edge WebView2 Runtime。是否打开微软官方下载页面？安装运行时后，请重新运行本安装程序。" /SD IDNO IDNO runtime_missing
    ExecShell "open" "https://developer.microsoft.com/microsoft-edge/webview2/"
    runtime_missing:
    SetErrorLevel 12
    Quit
  ${EndIf}
FunctionEnd

Section "笔记互迁"
  SetShellVarContext current
  ; File writes abort on a running executable; no process is forcibly terminated.
  SetOverwrite on
  SetOutPath "$INSTDIR"
  File /r "${STAGE}\*"
  FileOpen $0 "$INSTDIR\.note-bridge-install" w
  FileWrite $0 "NoteBridge ${VERSION}$\r$\n"
  FileClose $0
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\NoteBridge" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge" "DisplayName" "笔记互迁"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge" "NoRepair" 1
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/NoShortcut" $1
  ${If} ${Errors}
    IfFileExists "$DESKTOP\笔记互迁.lnk" desktop_exists 0
      CreateShortcut "$DESKTOP\笔记互迁.lnk" "$INSTDIR\NoteBridge.exe" "" "$INSTDIR\NoteBridge.exe" 0
      WriteINIStr "$INSTDIR\.note-bridge-shortcuts" "Owned" "Desktop" "1"
    desktop_exists:
    CreateDirectory "$SMPROGRAMS\笔记互迁"
    IfFileExists "$SMPROGRAMS\笔记互迁\笔记互迁.lnk" start_exists 0
      CreateShortcut "$SMPROGRAMS\笔记互迁\笔记互迁.lnk" "$INSTDIR\NoteBridge.exe"
      WriteINIStr "$INSTDIR\.note-bridge-shortcuts" "Owned" "Start" "1"
    start_exists:
    IfFileExists "$SMPROGRAMS\笔记互迁\卸载.lnk" uninstall_shortcut_exists 0
      CreateShortcut "$SMPROGRAMS\笔记互迁\卸载.lnk" "$INSTDIR\Uninstall.exe"
      WriteINIStr "$INSTDIR\.note-bridge-shortcuts" "Owned" "Uninstall" "1"
    uninstall_shortcut_exists:
  ${EndIf}
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  IfFileExists "$INSTDIR\.note-bridge-install" +4 0
    MessageBox MB_ICONSTOP "未找到本程序的安装标记，卸载已停止。" /SD IDOK
    SetErrorLevel 20
    Quit
  ; The generated allowlist deletes installed files individually, never user exports.
  !include "${DELETE_MANIFEST}"
  ReadINIStr $0 "$INSTDIR\.note-bridge-shortcuts" "Owned" "Desktop"
  ${If} $0 == "1"
    Delete "$DESKTOP\笔记互迁.lnk"
  ${EndIf}
  ReadINIStr $0 "$INSTDIR\.note-bridge-shortcuts" "Owned" "Start"
  ${If} $0 == "1"
    Delete "$SMPROGRAMS\笔记互迁\笔记互迁.lnk"
  ${EndIf}
  ReadINIStr $0 "$INSTDIR\.note-bridge-shortcuts" "Owned" "Uninstall"
  ${If} $0 == "1"
    Delete "$SMPROGRAMS\笔记互迁\卸载.lnk"
  ${EndIf}
  IfFileExists "$INSTDIR\.note-bridge-shortcuts" 0 +2
    RMDir "$SMPROGRAMS\笔记互迁"
  Delete "$INSTDIR\.note-bridge-shortcuts"
  Delete "$INSTDIR\.note-bridge-install"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ReadRegStr $0 HKCU "Software\NoteBridge" "InstallDir"
  ${If} $0 == "$INSTDIR"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NoteBridge"
    DeleteRegKey HKCU "Software\NoteBridge"
  ${EndIf}
  ; User data in LocalAppData\NoteBridge and all exported files are retained.
SectionEnd
