import os

with open("todo_el_proyecto.txt", "w", encoding="utf-8") as outfile:
    for root, dirs, files in os.walk("."):
        # Ignoramos carpetas que no suman contexto
        if any(ignored in root for ignored in [".git", "__pycache__", "venv", ".venv"]):
            continue
        
        for file in files:
            if file.endswith((".py", ".yaml", ".md")):
                filepath = os.path.join(root, file)
                outfile.write(f"\n\n{'='*60}\n")
                outfile.write(f"ARCHIVO: {filepath}\n")
                outfile.write(f"{'='*60}\n\n")
                try:
                    with open(filepath, "r", encoding="utf-8") as infile:
                        outfile.write(infile.read())
                except Exception as e:
                    outfile.write(f"[Error leyendo el archivo: {e}]\n")

print("Listo! Se generó 'todo_el_proyecto.txt'")