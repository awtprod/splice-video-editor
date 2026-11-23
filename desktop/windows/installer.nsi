Unicode True
Name "Splice"
OutFile "Splice-Setup-x64.exe"
InstallDir "$LOCALAPPDATA\Programs\Splice"
RequestExecutionLevel user
SetCompressor /SOLID lzma
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
  SetOutPath "$INSTDIR\Application"
  File /r "payload\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\Splice"
  CreateShortcut "$SMPROGRAMS\Splice\Splice.lnk" "$INSTDIR\Application\Splice.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "DisplayName" "Splice"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "DisplayVersion" "0.2.0-preview.1"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice" "UninstallString" '"$INSTDIR\Uninstall.exe"'
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR\Application"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\Splice\Splice.lnk"
  RMDir "$SMPROGRAMS\Splice"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Splice"
SectionEnd
