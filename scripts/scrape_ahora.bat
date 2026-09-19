@echo off
rem Para correr el scrape a mano con doble clic: muestra el progreso y, al
rem terminar, espera una tecla para que alcances a leer el resumen.
rem La tarea de las 7 usa run_scrape.bat directo, sin esta pausa: corre sin
rem ventana y se quedaría esperando una tecla para siempre.

call "%~dp0run_scrape.bat"
echo.
echo Todo queda guardado tambien en data\cron.log
pause
