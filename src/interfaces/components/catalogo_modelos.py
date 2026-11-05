"""🧠 Modelos: elegir, instalar, usar y borrar modelos del catálogo.

Pedidos del usuario (2026-10-31 y 2026-11-05):
  · ver la lista del catálogo (no sólo lo instalado), con para qué sirve cada uno y cuánto ocupa;
  · seguir la descarga con una barra de avance;
  · **elegir** el modelo de cada rol (el chat NO se instala solo), **cambiarlo**, y si no le
    convence **eliminarlo** y bajar otro.

Todo sale del catálogo (`config/model_catalog.yaml`): no se inventan modelos ni tamaños. La
selección del rol se guarda en el `.env` con `src/core/model_seleccion.py` (y avisa que hay que
reiniciar AXIOMA para que tome). Borrar usa `OllamaClient.delete_model`; el que está en uso no se
deja borrar (primero hay que cambiarle el rol).
"""
from __future__ import annotations

import asyncio
from typing import Any

from nicegui import ui

from config.model_catalog import cargar_catalogo
from src.core import model_seleccion


def _ollama_del_catalogo() -> list[Any]:
    """Modelos de Ollama del catálogo (los `piper` son archivos, no se bajan con `ollama pull`)."""
    return [m for m in cargar_catalogo().modelos.values() if m.tipo == "ollama"]


def _estado(modelo: Any, instalados: set) -> str:
    return "✅ instalado" if modelo.nombre in instalados else "⬇️ falta"


def _total_a_descargar(modelos: list[Any], instalados: set) -> float:
    """GB de descarga si se instala todo lo que falta (para saberlo ANTES de empezar)."""
    return round(sum(m.disco_gb for m in modelos if m.nombre not in instalados), 1)


def _roles_del_modelo(modelo: Any) -> list[str]:
    """Roles del catálogo que además se pueden asignar desde acá (chat/código/visión)."""
    return [r for r in modelo.roles if r in model_seleccion.ROLES]


def _roles_en_uso(nombre: str, actuales: dict) -> list[str]:
    return [r for r, actual in actuales.items() if actual and actual == nombre]


