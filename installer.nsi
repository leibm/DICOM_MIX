; DICOM MIX Tools V3.0 安装脚本
Unicode true
!include "MUI2.nsh"

Name "DICOM MIX Tools V3.0"
OutFile "dist\DICOM_MIX_Tools_V3.0_Setup.exe"
InstallDir "$PROGRAMFILES64\DICOM_MIX_Tools"
InstallDirRegKey HKCU "Software\DICOM_MIX_Tools" "InstallDir"
RequestExecutionLevel admin

; 界面
!define MUI_ABORTWARNING
!define MUI_ICON "assets\logo.ico"
!define MUI_UNICON "assets\logo.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"

Section "Install"
    SetOutPath "$INSTDIR"
    File /r "dist\DICOM_MIX_Tools\*"

    ; 创建开始菜单快捷方式
    CreateDirectory "$SMPROGRAMS\DICOM MIX Tools"
    CreateShortcut "$SMPROGRAMS\DICOM MIX Tools\DICOM MIX Tools.lnk" "$INSTDIR\DICOM_MIX_Tools.exe" "" "$INSTDIR\DICOM_MIX_Tools.exe" 0
    CreateShortcut "$SMPROGRAMS\DICOM MIX Tools\卸载.lnk" "$INSTDIR\uninstall.exe"

    ; 创建桌面快捷方式
    CreateShortcut "$DESKTOP\DICOM MIX Tools.lnk" "$INSTDIR\DICOM_MIX_Tools.exe" "" "$INSTDIR\DICOM_MIX_Tools.exe" 0

    ; 写入注册表
    WriteRegStr HKCU "Software\DICOM_MIX_Tools" "InstallDir" "$INSTDIR"
    WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
    Delete "$INSTDIR\uninstall.exe"
    RMDir /r "$INSTDIR"
    Delete "$DESKTOP\DICOM MIX Tools.lnk"
    Delete "$SMPROGRAMS\DICOM MIX Tools\DICOM MIX Tools.lnk"
    Delete "$SMPROGRAMS\DICOM MIX Tools\卸载.lnk"
    RMDir "$SMPROGRAMS\DICOM MIX Tools"
    DeleteRegKey HKCU "Software\DICOM_MIX_Tools"
SectionEnd
