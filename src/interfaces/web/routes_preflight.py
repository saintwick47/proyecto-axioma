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
import hmac
import os
import time
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from config.settings import settings
from config.acceso_web import token_configurado
from src.interfaces.web.routes import (
    router,
    _log_web_event,
    _log_error,
)


# ═══════════════════════════════════════════════════════════════
# AUTENTICACIÓN DE LAS RUTAS QUE ESCRIBEN CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════
# MEDIDO (2026-10-25): `POST /api/v1/preflight/claves` escribía el `.env` y
# respondía 200 SIN credenciales. Lo único que lo frenaba era el middleware de
# `Host` de `server.py`, que **sólo actúa en loopback**: con
# `AXIOMA_WEB_HOST=0.0.0.0` un `Host` ajeno pasaba y la escritura también.
#
# Diseño (el más simple que cierra el agujero sin romper la UI local):
#   · Token en `data/.web_token` (lo genera solo AXIOMA al exponerse; 0600),
#     enviado en el header `X-AXIOMA-Token` y comparado con `hmac.compare_digest`
#     (no se filtra por tiempo). `AXIOMA_WEB_TOKEN` (entorno o `.env`) sigue
#     siendo la opción AVANZADA para fijarlo. Ver `config/acceso_web.py`.
#   · Si hay token configurado, se EXIGE siempre (también en loopback): una
#     página local maliciosa puede llamar a 127.0.0.1 con el `Host` correcto.
#   · Si NO hay token configurado: se permite en loopback (la UI y el uso local
#     de siempre no se rompen) y se RECHAZA cuando el bind no es local. Ese es
#     el default seguro: el caso expuesto a la red nunca queda abierto.
# La verificación se aplica sólo a las rutas que ESCRIBEN archivos de
# configuración (`/preflight/claves` y `/preflight/modelos`); las de lectura
# siguen abiertas para no romper la pantalla.
TOKEN_HEADER = "X-AXIOMA-Token"
_BIND_LOCAL = {"127.0.0.1", "localhost", "::1"}


def _bind_es_local() -> bool:
    """¿El servidor escucha sólo en loopback? (misma fuente que `server._host_ligado`)."""
    host = (os.environ.get("AXIOMA_WEB_HOST") or "127.0.0.1").strip().lower()
    if host.startswith("["):            # [::1]:8080 → ::1
        host = host[1:host.index("]")] if "]" in host else host
    else:
        host = host.split(":")[0]       # 127.0.0.1:8080 → 127.0.0.1
    return host in _BIND_LOCAL


def _token_esperado() -> str:
    """Token disponible para autorizar la escritura, o cadena vacía si no hay.

    ✅ 2026-10-25 (token automático): la resolución completa vive en
    `config/acceso_web.py` y es, en orden: `AXIOMA_WEB_TOKEN` del entorno → el
    valor del `.env` (`settings.axioma_web_token`) → `data/.web_token`. El
    archivo lo genera solo el arranque de AXIOMA cuando está expuesto (o
    `python main.py --rotar-token`), así que el usuario **nunca** tiene que
    pegar un token a mano. Acá NO se genera nada durante un request: esta
    función sólo pregunta (escribir por request sería un efecto raro y, si el
    disco falla, el error aparecería en el peor lugar).
    """
    return token_configurado()