class _CatalogoUI:
    """Estado y acciones del diálogo (métodos cortos; el auditor marca funciones >100 líneas)."""

    def __init__(self) -> None:
        self.estado: dict = {"instalados": set(), "actuales": {}}
        self.resumen = None
        self.lista = None
        self.progreso = None

    async def cargar(self) -> None:
        from src.interfaces.web import routes_preflight as rp

        datos = await rp.preflight_modelos()
        self.estado["instalados"] = set(datos.get("instalados") or [])
        self.estado["actuales"] = model_seleccion.actuales()
        modelos = _ollama_del_catalogo()
        total = _total_a_descargar(modelos, self.estado["instalados"])
        self.resumen.set_text(f'Si instalás todo lo que falta: {total:.1f} GB de descarga'
                              if total else 'Ya está todo instalado')
        self.lista.clear()
        with self.lista:
            for m in modelos:
                self.fila(m)

    def fila(self, m: Any) -> None:
        en_uso = _roles_en_uso(m.nombre, self.estado["actuales"])
        with ui.column().classes('w-full gap-0 border-b pb-1'):
            with ui.row().classes('items-center gap-2 w-full'):
                ui.label(m.nombre).classes('text-xs font-mono w-[15rem]')
                roles = ' · '.join(model_seleccion.etiqueta(r) for r in m.roles)
                ui.label(f'[{roles}]').classes('text-xs')
                ui.label(_estado(m, self.estado["instalados"])).classes('text-xs w-[6.5rem]')
                if en_uso:
                    ui.badge('en uso: ' + ', '.join(model_seleccion.etiqueta(r) for r in en_uso),
                             color='green').classes('text-xs')
                if m.nota:
                    ui.icon('info').classes('text-xs opacity-60').tooltip(m.nota)
            with ui.row().classes('items-center gap-2 w-full'):
                ui.label(m.para).classes('text-xs opacity-70 flex-1')
                ui.label(f'{m.disco_gb:.1f} GB disco · {m.ram_gb:.1f} GB RAM').classes('text-xs')
            with ui.row().classes('items-center gap-1 w-full justify-end'):
                if m.nombre not in self.estado["instalados"]:
                    ui.button('⬇️ Instalar', on_click=lambda n=m.nombre: self.instalar(n)
                              ).props('dense')
                for rol in _roles_del_modelo(m):
                    if self.estado["actuales"].get(rol) != m.nombre:
                        ui.button(f'Usar para {model_seleccion.etiqueta(rol)}',
                                  on_click=lambda n=m.nombre, r=rol: self.usar(r, n)
                                  ).props('dense flat')
                if m.nombre in self.estado["instalados"]:
                    accion = self.aviso_en_uso if en_uso else self.confirmar_borrado
                    ui.button('🗑️ Eliminar', on_click=lambda n=m.nombre, a=accion: a(n)
                              ).props('dense flat color=negative')

    def aviso_en_uso(self, nombre: str) -> None:
        ui.notify(f'{nombre} está en uso: primero elegí otro modelo para ese rol', type='warning')

    def confirmar_borrado(self, nombre: str) -> None:
        with ui.dialog() as confirmacion, ui.card():
            ui.label(f'¿Eliminar {nombre}?').classes('text-base font-bold')
            ui.label('Se borra del disco de Ollama y libera espacio. Después lo podés volver a '
                     'instalar desde esta lista.').classes('text-xs opacity-70')
            with ui.row().classes('w-full justify-end'):
                ui.button('Cancelar', on_click=confirmacion.close).props('flat dense')
                ui.button('Sí, eliminar', on_click=lambda: self.borrar(nombre)
                          ).props('dense color=negative')
        confirmacion.open()

    async def borrar(self, nombre: str) -> None:
        from src.llm.client import OllamaClient

        try:
            await asyncio.to_thread(OllamaClient().delete_model, nombre)
            ui.notify(f'🗑️ {nombre} eliminado', type='positive')
        except Exception as exc:
            ui.notify(f'No se pudo eliminar {nombre}: {exc}', type='negative', timeout=8000)
        await self.cargar()

    async def usar(self, rol: str, nombre: str) -> None:
        try:
            model_seleccion.asignar(rol, nombre)
            ui.notify(f'✅ {model_seleccion.etiqueta(rol).capitalize()}: {nombre} · reiniciá AXIOMA '
                      'para que tome el cambio', type='positive', timeout=8000)
        except Exception as exc:
            ui.notify(f'No se pudo guardar: {exc}', type='negative', timeout=8000)
        await self.cargar()

    async def instalar(self, nombre: str) -> None:
        from src.llm.client import OllamaClient

        self.progreso.clear()
        with self.progreso:
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
        except Exception as exc:
            ui.notify(f'No se pudo instalar {nombre}: {exc}', type='negative', timeout=8000)
        self.progreso.clear()
        await self.cargar()


def abrir_catalogo_modelos() -> None:
    """Abre el diálogo con la lista, la elección de rol y los botones de instalar/borrar."""
    pantalla = _CatalogoUI()
    with ui.dialog() as dialogo, ui.card().classes('w-[58rem] max-w-full'):
        ui.label('🧠 Modelos').classes('text-lg font-bold')
        ui.label('El chat, el código y la visión son ELEGIBLES: nada se instala solo. El de '
                 'embeddings es obligatorio y AXIOMA lo administra.').classes('text-xs opacity-70')
        pantalla.resumen = ui.label('').classes('text-xs font-bold')
        pantalla.lista = ui.column().classes('w-full gap-1')
        pantalla.progreso = ui.column().classes('w-full gap-1')
        with ui.row().classes('w-full justify-end'):
            ui.button('Cerrar', on_click=dialogo.close).props('flat dense')

    ui.timer(0.05, pantalla.cargar, once=True)
    dialogo.open()
