@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ===== detect local proxy (Clash/v2rayN etc.) =====
set PROXY=
for /f %%i in ('powershell -NoProfile -Command "$ports=7890,7897,10809,10808,1080,33210; foreach($p in $ports){try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',$p);if($c.Connected){$c.Close();Write-Output $p;break}}catch{}}"') do set PROXY=http://127.0.0.1:%%i

if defined PROXY (
    echo [i] Using local proxy: !PROXY!
    set "CURLARGS=-x !PROXY!"
) else (
    echo [i] No local proxy detected, trying direct connection...
    set "CURLARGS="
)

rem ===== get GitHub token from Windows credential manager =====
> "%TEMP%\ghcred.txt" echo protocol=https
>> "%TEMP%\ghcred.txt" echo host=github.com
>> "%TEMP%\ghcred.txt" echo.
set TOKEN=
for /f "tokens=2 delims==" %%t in ('git credential fill ^< "%TEMP%\ghcred.txt" 2^>nul ^| findstr /b "password="') do set TOKEN=%%t
del "%TEMP%\ghcred.txt" >nul 2>&1
if not defined TOKEN (
    echo [x] No GitHub credential found. Run once: git push and login in the popup.
    pause
    exit /b 1
)

set API=https://api.github.com

rem ===== create repo (422 = already exists, ok) =====
echo [i] Creating repo XinChengP/Free-Dmde if missing...
curl -s !CURLARGS! -H "Authorization: token !TOKEN!" -H "Accept: application/vnd.github+json" ^
  %API%/user/repos -d "{\"name\":\"freedmde\",\"description\":\"DMDE free-edition batch recovery: per-folder recovery with original relative paths, GUI, resume, folder filter, trial mode\",\"private\":false}" -o "%TEMP%\repocreate.json"
powershell -NoProfile -Command "$j=Get-Content '%TEMP%\repocreate.json' -Raw | ConvertFrom-Json; if($j.html_url){Write-Output ('[i] repo: '+$j.html_url)} elseif($j.message){Write-Output ('[i] api: '+$j.message)}"

rem ===== push =====
git remote get-url origin >nul 2>&1
if %errorlevel% neq 0 git remote add origin https://github.com/XinChengP/Free-Dmde.git

if defined PROXY (
    git -c http.proxy=!PROXY! push -u origin main
) else (
    git push -u origin main
)
if %errorlevel% equ 0 (
    echo.
    echo [OK] Published: https://github.com/XinChengP/Free-Dmde
) else (
    echo.
    echo [x] Push failed. Turn on your proxy/VPN and run this again.
)
pause
