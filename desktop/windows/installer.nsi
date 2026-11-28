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

; A running Splice (or its server, or an FFmpeg it started) holds its program files open,
; and replacing or deleting them would then fail half-way. A program that is running
; cannot be opened for writing, which is what InUse tests.
!macro SPLICE_RUNNING_CHECK PREFIX
; Pops a path; pushes 1 when the file exists and is in use, else 0.
Function ${PREFIX}InUse
  Exch $R0
  Push $R1
  StrCpy $R1 0
  IfFileExists $R0 0 done
  ClearErrors
  FileOpen $R1 $R0 a
  IfErrors busy
  FileClose $R1
  StrCpy $R1 0
  Goto done
  busy:
  StrCpy $R1 1
  done:
  StrCpy $R0 $R1
  Pop $R1
  Exch $R0
FunctionEnd

; Asks the user to close Splice until it is closed; quits with exit code 3 on Cancel
; (and in silent mode, where the answer is Cancel).
Function ${PREFIX}CloseSplice
  retry:
  StrCpy $0 0
  Push "$INSTDIR\Application\Splice.exe"
  Call ${PREFIX}InUse
  Pop $1
  IntOp $0 $0 | $1
  Push "$INSTDIR\Application\resources\python\python.exe"
  Call ${PREFIX}InUse
  Pop $1
  IntOp $0 $0 | $1
  Push "$INSTDIR\Application\resources\ffmpeg\ffmpeg.exe"
  Call ${PREFIX}InUse
  Pop $1
  IntOp $0 $0 | $1
  Push "$INSTDIR\Application\resources\ffmpeg\ffprobe.exe"
  Call ${PREFIX}InUse
  Pop $1
  IntOp $0 $0 | $1
  IntCmp $0 0 closed
  MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Splice is running. Close every Splice window (an export in progress stops when Splice closes), then click Retry." /SD IDCANCEL IDRETRY retry
  SetErrorLevel 3
  Quit
  closed:
FunctionEnd
!macroend
!insertmacro SPLICE_RUNNING_CHECK ""
!insertmacro SPLICE_RUNNING_CHECK "un."

Function .onInit
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Splice"
  Call CloseSplice
FunctionEnd

Function un.onInit
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Splice"
  Call un.CloseSplice
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
