#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Rutas del preflight (¿qué falta para funcionar?)
# ═══════════════════════════════════════════════════════════════
# ✅ Fase 1 del plan de contenedores (`docs/PLAN_CONTENEDORES.md` §3.1: "el preflight que
# pregunta… una pantalla/CLI que pregunte y guíe"). Esta es la mitad de API: la interfaz
# puede mostrar qué falta y con qué comando se resuelve, sin que el usuario abra una consola.
#
# Convención del módulo (igual que `routes_hwfit.py`): NO es fatal — si algo falla, se
# devuelve JSON estructurado con el error en vez de propagar la excepción (una pantalla que
# no carga no debe romper la aplicación).
import asyncio
import time
from typing import Any, Dict

from src.interfaces.web.routes import (
    router,
    _log_web_event,
    _log_error,
    logger,
)


@router.get("/preflight")
async def preflight_estado() -> Dict[str, Any]:
    """¿Qué le falta a este equipo para que AXIOMA pueda responder?

    Devuelve el veredicto, los requisitos uno por uno (con qué hacer si faltan) y qué
    modelos se bajarían. Las sondas tocan red y hardware, así que corren en un hilo aparte
    para no bloquear el bucle de la interfaz.
    """
    start = time.time()
    _log_web_event("API", "GET /api/v1/preflight")
    try:
        from src.core.preflight import descargas_pendientes, revisar
        pre = await asyncio.to_thread(revisar)
        datos = pre.como_dict()
        datos["a_descargar"] = [
            {"modelo": r.datos.get("modelo"), "rol": r.clave,
             "obligatorio": bool(r.datos.get("obligatorio")),
             "disco_gb": r.datos.get("disco_gb")}
            for r in descargas_pendientes(pre, incluir_opcionales=True)
        ]
        datos["duracion_ms"] = round((time.time() - start) * 1000, 1)
        _log_web_event("API", "GET /api/v1/preflight",
                       extra={"puede_responder": datos.get("puede_responder"),
                              "faltantes": datos.get("faltantes")})
        return datos
    except Exception as exc:  # noqa: BLE001 — el endpoint nunca tumba la aplicación
        _log_error(exc, "routes_preflight.preflight_estado")
        return {
            "puede_responder": False,
            "veredicto": "No se pudo revisar el entorno (ver el registro).",
            "faltantes": ["preflight"],
            "avisos": [],
            "requisitos": [],
            "a_descargar": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


@router.get("/preflight/catalogo")
async def preflight_catalogo() -> Dict[str, Any]:
    """Catálogo de modelos por rol: qué hay que instalar, cuánto ocupa y qué se rompe si falta."""
    start = time.time()
    _log_web_event("API", "GET /api/v1/preflight/catalogo")
    try:
        from config.model_catalog import cargar_catalogo
        catalogo = await asyncio.to_thread(cargar_catalogo)
        return {
            "version": catalogo.version,
            "por_rol": dict(catalogo.por_rol),
            "hardware": {
                # Memoria por niveles (no "cumple o no cumple"): ver el catálogo
                "ram_para_funcionar_gb": catalogo.hardware.ram_para_funcionar_gb,
                "ram_bien_gb": catalogo.hardware.ram_bien_gb,
                "ram_ideal_gb": catalogo.hardware.ram_ideal_gb,
                "ram_margen_gb": catalogo.hardware.ram_margen_gb,
                "disco_factor": catalogo.hardware.disco_factor,
                "restricciones": dict(catalogo.hardware.restricciones),
                "nota": catalogo.hardware.nota,
            },
            "modelos": [
                {
                    "nombre": m.nombre, "roles": list(m.roles), "obligatorio": m.obligatorio,
                    "tipo": m.tipo, "disco_gb": m.disco_gb, "ram_gb": m.ram_gb,
                    "para": m.para, "si_falta": m.si_falta, "instalar": m.instalar,
                }
                for m in catalogo.modelos.values()
            ],
            "espacio_obligatorios_gb": catalogo.espacio_para_descargar_gb(
                catalogo.obligatorios()),
            "duracion_ms": round((time.time() - start) * 1000, 1),
        }
    except Exception as exc:  # noqa: BLE001
        _log_error(exc, "routes_preflight.preflight_catalogo")
        return {"por_rol": {}, "modelos": [], "error": f"{type(exc).__name__}: {exc}"}


# ═══════════════════════════════════════════════════════════════
# DESCARGA GUIADA: arrancar, ver el avance y DETENER
# ═══════════════════════════════════════════════════════════════
# Pedido del usuario: instalar los modelos "con un clic y dejarlos listos para usar". La descarga
# corre en un hilo (no bloquea la pantalla), informa el avance real de Ollama y se puede cortar.

_TRABAJOS: Dict[str, Any] = {}
_MAX_TRABAJOS = 5          # se conservan los últimos para poder consultar cómo terminaron


def _guardar_trabajo(trabajo: Any) -> None:
    _TRABAJOS[trabajo.identificador] = trabajo
    while len(_TRABAJOS) > _MAX_TRABAJOS:
        _TRABAJOS.pop(next(iter(_TRABAJOS)))


@router.post("/preflight/instalar")
async def preflight_instalar(incluir_opcionales: bool = False) -> Dict[str, Any]:
    """Arranca la descarga de los modelos que faltan (en segundo plano) y devuelve su avance.

    Por defecto baja **sólo los obligatorios** (chat y código); con `incluir_opcionales=true` entran
    también el respaldo de código, la visión y la voz (pesan GB de más).
    """
    _log_web_event("API", f"POST /api/v1/preflight/instalar (opcionales={incluir_opcionales})")
    try:
        from src.core.preflight import TrabajoDeDescarga, revisar
        from src.llm.client import OllamaClient

        pre = await asyncio.to_thread(revisar)
        if not pre.faltantes() and not incluir_opcionales:
            return {"identificador": None, "estado": "nada-pendiente",
                    "detalle": "No falta ningún modelo obligatorio.",
                    "requisitos": pre.como_dict()["requisitos"]}
        trabajo = TrabajoDeDescarga(pre, OllamaClient(), incluir_opcionales=incluir_opcionales)
        _guardar_trabajo(trabajo)
        trabajo.iniciar()
        _log_web_event("API", "POST /api/v1/preflight/instalar",
                       extra={"identificador": trabajo.identificador,
                              "modelos": [m["modelo"] for m in trabajo.estado()["modelos"]]})
        return trabajo.estado()
    except Exception as exc:  # noqa: BLE001 — el endpoint nunca tumba la aplicación
        _log_error(exc, "routes_preflight.preflight_instalar")
        return {"identificador": None, "estado": "error", "error": f"{type(exc).__name__}: {exc}"}


@router.get("/preflight/instalar/{identificador}")
async def preflight_avance(identificador: str) -> Dict[str, Any]:
    """Avance de una descarga: por modelo, porcentaje real y si se puede detener."""
    trabajo = _TRABAJOS.get(identificador)
    if trabajo is None:
        return {"identificador": identificador, "estado": "desconocido",
                "detalle": "No hay ninguna descarga con ese identificador."}
    return trabajo.estado()


@router.post("/preflight/instalar/{identificador}/detener")
async def preflight_detener(identificador: str) -> Dict[str, Any]:
    """Corta la descarga (cierra la conexión con Ollama: se corta de verdad)."""
    trabajo = _TRABAJOS.get(identificador)
    if trabajo is None:
        return {"identificador": identificador, "estado": "desconocido"}
    cerradas = await asyncio.to_thread(trabajo.detener)
    _log_web_event("API", f"POST /api/v1/preflight/instalar/{identificador}/detener",
                   extra={"conexiones_cerradas": cerradas})
    return trabajo.estado()


@router.post("/preflight/claves")
async def preflight_guardar_claves(datos: Dict[str, str]) -> Dict[str, Any]:
    """Guarda las claves que manda el usuario en el `.env` (sólo las que el proyecto usa).

    Devuelve **los nombres** guardados, nunca los valores. Las claves son del usuario: no viajan en
    la imagen y quedan en un archivo con permisos sólo para él (`0600`).
    """
    _log_web_event("API", "POST /api/v1/preflight/claves")
    try:
        from src.core.apikeys import CLAVES, guardar_valores, valores_configurados

        conocidas = {c.variable for c in CLAVES}
        a_guardar = {k: (v or "") for k, v in (datos or {}).items() if k in conocidas}
        ignoradas = sorted(set(datos or {}) - conocidas)
        if a_guardar:
            await asyncio.to_thread(guardar_valores, a_guardar)
        return {
            "guardadas": sorted(a_guardar),
            "ignoradas": ignoradas,          # no se inventan claves que el proyecto no lee
            "estado": [{"variable": c.variable,
                        "configurada": valores_configurados().get(c.variable, False)}
                       for c in CLAVES],
            "detalle": "Las claves quedaron en el .env del proyecto (permisos sólo para tu usuario).",
        }
    except Exception as exc:  # noqa: BLE001
        _log_error(exc, "routes_preflight.preflight_guardar_claves")
        return {"guardadas": [], "error": f"{type(exc).__name__}: {exc}"}
