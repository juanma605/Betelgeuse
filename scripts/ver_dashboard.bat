@echo off
rem Abre el dashboard con TUS datos (data\listings.db, lo último scrapeado),
rem no con el demo anonimizado de GitHub. Doble clic y se abre el navegador.
rem
rem --server.address localhost: solo se puede ver desde esta compu. Sin eso,
rem Streamlit también escucha en la red (wifi de tu casa, de la facu...) y
rem cualquiera conectado podría entrar con tu IP.
rem
rem Para apagarlo, cerrá esta ventana.

cd /d "%~dp0.."
"C:\Users\juanm\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe" -m streamlit run dashboard.py --server.address localhost
