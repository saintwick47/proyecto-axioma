#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Pantalla "Configurar AXIOMA" (Fase 1 del plan de contenedores)
# ═══════════════════════════════════════════════════════════════
# Pedido del usuario (2026-10-04): *"implementar una función la cual escanee qué necesita y le
# pregunte al usuario si lo quiere instalar, además explicar que sin ese repo o programa no
# funcionaría AXIOMA. También se podría agregar una forma de instalar los modelos con un clic y
# dejarlos listos para usar."*
#
# Qué muestra:
#   1. **Estado del entorno** (el preflight): cada requisito con ✅/⚠️/❌, para qué sirve y qué se
#      pierde si falta.
#   2. **Descargar con un clic** los modelos que faltan (por defecto los obligatorios), con el avance
#      real de Ollama y un botón **DETENER** que corta la descarga de verdad.
#   3. **Tus claves de API** (son personales y no viajan en la imagen): se cargan acá y quedan en el
#      `.env` del proyecto con permisos sólo para tu usuario.
#
# Reglas que cumple (R7): la descarga corre en un trabajo de fondo que informa avance; el refresco de
# la pantalla es un temporizador que **se apaga solo** cuando el trabajo termina (nada de sondeo
# permanente). Nunca se muestran los valores de las claves.
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional
from pathlib import Path

from nicegui import ui

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

_ICONO = {"ok": "✅", "aviso": "⚠️", "falta": "❌"}


def _modelos_faltantes(pre: Any) -> List[Any]:
    """Requisitos de modelos que no están (con el nombre del modelo en los datos)."""
    return [r for r in pre.requisitos if r.datos.get("modelo") and r.estado != "ok"]


def resumen_para_pantalla(pre: Any) -> List[dict]:
    """Convierte el preflight en filas simples (para la pantalla y para las pruebas)."""
    return [
        {
            "icono": _ICONO.get(r.estado, "?"),
            "titulo": r.titulo,
            "detalle": r.detalle,
            "remedio": r.remedio if r.estado != "ok" else "",
            "estado": r.estado,
        }
        for r in pre.requisitos
    ]


def _marcador(ruta: Optional[Any] = None) -> Any:
    """Archivo que recuerda que el primer arranque ya se mostró (vive en `data/`, así persiste)."""
    from config.paths import Paths
    return Path(ruta) if ruta else Paths.DATA / ".primer_arranque_listo"


def es_primer_arranque(ruta: Optional[Any] = None) -> bool:
    """¿Es la primera vez que AXIOMA arranca en este equipo? (para guiar al usuario).

    Pedido del usuario: *"el primer arranque guiado: que la pantalla de configuración se abra sola y
    explique todo la primera vez"*. Se marca en `data/` (volumen), así se muestra UNA sola vez.
    """
    return not _marcador(ruta).exists()


def marcar_primer_arranque(ruta: Optional[Any] = None) -> None:
    """Deja constancia de que ya se mostró la guía (no vuelve a abrirse sola)."""
    try:
        archivo = _marcador(ruta)
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_text("La guía de primer arranque ya se mostró.\n", encoding="utf-8")
    except OSError as e:
        log_degraded(logger, e, "configurar_axioma.marcar_primer_arranque")


