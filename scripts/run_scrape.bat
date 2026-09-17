@echo off
rem Wrapper para el Programador de Tareas de Windows: corre el scrape diario
rem y deja el log en data\cron.log (se acumula; borralo de vez en cuando).
rem Usa la ruta completa del python que tiene las dependencias instaladas
rem (httpx, pandas, playwright, etc.) — el "python" del PATH puede resolver
rem a otra instalación sin nada instalado.

cd /d "%~dp0.."
"C:\Users\juanm\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe" -m inmobot scrape >> data\cron.log 2>&1
