; Uninstaller additions, included by Tauri's NSIS template (installerHooks in
; tauri.conf.json).
;
; The bundled Python writes __pycache__ folders beside its modules while the
; app runs. The uninstaller only deletes the files it installed, so those
; folders kept python\ and the install folder alive. This removes the whole
; python\ folder, which only ever holds what the app put there, then the
; install folder if that left it empty. It never removes $INSTDIR recursively,
; in case someone installed into a folder that holds other things.
;
; The WebView profile and logs in %LOCALAPPDATA%\<identifier> are left to the
; template: the uninstall page's "Delete the application data" box removes
; them, and a silent or update uninstall keeps them.

!macro NSIS_HOOK_POSTUNINSTALL
  RMDir /r /REBOOTOK "$INSTDIR\python"
  RMDir "$INSTDIR"
!macroend
