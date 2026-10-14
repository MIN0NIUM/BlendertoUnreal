@echo off
chcp 65001 > nul
rem ===========================================================================
rem  ワンポチツール: UE 側スクリプトを Unreal プロジェクトへ配置する
rem
rem  使い方:
rem    deploy_to_ue.bat                         ... 既定のプロジェクトへ配置
rem    deploy_to_ue.bat "D:\MyProj\MyProj.uproject" ... 指定プロジェクトへ配置
rem
rem  配置先: <プロジェクト>\Content\Python\
rem  （UE はこのフォルダを Python のパスに自動で追加します）
rem ===========================================================================
setlocal

set "SCRIPT_DIR=%~dp0"
set "SRC=%SCRIPT_DIR%ue_plugin"

if "%~1"=="" (
    set "PROJECT_DIR=%SCRIPT_DIR%..\UEProject\OnePochiPlayground"
) else (
    set "PROJECT_DIR=%~dp1"
)

if not exist "%PROJECT_DIR%" (
    echo [エラー] プロジェクトフォルダが見つかりません: %PROJECT_DIR%
    exit /b 1
)

set "DEST=%PROJECT_DIR%\Content\Python"
if not exist "%DEST%" mkdir "%DEST%"

echo [ワンポチ] コピー元: %SRC%
echo [ワンポチ] コピー先: %DEST%

robocopy "%SRC%" "%DEST%" /E /PURGE /NFL /NDL /NJH /NJS /XD __pycache__ /XF *.pyc
if errorlevel 8 (
    echo [エラー] コピーに失敗しました。
    exit /b 1
)

rem プリセットも参照できるように配置する
set "PRESET_DEST=%DEST%\presets"
if not exist "%PRESET_DEST%" mkdir "%PRESET_DEST%"
robocopy "%SCRIPT_DIR%presets" "%PRESET_DEST%" /E /PURGE /NFL /NDL /NJH /NJS >nul

echo [ワンポチ] 完了。UE のコンソールで次を実行できます:
echo     py "%DEST%\run_import.py" --manifest "<onepochi.json のパス>"
endlocal