def abrir_configurar_axioma(primer_arranque: Optional[bool] = None) -> None:
    """Abre la pantalla. Se llama desde el botón del encabezado (o sola, en el primer arranque)."""
    if primer_arranque is None:
        primer_arranque = es_primer_arranque()
    estado: dict = {"trabajo": None, "temporizador": None}

    with ui.dialog() as dialogo, ui.card().classes('w-[46rem] max-w-full'):
        ui.label('🧩 Configurar AXIOMA').classes('text-lg font-bold')
        if primer_arranque:
            ui.label('👋 ¡Es la primera vez que arranca AXIOMA en este equipo! Acá te muestro qué '
                     'falta y podés instalarlo con un clic, sin usar la terminal.'
                     ).classes('text-sm bg-blue-50 p-2 rounded')
        ui.label('Qué le falta a este equipo para funcionar, y qué podés instalar ahora.'
                 ).classes('text-xs opacity-70')
        cuerpo = ui.column().classes('w-full gap-1')
        avance = ui.row().classes('w-full items-center gap-2')
        claves_zona = ui.column().classes('w-full gap-1')
        with ui.row().classes('w-full justify-end'):
            ui.button('Cerrar', on_click=dialogo.close).props('flat dense')

    # ── estado del entorno + descarga ────────────────────────────────────────
    async def refrescar() -> None:
        from src.core.preflight import revisar
        cuerpo.clear()
        avance.clear()
        with cuerpo:
            ui.label('Revisando…').classes('text-sm opacity-70')
        pre = await asyncio.to_thread(revisar)

        cuerpo.clear()
        with cuerpo:
            for fila in resumen_para_pantalla(pre):
                with ui.row().classes('items-start gap-2 w-full'):
                    ui.label(fila["icono"])
                    with ui.column().classes('gap-0'):
                        ui.label(f"{fila['titulo']}: {fila['detalle']}").classes('text-sm')
                        if fila["remedio"]:
                            ui.label(f"→ {fila['remedio']}").classes('text-xs opacity-70')
            ui.separator()
            ui.label('AXIOMA ' + ('puede responder.' if pre.puede_responder()
                                  else 'todavía NO puede responder.')).classes('text-sm font-bold')

        faltantes = _modelos_faltantes(pre)
        avance.clear()
        with avance:
            if not faltantes:
                ui.label('No falta ningún modelo.').classes('text-sm')
                return
            modelos = ', '.join(str(r.datos.get('modelo')) for r in faltantes)
            ui.label(f'Faltan {len(faltantes)}: {modelos}').classes('text-sm')

            def arrancar() -> None:
                from src.core.preflight import TrabajoDeDescarga
                from src.llm.client import OllamaClient
                estado["trabajo"] = TrabajoDeDescarga(pre, OllamaClient()).iniciar()
                boton_detener.set_visibility(True)      # mientras dura, se puede cortar
                temporizador.activate()                 # y recién ahí late (R7)

            def actualizar() -> None:
                trabajo = estado["trabajo"]
                if trabajo is None:
                    return
                info = trabajo.estado()
                etiqueta.set_text(f"{trabajo.resumen()} · {info['segundos']}s")
                if not info["puede_detenerse"]:
                    # El trabajo terminó: el temporizador se APAGA (nada de latido permanente).
                    temporizador.deactivate()
                    boton_detener.set_visibility(False)
                    if info["estado"] == "terminado":
                        ui.notify('✅ Listo: los modelos quedaron instalados', type='positive')

            def detener() -> None:
                trabajo = estado["trabajo"]
                if trabajo is not None:
                    trabajo.detener()
                    ui.notify('⏹️ Descarga detenida', type='warning')

            ui.button('⬇️ Descargar los que faltan', on_click=arrancar).props('dense color=primary')
            boton_detener = ui.button('⏹️ DETENER', on_click=detener).props('dense flat')
            boton_detener.set_visibility(False)
            etiqueta = ui.label('').classes('text-xs opacity-70')
            # Nace APAGADO: sólo late mientras hay una descarga en curso (R7: sin sondeo eterno).
            temporizador = ui.timer(1.0, actualizar, active=False)

    # ── claves del usuario ───────────────────────────────────────────────────
    def construir_claves() -> None:
        from src.core.apikeys import CLAVES, valores_configurados

        puesto = valores_configurados()
        entradas: dict = {}
        campos: dict = {}

        claves_zona.clear()
        with claves_zona:
            ui.separator()
            ui.label('🔑 Tus claves de API (son tuyas: no viajan en la imagen del proyecto)'
                     ).classes('text-sm font-bold')
            ui.label('Enter para saltear una. Si falta una clave, se pierde lo que dice al lado.'
                     ).classes('text-xs opacity-70')
            for clave in CLAVES:
                with ui.row().classes('items-center gap-2 w-full'):
                    marca = '✅' if puesto.get(clave.variable) else '—'
                    ui.label(f'{marca} {clave.para}').classes('text-xs w-[16rem]')
                    entradas[clave.variable] = ui.input(
                        placeholder=clave.variable).props('dense type=password').classes('flex-1')
                    campos[clave.variable] = clave

            def guardar() -> None:
                from src.core.apikeys import guardar_valores
                cambios = {k: (v.value or '') for k, v in entradas.items() if (v.value or '')}
                if not cambios:
                    ui.notify('No escribiste ninguna clave', type='info')
                    return
                try:
                    guardar_valores(cambios)
                    for campo in entradas.values():
                        campo.value = ''
                    ui.notify(f'✅ Guardadas {len(cambios)} clave(s) en el .env', type='positive')
                    construir_claves()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[configurar] no se pudieron guardar las claves: {exc}")
                    ui.notify(f'No se pudieron guardar: {exc}', type='negative')

            ui.button('Guardar claves', on_click=guardar).props('dense')

    construir_claves()
    dialogo.open()
    ui.timer(0.05, refrescar, once=True)
