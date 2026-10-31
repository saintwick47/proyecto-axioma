"""🧠 Modelos: la lista del catálogo para ELEGIR e instalar con progreso.

Pedido del usuario (2026-10-31): en la pantalla de configuración sólo aparecían los modelos ya
instalados, sin forma de ver otras opciones según el equipo ni de seguir la descarga. Esta pantalla
lista el catálogo medido (`config/model_catalog.yaml`), dice para qué sirve cada uno, cuánto ocupa y
cuánta RAM necesita, y permite descargarlo viendo el avance.

No inventa modelos ni tamaños: todo sale del catálogo. La descarga usa `OllamaClient.pull_model`, el
mismo camino que el resto de AXIOMA (informa progreso y se puede cancelar).
"""
from __future__ import annotations

import asyncio
from typing import Any, List

from nicegui import ui

from config.model_catalog import cargar_catalogo


def _ollama_del_catalogo() -> List[Any]:
    """Modelos de Ollama del catálogo (los `piper` son archivos, no se bajan con `ollama pull`)."""
    return [m for m in cargar_catalogo().modelos.values() if m.tipo == "ollama"]


def _estado(modelo: Any, instalados: set) -> str:
    return "✅ instalado" if modelo.nombre in instalados else "⬇️ falta"


def abrir_catalogo_modelos() -> None:
    """Abre el diálogo con la lista y los botones de descarga."""
    estado: dict = {"instalados": set()}

    with ui.dialog() as dialogo, ui.card().classes('w-[54rem] max-w-full'):
        ui.label('🧠 Modelos').classes('text-lg font-bold')
        ui.label('Elegí según para qué lo querés y cuánta memoria tiene tu equipo. Lo que falte se '
                 'descarga acá, con el avance a la vista.').classes('text-xs opacity-70')
        lista = ui.column().classes('w-full gap-1')
        progreso = ui.column().classes('w-full gap-1')
        with ui.row().classes('w-full justify-end'):
            ui.button('Cerrar', on_click=dialogo.close).props('flat dense')

    async def cargar() -> None:
        from src.interfaces.web import routes_preflight as rp
        datos = await rp.preflight_modelos()
        estado["instalados"] = set(datos.get("instalados") or [])
        lista.clear()
        with lista:
            for m in _ollama_del_catalogo():
                with ui.row().classes('items-center gap-2 w-full'):
                    ui.label(m.nombre).classes('text-xs font-mono w-[15rem]')
                    ui.label(m.para).classes('text-xs opacity-70 flex-1')
                    ui.label(f'{m.disco_gb:.1f} GB · {m.ram_gb:.1f} GB RAM').classes('text-xs')
                    ui.label(_estado(m, estado["instalados"])).classes('text-xs w-[7rem]')
                    if m.nombre not in estado["instalados"]:
                        ui.button('⬇️ Instalar',
                                  on_click=lambda n=m.nombre: instalar(n)).props('dense')

    async def instalar(nombre: str) -> None:
        from src.llm.client import OllamaClient
        progreso.clear()
        with progreso:
            etiqueta = ui.label(f'Descargando {nombre}…').classes('text-xs')
            barra = ui.linear_progress(0, show_value=False).classes('w-full')

        def avance(datos: dict) -> None:
            total = datos.get('total') or 0
            hecho = datos.get('completed') or 0
            if total:
                barra.value = min(1.0, hecho / total)
                etiqueta.set_text(f'{nombre}: {hecho / 1e9:.1f} de {total / 1e9:.1f} GB')
            elif datos.get('status'):
                etiqueta.set_text(f"{nombre}: {datos['status']}")

        try:
            await asyncio.to_thread(OllamaClient().pull_model, nombre, 3600, avance)
            ui.notify(f'✅ {nombre} instalado', type='positive')
        except Exception as exc:  # noqa: BLE001 — se informa y la pantalla sigue usable
            ui.notify(f'No se pudo instalar {nombre}: {exc}', type='negative', timeout=8000)
        progreso.clear()
        await cargar()

    ui.timer(0.05, cargar, once=True)
    dialogo.open()
