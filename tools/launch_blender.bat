@echo off
chcp 65001 > nul
rem ===========================================================================
rem  ワンポチツール: アドオン読み込み済みの Blender を起動する
rem  （Blender の設定フォルダを汚さずに開発版アドオンを読み込みます）
rem
rem  使い方:
rem    launch_blender.bat                      ... 空シーンで起動
rem    launch_blender.bat "D:\a\Crate_01.blend" ... 指定の .blend を開いて起動
rem
rem  BLENDER_EXE 環境変数を設定しておくと、そのパスを優先します。
rem ===========================================================================
setlocal

set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP=%SCRIPT_DIR%dcc_blender\bootstrap_addon.py"

if defined BLENDER_EXE goto :found
set "BLENDER_EXE=C:\Users\%USERNAME%\Documents\Blender\stable\blender-5.2.0-lts.fbe6228777e7\blender.exe"
if exist "%BLENDER_EXE%" goto :found

rem ポータブル配置を探す（Program Files にインストールされていない構成に対応）
for /d %%D in ("%USERPROFILE%\Documents\Blender\stable\blender-*") do (
    if exist "%%D\blender.exe" set "BLENDER_EXE=%%D\blender.exe"
)
if exist "%BLENDER_EXE%" goto :found

rem 通常インストールも探す
for /d %%D in ("%ProgramFiles%\Blender Foundation\Blender *") do (
    if exist "%%D\blender.exe" set "BLENDER_EXE=%%D\blender.exe"
)
if exist "%BLENDER_EXE%" goto :found

echo [エラー] blender.exe が見つかりません。
echo         環境変数 BLENDER_EXE に blender.exe のフルパスを設定してください。
exit /b 1

:found
echo [ワンポチ] Blender: %BLENDER_EXE%
echo [ワンポチ] アドオン: %BOOTSTRAP%
if "%~1"=="" (
    "%BLENDER_EXE%" --python "%BOOTSTRAP%"
) else (
    "%BLENDER_EXE%" "%~1" --python "%BOOTSTRAP%"
)
endlocal