async def exigir_token_de_escritura(request: Request) -> None:
    """Dependencia de FastAPI: autoriza (o rechaza) una ruta que escribe configuración.

    401 si hay token configurado y el header falta o no coincide; 403 si no hay
    token configurado y AXIOMA no está ligado a loopback. El valor recibido no
    se registra nunca.
    """
    esperado = _token_esperado()
    if esperado:
        recibido = request.headers.get(TOKEN_HEADER, "")
        # compare_digest sobre bytes: misma comparación en tiempo constante para
        # cualquier longitud (no se corta en el primer byte distinto).
        if not hmac.compare_digest(recibido.encode("utf-8"), esperado.encode("utf-8")):
            _log_web_event("API", "POST de configuración rechazado: token inválido o ausente")
            raise HTTPException(
                status_code=401,
                detail=(f"Token inválido o ausente. Mandá el header {TOKEN_HEADER} con el código "
                        "de acceso (data/.web_token; también vale AXIOMA_WEB_TOKEN)."),
            )
        return

    if not _bind_es_local():
        _log_web_event("API", "POST de configuración rechazado: expuesto sin token configurado")
        raise HTTPException(
            status_code=403,
            detail=("Esta AXIOMA está expuesta a la red y no tiene token de acceso guardado "
                    "(el código que se genera solo). Reiniciá AXIOMA para que lo genere, o "
                    "generá uno con «python main.py --rotar-token»; después mandalo en el header "
                    f"{TOKEN_HEADER} para poder guardar configuración desde la API."),
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


@router.post("/preflight/claves", dependencies=[Depends(exigir_token_de_escritura)])
async def preflight_guardar_claves(datos: Dict[str, str]) -> Dict[str, Any]:
    """Guarda las claves que manda el usuario en el `.env` (sólo las que el proyecto usa).

    Devuelve **los nombres** guardados, nunca los valores. Las claves son del usuario: no viajan en
    la imagen y quedan en un archivo con permisos sólo para él (`0600`).

    Requiere el token de escritura (`X-AXIOMA-Token` / `AXIOMA_WEB_TOKEN`) cuando está configurado;
    sin token configurado sólo se permite si AXIOMA escucha en loopback (ver
    `exigir_token_de_escritura`).
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


# ═══════════════════════════════════════════════════════════════
# ELEGIR LOS MODELOS POR ROL (paso de la interfaz)
# ═══════════════════════════════════════════════════════════════
# Idea del proyecto (usuario, 2026-10-10): descargar AXIOMA, instalarlo, escanear el hardware, SUGERIR
# modelos y que cada persona ELIJA; lo elegido queda configurado en su `.env` y el catálogo/preflight lo
# respetan (hecho el 2026-10-10 con `modelos_efectivos`).
# Estas dos rutas son las que usa la pantalla 🧩: una para saber qué hay EN OLLAMA, otra para guardar lo
# elegido **validando que exista** (si no existe, no se escribe nada y se explica por qué).

# Rol → variable del `.env` que lo configura (los nombres son los de `.env.example`).
VARIABLES_DE_ROL: Dict[str, str] = {
    "chat": "LLM_DEFAULT_MODEL",
    "codigo": "LLM_CODE_MODEL",
    "codigo_respaldo": "LLM_CODE_MODEL_FALLBACK",
    "vision": "LLM_VISION_MODEL",
}


def _ollama_host() -> str:
    """El Ollama a consultar (el del equipo por defecto)."""
    return str(settings.ollama_host or "http://127.0.0.1:11434")


async def _modelos_en_ollama() -> list:
    """Los modelos INSTALADOS, preguntándole a Ollama. Devuelve [] si no responde (no bloquea la UI)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as cliente:
            r = await cliente.get(f"{_ollama_host()}/api/tags")
            r.raise_for_status()
            return sorted(m.get("name", "") for m in (r.json().get("models") or []) if m.get("name"))
    except Exception as exc:  # noqa: BLE001  (sin Ollama la pantalla tiene que seguir andando)
        _log_web_event("API", f"GET /preflight/modelos sin Ollama: {type(exc).__name__}")
        return []


@router.get("/preflight/modelos")
async def preflight_modelos() -> Dict[str, Any]:
    """Qué modelos hay en Ollama y cuáles están configurados hoy por rol (para poder elegir)."""
    instalados = await _modelos_en_ollama()
    # ✅ 2026-10-10: MEDIDO con la prueba visual (`tools/verificar_primer_arranque.py`, comprobación 3):
    # acá el nombre del atributo se armaba desde el ROL (`llm_codigo_model`, `llm_codigo_respaldo_model`),
    # así que esos dos roles NO existían como atributos y la pantalla los mostraba **vacíos** aunque el
    # `.env` tuviera `LLM_CODE_MODEL=qwen2.5-coder:7b`. El nombre sale de la VARIABLE, que es lo que el
    # usuario escribe y ya vive en `VARIABLES_DE_ROL` (una sola fuente de verdad).
    configurados = {rol: str(getattr(settings, variable.lower(), "") or "")
                    for rol, variable in VARIABLES_DE_ROL.items()}
    return {"instalados": instalados,
            "ollama_disponible": bool(instalados),
            "variables": VARIABLES_DE_ROL,
            "configurados": configurados,
            "detalle": ("Elegí uno de los instalados, o escribí el nombre exacto de un modelo que tengas. "
                        "Se guarda en tu `.env` y AXIOMA lo usa para ese rol.")}


@router.post("/preflight/modelos", dependencies=[Depends(exigir_token_de_escritura)])
async def preflight_guardar_modelos(datos: Dict[str, str]) -> Dict[str, Any]:
    """Guarda el modelo elegido para cada rol, VALIDANDO que exista en Ollama.

    Si un modelo no está instalado, **no se escribe nada** y se explica: escribir un nombre que no existe
    dejaría a AXIOMA sin poder responder en ese rol, y el error aparecería mucho después.

    Escribe el `.env`: requiere el token de escritura, igual que `/preflight/claves`.
    """
    _log_web_event("API", "POST /preflight/modelos")
    try:
        from src.core.apikeys import guardar_valores

        pedidos = {rol: str(valor or "").strip() for rol, valor in (datos or {}).items()
                   if rol in VARIABLES_DE_ROL and str(valor or "").strip()}
        if not pedidos:
            return {"guardados": [], "errores": {}, "detalle": "No mandaste ningún modelo para guardar."}

        instalados = await _modelos_en_ollama()
        errores = {}
        if instalados:
            errores = {rol: f"`{nombre}` no está en Ollama (instalados: {', '.join(instalados[:6])})"
                       for rol, nombre in pedidos.items() if nombre not in instalados}
        else:
            # Sin Ollama no se puede validar: se avisa, pero no se guarda a ciegas.
            return {"guardados": [], "errores": {r: "Ollama no responde: no puedo validar el modelo"
                                                 for r in pedidos},
                    "detalle": "Encendé Ollama (o el perfil `ollama-local`) y volvé a intentar."}
        a_guardar = {VARIABLES_DE_ROL[rol]: nombre for rol, nombre in pedidos.items()
                     if rol not in errores}
        aplicado = ""
        if a_guardar:
            await asyncio.to_thread(guardar_valores, a_guardar)
            # ✅ 2026-10-10: que se aplique YA. Antes quedaba sólo en el `.env` y la pantalla —que lee
            # `settings`, no el archivo— seguía mostrando el modelo viejo, mientras este mensaje decía
            # «AXIOMA lo usa para ese rol» (falso hasta reiniciar). Se actualiza la copia en memoria y se
            # descarta el router para que la próxima consulta se rearme leyendo los valores nuevos.
            for variable, nombre in a_guardar.items():
                setattr(settings, variable.lower(), nombre)
            try:
                from src.interfaces.web.routes import cleanup_router
                await cleanup_router()
                aplicado = " Se aplica ya: el router se rearmó con tu elección."
            except Exception as exc:  # noqa: BLE001 — R3: se registra y se dice, no se esconde
                _log_error(exc, "routes_preflight.aplicar_modelo")
                aplicado = " Se aplica cuando reinicies AXIOMA (no pude rearmar el router ahora)."
        return {"guardados": sorted(VARIABLES_DE_ROL[rol] for rol in pedidos if rol not in errores),
                "errores": errores,
                "detalle": ("Quedó en tu `.env` (y el preflight deja de pedirte otro modelo)." + aplicado)}
    except Exception as exc:  # noqa: BLE001
        _log_error(exc, "routes_preflight.preflight_guardar_modelos")
        return {"guardados": [], "errores": {"general": f"{type(exc).__name__}: {exc}"}}
