#!/usr/bin/env python3
"""Verifica las fuentes de scraping: HTTP, latencia y valor extraído (2026-11-08).

Sirve para responder "¿es confiable?" antes de usarla en el chat:

    venv_axioma/bin/python tools/verificar_scraping.py
    venv_axioma/bin/python tools/verificar_scraping.py --pais argentina --timeout 10

Código de salida: 0 si todas las fuentes probadas devolvieron un valor; 4 si alguna falló.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def main() -> int:
    from src.handlers.scraping_fuentes import buscar, cargar_fuentes, limpiar_cache

    parser = argparse.ArgumentParser(description="Verificar fuentes de scraping de AXIOMA")
    parser.add_argument("--pais", default="", help="sólo este país (por defecto: todos)")
    parser.add_argument("--timeout", type=float, default=5.0, help="segundos por fuente")
    args = parser.parse_args()

    fuentes = cargar_fuentes()
    if not fuentes:
        print("  No hay fuentes declaradas en config/fuentes_scraping.yaml")
        return 1
    paises = [args.pais] if args.pais else sorted({f.pais for f in fuentes})
    limpiar_cache()
    fallos = 0
    print(f"  Fuentes declaradas: {len(fuentes)} · países: {', '.join(paises)}")
    for pais in paises:
        inicio = time.time()
        dato = buscar(pais, timeout=args.timeout)
        ms = (time.time() - inicio) * 1000
        if dato:
            print(f"  ✅ {pais}: {dato['valor']} · {dato['fuente']} · {ms:.0f} ms")
        else:
            print(f"  ❌ {pais}: ninguna fuente devolvió un número · {ms:.0f} ms")
            fallos += 1
    print(f"\n  Resultado: {len(paises) - fallos} de {len(paises)} con dato")
    return 0 if fallos == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
