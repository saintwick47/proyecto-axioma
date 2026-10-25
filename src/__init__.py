# AXIOMA Package
# Ruta: /ruta/a/axioma/src/__init__.py
# AXIOMA v0.7.5 — Package Root
# Autor: SaintWick

# ⚠️ FUENTE ÚNICA DE LA VERSIÓN: este valor es el que muestra la API
# (`/v1/health`, `/v1/status`, `FastAPI(version=...)`), el banner de `main.py`
# y el título del servidor web. No se repite en ningún otro `.py`.
__version__ = "0.7.5"
__author__ = "SaintWick"
__description__ = "Sistema Multi-Agente de IA"

__all__ = ["domain", "llm", "memory", "router", "agents", "interfaces"]
