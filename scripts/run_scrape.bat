@echo off
rem Wrapper para el Programador de Tareas de Windows: corre el scrape diario
rem y deja el log en data\cron.log (se acumula; borralo de vez en cuando).
rem Usa la ruta completa del python que tiene las dependencias instaladas
rem (httpx, pandas, playwright, etc.) — el "python" del PATH puede resolver
rem a otra instalación sin nada instalado.

cd /d "%~dp0.."

rem Si la laptop estaba suspendida, la tarea arranca apenas se despierta y el
rem wifi todavía no conectó: la primera zona falla con "getaddrinfo failed".
rem Se espera hasta 3 minutos a que resuelva DNS; si no, corre igual y lo anota.
powershell -NoProfile -Command "for ($i = 0; $i -lt 36; $i++) { try { [void][Net.Dns]::GetHostAddresses('inmuebles.mercadolibre.com.ar'); exit 0 } catch { Start-Sleep -Seconds 5 } }; exit 1"
if errorlevel 1 echo %date% %time% sin red despues de 3 minutos, corro igual>> data\cron.log

echo --- corrida %date% %time% --->> data\cron.log
"C:\Users\juanm\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe" -m inmobot scrape >> data\cron.log 2>&1
