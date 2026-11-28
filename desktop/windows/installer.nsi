; Per-user installer: the program goes to %LOCALAPPDATA%\Programs\Splice;
; projects, media cache, settings and the browser profile live in
; %LOCALAPPDATA%\Splice and are kept on uninstall.
!ifndef VERSION
  !error "Pass /DVERSION=<version> (build.py reads it from package.json)"
!endif

Unicode True
Name "Splice"
OutFile "Splice-Setup-x64.exe"
InstallDir "$LOCALAPPDATA\Programs\Splice"
RequestExecutionLevel user
SetCompressor zlib
ShowInstDetails show
ShowUninstDetails show

Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Function .onInit
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Splice"
FunctionEnd

Function un.onInit
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Splice"
FunctionEnd

Section "Splice"
  ; Replace an earlier version completely so no stale runtime files remain.
  RMDir /r "$INSTDIR\Application"
  SetOutPath "$INSTDIR\Application"
  File /r "payload\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\Splice"
  CreateShortcut "$SMPROGRAMS\Splice\Splice.lnk" "$INSTDIR\Application\Splice.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "DisplayName" "Splice"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "DisplayIcon" "$INSTDIR\Application\Splice.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "NoRepair" 1
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR\Application"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\Splice\Splice.lnk"
  RMDir "$SMPROGRAMS\Splice"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice"
SectionEnd
