#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# analyst.py - Agente Analista (AnalystAgent)
# Ruta: /ruta/a/axioma/src/agents/analyst.py
# Autor: SaintWick
# Versión: 0.1.2
#
# Analiza documentos, datos y estructuras, extrayendo información clave,
# identificando patrones, tendencias y anomalías. Soporta múltiples formatos
# (JSON, CSV, logs, TOON, PDF, etc.) y proporciona comparaciones y resúmenes.
# Utiliza ContextWindowManager para optimizar el contexto y mantiene
# un modelo exclusivo (qwen3:8b) para la Revisión Axioma Completa (RAC).
# ═══════════════════════════════════════════════════════════════
import logging
import time
import hashlib
import json
import os
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
from config.settings import settings
from src.domain.types import TaskType
from src.domain.entities import Message
from src.domain.exceptions import AgentError, LLMError
from src.utils.degradation import log_degraded
from .base import BaseAgent, AgentResult
from .analyst_file_handlers import AnalystFileHandlersMixin

# ═══════════════════════════════════════════════════════════════
# ✅ FIX RAC (v0.6.9w): pre-análisis estático determinista + segmentación
# sintácticamente íntegra + filtro anti-alucinación. Sin LLM, sin deps extra.
# ═══════════════════════════════════════════════════════════════
from .rac_static import (
    DiagnosticoEstatico,
    analizar_archivo,
    construir_prompt_rac,
    filtrar_alucinaciones,
    leer_fuente,
)

# ═══════════════════════════════════════════════════════════════
# ✅ FIX CRÍTICO: Importar OllamaClientBase y ScenarioType
# para preload y llamada directa desde RAC sin pasar por
# el agente (que hereda CB y fallback de BaseAgent).
# ═══════════════════════════════════════════════════════════════
try:
    from src.llm.client_base import OllamaClientBase, ScenarioType
    _CLIENT_BASE_AVAILABLE = True
except ImportError:
    _CLIENT_BASE_AVAILABLE = False

try:
    from src.llm.client import OllamaClient
    _OLLAMA_CLIENT_AVAILABLE = True
except ImportError:
    _OLLAMA_CLIENT_AVAILABLE = False

# ============================================================================
# INTEGRACIÓN DETAILEDLOGGER — IMPORT SEGURO
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


def _log_agent(event: str, task_type: str = "", duration: float = None,
               success: bool = True, error: str = None):
    """
    Registra ejecución de agente en DetailedLogger.

    Args:
        event: Evento a registrar (started, completed, failed)
        task_type: Tipo de tarea asociada
        duration: Duración en segundos (opcional)
        success: Si la ejecución fue exitosa
        error: Mensaje de error si aplica
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_agent_execution(
                agent_name="analyst", task_type=task_type, state=event,
                duration=duration, success=success, error=error
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 analyst.py:77] excepción degradada (intencional): {_d2e}")


def _log_event(event: str, level: str = "INFO", extra: dict = None):
    """
    Log evento de analyst en DetailedLogger.

    Args:
        event: Mensaje del evento
        level: Nivel de log (INFO, WARNING, ERROR)
        extra: Información adicional para el log
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, "is_initialized") and log.is_initialized:
            log.log_event("ANALYST", event, level=level, extra=extra or {})
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 analyst.py:96] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra_info: Dict[str, Any] = None):
    """
    Registra error con traceback completo en DetailedLogger.

    Args:
        error: Excepción capturada
        context: Contexto donde ocurrió el error
        extra_info: Información adicional para diagnóstico
    """
    if not LOGGER_AVAILABLE:
        logger.error(f"analyst - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error=error, context=context, extra_info=extra_info,
                         function_name=f"analyst.{context}")
    except Exception:
        logger.error(f"analyst - {context}: {error}")


# ============================================================================
# UTILIDADES DE RUTA — ROBUSTEZ DE DIRECTORIO
# ============================================================================
def find_project_root(start_path: Path, markers: tuple = ("pyproject.toml", ".git", "axioma.toml")) -> Path:
    """
    Busca recursivamente hacia arriba el directorio raíz del proyecto
    buscando un archivo centinela. Evita la fragilidad de parents[N].

    Args:
        start_path: Ruta de inicio para la búsqueda (normalmente __file__)
        markers: Tupla de nombres de archivos/directorios que identifican la raíz

    Returns:
        Path: Ruta raíz del proyecto

    Raises:
        RuntimeError: Si no se encuentra ningún centinela y el fallback falla
    """
    current = start_path.resolve()
    while current != current.parent:
        if any((current / marker).exists() for marker in markers):
            return current
        current = current.parent

    # Fallback de seguridad basado en la estructura actual v0.0.9
    logger.warning("find_project_root: No se encontró centinela. Usando fallback parents[2].")
    return start_path.resolve().parents[2]


# ============================================================================
# CLASE PRINCIPAL: AnalystAgent
# ============================================================================
# ✅ plan_memory (2026-08-29, punto 5): map analysis_type → TaskType REAL.
# Antes se armaba f"analysis_{analysis_type}" (ej. "analysis_document"), que no
# matchea ningún TaskType → _get_strategy_for_task() caía al else →
# RecencySelector en vez de RelevanceSelector para análisis.
_ANALYSIS_TASK_TYPE = {
    "document": TaskType.DOCUMENT_ANALYSIS.value,
    "data": TaskType.DATA_ANALYSIS.value,
    "comparison": TaskType.DATA_ANALYSIS.value,
    "structure": TaskType.DOCUMENT_ANALYSIS.value,
    "summary": TaskType.SUMMARIZATION.value,
}


# ═══════════════════════════════════════════════════════════════
# ✅ v0.7.5 (RAC): estado resumible, prioridad por riesgo y cobertura
# ═══════════════════════════════════════════════════════════════
# Motivo MEDIDO (2026-09-29, simulación con las funciones REALES de RAC sobre este
# repo): cada bloque cuesta 100-165 s (prefill 30-93 s + generación 50-75 s a
# 7,92 tok/s) y cada archivo tiene 2-4 bloques ⇒ 5-10 min por archivo. Con el
# presupuesto de 45 min, `parcial` cubría **10 de 48 archivos (21 %)** y `completa`
# **7 de +200 (3 %)**. Como el recorrido es determinista y NO había estado, cada
# corrida repetía el mismo prefijo (el informe del 13-09, previo al chunking AST,
# cubría 31 de 48). Eso explica que RAC "nunca diera buen resultado".
RAC_STATE_PATH = "data/rac_state.json"

# ✅ 2026-09-29: control del RAC desde afuera (pedido del usuario: no podía pausar
# ni detenerlo, y no tenía forma de saber en qué estado estaba). Son CENTINELAS:
# si el archivo existe, el bucle actúa en el próximo límite entre archivos.
RAC_STOP_PATH = Path("data/rac_stop")        # detener ordenadamente (escribe informe)
RAC_PAUSE_PATH = Path("data/rac_pause")      # pausar sin consumir CPU
RAC_PAUSE_POLL = 5.0                          # s entre chequeos mientras está en pausa
RAC_EN_CURSO_TEMPLATE = "RAC_{scope}_en_curso.md"   # informe EN VIVO (se reescribe)

# ✅ 2026-09-29 (ISSUE-114) — COSTO MEDIDO y las dos palancas que lo bajan:
#   577 llamadas para el scope `completa` (213 archivos, 3,67 M de caracteres).
#   · PREFFILL del código: **6,8 h** (el costo dominante: es leer el código).
#   · GENERACIÓN: con `num_predict=600` el peor caso son **12,8 h**; con ~200 tokens
#     de salida típica son **4,3 h**. Por eso el tope baja a `RAC_NUM_PREDICT`.
#   · El `num_ctx` bailaba entre 2048/4096/8192/16384 (49/21/9/1 llamadas) y cada
#     cambio **descarta el KV cacheado** (ISSUE-086): con un valor FIJO, Ollama
#     reutiliza el system prompt común (~370 tok × 577 ≈ 1,3 h de prefill ahorrado)
#     y la RAM del KV deja de moverse. 8192 cubre el prompt más grande visto (6.694
#     tokens) + el tope de salida.
RAC_NUM_CTX = 8192
RAC_NUM_PREDICT = 400

# ✅ 2026-09-29: el system prompt del RAC es una CONSTANTE (antes era un literal
# dentro del bucle) para poder TESTEARLO. Su versión anterior incluía la válvula de
# escape "responde exactamente `SIN HALLAZGOS RELEVANTES`": medido en la corrida real
# del usuario, **79 de 79** llamadas devolvieron esos 12 tokens ⇒ el RAC no producía
# nada (ISSUE-113).
RAC_SYSTEM_PROMPT = (
    "Eres un ingeniero de software senior realizando una revisión de código. "
    "El código que recibes SIEMPRE ha pasado una verificación estática previa con `ast.parse` "
    "(salvo que el prompt diga lo contrario): dala por cierta. "
    "NUNCA reportes errores de sintaxis, bloques sin cerrar, imports truncados, "
    "código incompleto o líneas cortadas; tampoco afirmes que un símbolo no existe si aparece "
    "en la lista de símbolos verificados o en el código enviado. "
    # ✅ 2026-09-29 (medido con USO REAL) — BUG GRAVE corregido: antes acá
    # decía "Si no encuentras defectos reales, responde exactamente
    # `SIN HALLAZGOS RELEVANTES`". Medido en la corrida real del usuario:
    # **79 de 79** llamadas devolvieron esos 12 tokens exactos (y 30 de 38
    # bloques del informe acumulado, el mismo texto) ⇒ el RAC trabajó horas
    # para no producir nada. La válvula de escape es el camino de menor
    # resistencia para un modelo 7B: se reemplaza por una ESTRUCTURA
    # obligatoria que siempre exige mirar el bloque.
    # A/B medido (mismo bloque, mismo modelo): 53 s / 24 chars / 12 tok
    # con el prompt viejo vs 72 s / 608 chars / 171 tok con este.
    "Responde SIEMPRE con estas dos secciones, en este orden:\n"
    "1) `## Qué hace`: 2-3 frases sobre el comportamiento real del bloque, "
    "citando nombres que aparezcan en el código enviado.\n"
    "2) `## Observaciones`: de 1 a 3 puntos concretos y verificables sobre el "
    "bloque enviado (riesgos de lógica/concurrencia/seguridad, casos borde no "
    "cubiertos, coste, claridad, nombres, duplicación). Cada punto con "
    "`archivo:línea` y la cita literal, en 1-2 frases; si es una sospecha y no una "
    "certeza, marcala con `[duda]`.\n"
    # ✅ 2026-09-29 (ISSUE-114): medido — el modelo generaba ~330 tokens por bloque y
    # remataba con un cierre inútil ("SIN HALLAZGOS RELEVANTES"), justo la frase que se
    # quitó del prompt. Acotar a 3 puntos cortos baja la GENERACIÓN (≈4,3 h de las ~11 h
    # del recorrido completo) y evita que la salida toque el tope de `num_predict`.
    "Sé breve: no agregues frases de cierre ni repitas 'sin hallazgos relevantes' "
    "si ya diste observaciones. No inventes símbolos ni archivos que no estén en el "
    "bloque. Si el bloque es trivial (constantes, imports), basta con 1 observación "
    "de claridad o 'nada relevante, bloque trivial'."
)

_RAC_RIESGO_PATRONES = (
    "except:", "except Exception", "subprocess", "threading", "asyncio",
    "eval(", "exec(", "pickle", "shell=True", "os.system", "global ",
    "time.sleep", "while True",
)


def _rac_config() -> Dict[str, int]:
    """Knobs de RAC desde `settings` (que SÍ lee `.env`).

    ✅ v0.7.5: antes eran `os.getenv("AXIOMA_RAC_*")` y **nunca** tomaban el valor
    del `.env` (nadie llama `load_dotenv()`; el `.env` lo lee pydantic). El informe,
    además, aconsejaba "ajustá AXIOMA_RAC_MAX_MINUTES" — imposible de aplicar.
    """
    return {
        "max_minutes": int(getattr(settings, "rac_max_minutes", 45)),
        "timeout": int(getattr(settings, "rac_timeout", 600)),
        "batch_pause": int(getattr(settings, "rac_batch_pause", 45)),
        "cooldown": int(getattr(settings, "rac_cooldown", 3)),
    }


RAC_CACHE_REL = "data/cache/rac_analisis.json"   # análisis por archivo (✅ ISSUE-115)


def _fecha_legible(ts: Any) -> str:
    """Fecha corta de un timestamp guardado (o "(fecha desconocida)")."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    return "(fecha desconocida)"


def _hash_archivo(ruta: Path) -> str:
    """SHA-1 del CONTENIDO (no `mtime`): es la única garantía de "no cambió".

    `mtime`+tamaño es más barato pero puede coincidir tras una edición (y al revés:
    un `touch` marcaría como cambiado algo idéntico). Hashear 3,67 M de caracteres
    cuesta milisegundos frente a las horas de LLM que ahorra.
    """
    try:
        return hashlib.sha1(ruta.read_bytes()).hexdigest()
    except OSError as e:
        # ✅ Con `log_degraded` (el auditor marca como LOW los handlers silenciosos).
        # Devolver "" es seguro por diseño: la huella no coincide ⇒ se re-analiza.
        log_degraded(logger, e, "analyst._hash_archivo (huella de la caché incremental)")
        return ""


def _rac_cache_vacia() -> Dict[str, Any]:
    """Estructura de la caché de análisis, atada a PROMPT y MODELO.

    ⚠️ La huella incluye el prompt del sistema y el modelo: si cambia cualquiera de
    los dos, el análisis guardado **queda obsoleto** y no debe reutilizarse (si no,
    reutilizaríamos análisis escritos por un prompt distinto — exactamente lo que
    bajó la calidad antes de `ISSUE-113`).
    """
    return {
        "version": 1,
        "prompt_hash": hashlib.sha1(RAC_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16],
        # El MODELO lo setea/valida el llamador cuando ya lo conoce (no hay
        # `settings.rac_model`: el modelo sale de `llm_code_model`).
        "modelo": "",
        "archivos": {},
    }


def _rac_cache_cargar(ruta: Path) -> Dict[str, Any]:
    """Carga la caché; si es de otro prompt/modelo la DESCARTA (devuelve vacía)."""
    try:
        if ruta.exists():
            data = json.loads(ruta.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("archivos"), dict):
                ref = _rac_cache_vacia()
                if data.get("prompt_hash") == ref["prompt_hash"]:
                    logger.info(f"RAC incremental: caché con {len(data['archivos'])} "
                                f"archivo(s) analizados previamente.")
                    return data
                logger.warning("RAC incremental: caché descartada (cambió el prompt "
                               "del sistema) ⇒ se re-analiza todo.")
    except (OSError, ValueError) as e:
        log_degraded(logger, e, "analyst._rac_cache_cargar")
    return _rac_cache_vacia()


def _rac_cache_guardar(ruta: Path, cache: Dict[str, Any]) -> None:
    """Persiste la caché (tolerante a fallos: nunca debe romper la corrida)."""
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:
        log_degraded(logger, e, "analyst._rac_cache_guardar")


def _rac_cargar_estado(ruta: Path) -> Dict[str, Any]:
    """Estado de progreso de RAC (por scope). Tolerante a archivo ausente/roto."""
    try:
        if ruta.exists():
            data = json.loads(ruta.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        log_degraded(logger, exc, "AnalystAgent._rac_cargar_estado")
    return {}


def _rac_guardar_estado(ruta: Path, estado: Dict[str, Any]) -> None:
    """Persiste el estado: lo llama RAC tras CADA archivo (no al final), así un
    corte por tiempo/fallos no pierde el trabajo hecho."""
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(estado, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log_degraded(logger, exc, "AnalystAgent._rac_guardar_estado")


def _rac_scope_estado(estado: Dict[str, Any], scope: str,
                      pendientes: List[str]) -> Dict[str, Any]:
    """Estado del scope, abriendo **CICLO NUEVO** si el anterior se completó.

    Un ciclo = una pasada completa por el alcance. Al terminarlo, el siguiente RAC
    arranca de cero (así el análisis completo es acumulativo y repetible).
    """
    sc = estado.setdefault(scope, {})
    completados = set(sc.get("completados") or [])
    if completados and not (set(pendientes) - completados):
        sc["ciclo"] = int(sc.get("ciclo", 0)) + 1
        sc["iniciado"] = time.time()
        sc["ciclos_completos"] = int(sc.get("ciclos_completos", 0)) + 1
        completados = set()
    if not sc.get("iniciado"):
        sc["iniciado"] = time.time()
        sc.setdefault("ciclo", 1)
        sc.setdefault("ciclos_completos", 0)
    sc["completados"] = sorted(completados)
    sc["pendientes_iniciales"] = len(pendientes)
    return sc


def _rac_pendientes(candidatos: List[tuple], completados: Any) -> List[tuple]:
    """Candidatos `(riesgo, rel_path, …)` que todavía NO están completados.

    Extraído del método para poder testear la resumibilidad sin LLM: es el corazón
    de "la próxima corrida continúa donde quedó".
    """
    hechos = set(completados or [])
    return [c for c in candidatos if c[1] not in hechos]


def _rac_riesgo(diag: Any) -> float:
    """Score de riesgo estático (GRATIS: sale del pre-paso, sin LLM).

    Ordena el trabajo para gastar el presupuesto primero donde más importa (antes
    se procesaba en el orden de `os.walk`, o sea arbitrario).
    """
    texto = "".join(u.texto for u in (diag.unidades or []))
    señales = sum(texto.count(p) for p in _RAC_RIESGO_PATRONES)
    return (diag.total_chars / 1000.0) + 2.5 * len(diag.unidades or []) + 3.0 * señales


def _rac_cobertura(total: int, completados: int) -> str:
    """Línea de cobertura del ciclo, para el encabezado del informe."""
    pct = (100.0 * completados / total) if total else 100.0
    return (f"**Cobertura del ciclo: {completados}/{total} archivos "
            f"({pct:.0f}%)**")


def _rac_estado_texto(scope: str) -> str:
    """Texto del `rac_control("status")` (separado para no pasar de 80 líneas)."""
    # ── status ──
    estado_path = Path(RAC_STATE_PATH)
    lineas = [f"Estado del RAC (`{estado_path}`)"]
    if RAC_STOP_PATH.exists():
        lineas.append("  🛑 hay una orden de PARADA pendiente (`data/rac_stop`)")
    if RAC_PAUSE_PATH.exists():
        lineas.append("  ⏸️  hay una PAUSA activa (`data/rac_pause`)")
    try:
        estado = json.loads(estado_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log_degraded(logger, e, "analyst.rac_control (leer estado)")
        lineas.append("  (todavía no hay estado guardado: el RAC no corrió nunca)")
        return "\n".join(lineas)

    for sc in dict.fromkeys((scope, "parcial", "completa")):   # sin repetir el scope pedido
        v = estado.get(sc)
        if not isinstance(v, dict):
            continue
        comp = v.get("completados") or []
        total = int(v.get("pendientes_iniciales") or 0)
        if not total:
            continue
        n = len(comp)
        # ⚠️ El ritmo sólo es válido para el scope que está CORRIENDO: en los demás, el
        # tiempo transcurrido incluye horas de inactividad y daría un ETA falso (medido:
        # el scope `parcial`, parado desde las 14:41, mostraba 25 min/archivo).
        # ⚠️ Sin `try/except`: el auditor marca como LOW los handlers silenciosos y
        # acá el dato sale de NUESTRO propio JSON, así que basta con validar el tipo.
        # ✅ El estado guarda el PID del proceso que corre el ciclo: así "en curso" es
        # un hecho (existe /proc/<pid>) y no un heurístico por tiempo, que marcaba
        # "en curso" un RAC ya detenido.
        _pid = v.get("pid")
        activo = bool(isinstance(_pid, int) and Path(f"/proc/{_pid}").exists())
        restante_txt = "sin actividad reciente (no es un ETA válido)"
        ritmo_txt = "—"
        if activo and n:
            _ini_raw = v.get("iniciado")
            _ini = float(_ini_raw) if isinstance(_ini_raw, (int, float)) else 0.0
            ritmo = ((time.time() - _ini) / 60) / n
            ritmo_txt = f"{ritmo:.2f} min/archivo"
            restante_txt = f"≈ **{ritmo * max(0, total - n):.0f} min**"
        lineas.append(
            f"  · {sc}: **{n}/{total}** ({100.0 * n / total:.0f}%) · "
            f"{'🟢 en curso' if activo else '⚪️ inactivo'} · ritmo {ritmo_txt} · "
            f"restante {restante_txt} · ciclo {v.get('ciclo')} · "
            f"último: `{comp[-1] if comp else '-'}`")
        if sc == scope:
            vivo = Path("docs") / RAC_EN_CURSO_TEMPLATE.format(scope=sc)
            if vivo.exists():
                lineas.append(f"    informe EN VIVO: `{vivo}`")
    lineas.append("  (el presupuesto y el timeout salen de `.env`: "
                   "AXIOMA_RAC_MAX_MINUTES / AXIOMA_RAC_TIMEOUT)")
    return "\n".join(lineas)


def rac_control(accion: str, scope: str = "completa") -> str:
    """Estado y control del RAC desde la CLI (✅ 2026-09-29).

    Pedido del usuario, medido en su corrida real: el RAC estuvo 47 min corriendo
    y **no había forma de saber en qué estado estaba, ni de pausarlo o detenerlo**
    desde la UI (que solo mostraba "Revisión Axioma Completa en progreso...").
    El informe se escribía recién al final, así que matar el proceso perdía todo lo
    que estaba en memoria.

    Acciones:
        - `status`: avance, ritmo, tiempo restante estimado y último archivo.
        - `stop`: crea `data/rac_stop` ⇒ el RAC corta entre archivos y **escribe**
          el informe con lo analizado.
        - `pause` / `resume`: crea/quita `data/rac_pause` (espera sin consumir CPU).
    """
    accion = (accion or "").strip().lower()
    if accion == "stop":
        RAC_STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAC_STOP_PATH.write_text("stop\n", encoding="utf-8")
        return ("Orden de PARADA creada (`data/rac_stop`). El RAC cortará al terminar "
                "el archivo en curso y ESCRIBIRÁ el informe con lo analizado.")
    if accion == "pause":
        RAC_PAUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAC_PAUSE_PATH.write_text("pause\n", encoding="utf-8")
        return ("RAC en PAUSA (`data/rac_pause`): espera entre archivos sin consumir "
                "CPU. Para continuar: `--rac resume`.")
    if accion == "resume":
        quitados = []
        for p in (RAC_PAUSE_PATH, RAC_STOP_PATH):
            if p.exists():
                try:
                    p.unlink()
                    quitados.append(p.name)
                except OSError as e:
                    log_degraded(logger, e, "analyst.rac_control (quitar centinela)")
        return f"Reanudado. Centinelas quitados: {', '.join(quitados) or 'ninguno'}."
    if accion not in ("", "status"):
        return f"Acción desconocida: {accion!r} (usá status|stop|pause|resume)."

    return _rac_estado_texto(scope)


class AnalystAgent(AnalystFileHandlersMixin, BaseAgent):
    """
    Agente especializado en análisis de documentos y datos.

    Features:
    - Análisis de documentos extensos con extracción de información clave
    - Identificación de patrones, tendencias y anomalías en datos
    - Análisis de datos estructurados (JSON, CSV, tablas)
    - Comparación de elementos múltiples con criterios configurables
    - Temperatura 0.4 para balance análisis/creatividad
    - Respuestas concisas (max 512 tokens por defecto)
    - Soporte para múltiples formatos de archivo (.json, .csv, .txt, .log, etc.)

    Example:
    >>> agent = AnalystAgent()
    >>> result = agent.analyze_document("Texto largo para analizar...")
    >>> print(result.content)

    >>> # Análisis de datos JSON
    >>> data = {"ventas": [100, 200, 150], "meses": ["ene", "feb", "mar"]}
    >>> result = agent.analyze_data(data)

    >>> # Comparación de elementos
    >>> items = ["Python", "JavaScript", "Rust"]
    >>> result = agent.compare_items(items, criteria=["rendimiento", "curva de aprendizaje"])
    """

    AGENT_TYPE = "analyst"

    # ✅ CORREGIDO: System prompt con instrucción explícita de concisión
    DEFAULT_SYSTEM_PROMPT = """Eres un analista experto en datos y documentos.
Tu trabajo es:
1. Extraer información clave de documentos
2. Identificar patrones y tendencias
3. Proporcionar insights accionables
4. Mantener precisión y objetividad
5. Citar fuentes cuando sea posible

IMPORTANTE: Sé CONCISO y directo. Máximo 3 párrafos. Evita explicaciones innecesariamente largas.

Formatos soportados: Texto, JSON, CSV, tablas, informes."""

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.4,
        timeout: Optional[int] = None,
        enable_memory: bool = True,
        enable_validation: bool = True,
        max_retries: int = 3,
        system_prompt: Optional[str] = None,
        output_format: str = "structured",
        # ✅ CORREGIDO: Agregar parámetro max_tokens explícito
        max_tokens: int = 512,
    ):
        """
        Inicializa agente analista.

        Args:
            model: Modelo LLM a usar (default: settings.llm_default_model)
            temperature: Temperatura para generación (default 0.4)
            timeout: Timeout en segundos para llamadas LLM
            enable_memory: Habilitar integración con memoria de sesión
            enable_validation: Habilitar validación de resultados
            max_retries: Máximo de reintentos ante fallos
            system_prompt: System prompt personalizado (opcional)
            output_format: Formato de salida (structured/json/plain)
            max_tokens: Máximo de tokens para respuestas (default 512)
        """
        super().__init__(
            model=model or (settings.llm_default_model if hasattr(settings, 'llm_default_model') else "qwen3:8b"),
            temperature=temperature,
            timeout=timeout or settings.ollama_timeout,
            enable_memory=enable_memory,
            enable_validation=enable_validation,
            max_retries=max_retries,
            system_prompt=system_prompt or self.DEFAULT_SYSTEM_PROMPT,
            max_tokens=max_tokens,
        )

        self.output_format = output_format
        self._analysis_stats = {"documents": 0, "patterns_found": 0}

        logger.info(f"AnalystAgent initialized: model={self.config.model}, max_tokens={self.config.max_tokens}")
        _log_event(f"AnalystAgent initialized: model={self.config.model}", extra={
            "model": self.config.model, "temperature": self.config.temperature,
            "output_format": self.output_format, "max_tokens": self.config.max_tokens
        })

    def _build_analysis_prompt(
        self, input_msg: Message, analysis_type: str,
        task_type: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """
        Construye prompt optimizado para análisis según tipo.

        Args:
            input_msg: Mensaje de entrada del usuario
            analysis_type: Tipo de análisis a realizar (document/data/structure/comparison/summary)
            task_type: Tipo de tarea para activar selección de contexto inteligente
            session_id: (plan_memory B) sesión para memoria por-sesión

        Returns:
            str: Prompt completo listo para enviar al LLM
        """
        # ✅ plan_memory (punto 5): task_type REAL, no f"analysis_{analysis_type}".
        if not task_type:
            task_type = _ANALYSIS_TASK_TYPE.get(analysis_type, TaskType.DATA_ANALYSIS.value)
        context = self._get_context(
            limit=8,
            task_type=task_type,
            query=input_msg.content,
            max_tokens=1024,
            session_id=session_id,  # ✅ plan_memory (B)
            user_id=user_id,  # ✅ UI de usuarios
        )
        context_text = "\n".join([f"{m['role']}: {m['content'][:200]}" for m in context]) if context else ""

        analysis_types = {
            "document": "Analiza el documento y extrae información clave. Sé conciso.",
            "data": "Analiza los datos e identifica patrones. Máximo 3 párrafos.",
            "structure": "Analiza la estructura. Sé directo.",
            "comparison": "Compara elementos. Prioriza lo esencial.",
            "summary": "Genera resumen ejecutivo. Conciso y claro.",
        }

        return f"""{analysis_types.get(analysis_type, 'Analiza el contenido. Sé conciso.')}

{f'Contexto:\n{context_text}' if context_text else ''}

Contenido a analizar:
{input_msg.content}

Formato de salida: {self.output_format}

Proporciona tu análisis ahora (máximo 3 párrafos):"""

    def _detect_analysis_type(self, content: str) -> str:
        """
        Detecta automáticamente el tipo de análisis necesario según el contenido.

        Args:
            content: Contenido a analizar (texto del mensaje del usuario)

        Returns:
            str: Tipo de análisis detectado ('document', 'data', 'comparison', 'summary', 'structure')
        """
        content_lower = content.lower()

        if any(w in content_lower for w in ["documento", "pdf", "archivo", "texto"]):
            return "document"
        elif any(w in content_lower for w in ["datos", "csv", "json", "tabla", "número"]):
            return "data"
        elif any(w in content_lower for w in ["compara", "diferencia", "vs"]):
            return "comparison"
        elif any(w in content_lower for w in ["resume", "resumen", "sintetiza"]):
            return "summary"
        else:
            return "structure"

    def execute(
        self, input_msg: Message, analysis_type: Optional[str] = None,
        session_id: Optional[str] = None,  # ✅ plan_memory (B)
        user_id: Optional[str] = None,     # ✅ UI de usuarios (2026-08-31)
    ) -> AgentResult:
        """
        Ejecuta análisis principal del agente.

        Args:
            input_msg: Mensaje de entrada del usuario
            analysis_type: Tipo de análisis (opcional, se auto-detecta si None)
            session_id: Sesión para memoria por-sesión

        Returns:
            AgentResult con el resultado del análisis, metadata y métricas
        """
        start_time = time.time()

        try:
            task = analysis_type or self._detect_analysis_type(input_msg.content)

            self._start_execution(
                task_type=f"analysis_{task}",
                input_summary=input_msg.content[:200],
            )

            prompt = self._build_analysis_prompt(
                input_msg, task,
                task_type=_ANALYSIS_TASK_TYPE.get(task, TaskType.DATA_ANALYSIS.value),
                session_id=session_id,
                user_id=user_id,
            )

            response_content = self._generate(
                prompt=prompt,
                max_tokens=self.config.max_tokens,
            )

            self._analysis_stats["documents"] += 1

            latency_ms = (time.time() - start_time) * 1000

            duration = time.time() - start_time
            _log_agent("completed", task_type=f"analysis_{task}", duration=duration, success=True)
            _log_event(f"Analysis completed: {task}", extra={
                "analysis_type": task, "duration_ms": round(duration * 1000, 2),
                "output_length": len(response_content), "format": self.output_format,
            })
            self._end_execution(success=True, output_summary=response_content[:200])

            return AgentResult(
                success=True,
                content=response_content,
                agent_type=self.AGENT_TYPE,
                model_used=self.config.model,
                task_type=f"analysis_{task}",
                latency_ms=latency_ms,
                confidence=0.8,
                metadata={
                    "analysis_type": task,
                    "format": self.output_format,
                    "max_tokens": self.config.max_tokens,
                },
            )

        except LLMError as e:
            _log_error(e, "execute_llm")
            self._end_execution(success=False, error=str(e))
            return AgentResult(
                success=False,
                content="",
                agent_type=self.AGENT_TYPE,
                model_used=self.config.model,
                task_type="analysis_unknown",
                latency_ms=(time.time() - start_time) * 1000,
                error=str(e),
            )

        except Exception as e:
            _log_error(e, "execute_unexpected")
            self._end_execution(success=False, error=str(e))
            return AgentResult(
                success=False,
                content="",
                agent_type=self.AGENT_TYPE,
                model_used=self.config.model,
                task_type="analysis_unknown",
                latency_ms=(time.time() - start_time) * 1000,
                error=f"Unexpected: {str(e)}",
            )

    def analyze_document(self, text: str, extract_entities: bool = True) -> AgentResult:
        """
        Analiza documento completo extrayendo información clave.

        Args:
            text: Texto completo del documento a analizar
            extract_entities: Si True, extrae entidades clave (personas, organizaciones, fechas)

        Returns:
            AgentResult con el análisis estructurado del documento
        """
        prompt = f"Analiza este documento:\n{text[:5000]}"

        if extract_entities:
            prompt += "\nExtrae entidades clave: personas, organizaciones, fechas, cantidades."

        return self.execute(
            Message(content=prompt, role="user"),
            analysis_type="document",
        )

    def analyze_data(self, data: Any, format_hint: str = "json") -> AgentResult:
        """
        Analiza conjunto de datos estructurados identificando patrones.

        Args:
            data: Datos a analizar (dict, list, str, o cualquier tipo serializable)
            format_hint: Pista de formato para contexto ('json', 'csv', 'dict', etc.)

        Returns:
            AgentResult con análisis de patrones, tendencias e insights
        """
        if isinstance(data, (dict, list)):
            data_str = json.dumps(data, indent=2, ensure_ascii=False)[:5000]
        else:
            data_str = str(data)[:5000]

        prompt = f"Analiza estos datos ({format_hint}):\n{data_str}"
        prompt += "\nIdentifica: patrones, tendencias, anomalías, insights clave."

        return self.execute(
            Message(content=prompt, role="user"),
            analysis_type="data",
        )

    def compare_items(self, items: List[str], criteria: Optional[List[str]] = None) -> AgentResult:
        """
        Compara múltiples elementos según criterios especificados.

        Args:
            items: Lista de elementos/ítems a comparar (máximo 10 mostrados)
            criteria: Lista opcional de criterios de comparación

        Returns:
            AgentResult con comparación estructurada de similitudes y diferencias
        """
        items_text = "\n".join([f"{i+1}. {item}" for i, item in enumerate(items[:10])])

        prompt = f"Compara estos elementos:\n{items_text}"

        if criteria:
            prompt += f"\nCriterios de comparación: {', '.join(criteria)}"
        else:
            prompt += "\nCompara por: similitudes, diferencias, ventajas, desventajas."

        return self.execute(
            Message(content=prompt, role="user"),
            analysis_type="comparison",
        )

    def get_analysis_stats(self) -> Dict[str, Any]:
        """
        Retorna estadísticas de análisis del agente.

        Returns:
            Dict con estadísticas combinadas (base + análisis específico)
        """
        return {
            **self.get_stats(),
            "analysis_stats": self._analysis_stats.copy(),
            "output_format": self.output_format,
        }

    def review_system(self, scope: str = "completa") -> Dict[str, Any]:
        """
        Ejecuta Revisión Axioma Completa (RAC) o Parcial de los archivos del sistema.
        Genera un documento MD en la carpeta docs.

        REGLA: RAC usa SOLO el modelo de código (settings.llm_code_model). Nunca cambia de modelo.
        Sin fallback. Sin VRAM thrashing. Sin switch.
        Procesa en BATCHES con pausas para purgar KV cache de Ollama.
        USA VRAM LOCK para bloquear otros modelos durante la ejecución.

        OPCIÓN B: Mayor profundidad y precisión sin cortar información.

        ✅ FIX RAC ABORT: Flag rac_abort rompe los 3 loops anidados
        (dir_name, root, file) cuando se alcanza límite de tiempo o fallos.

        ✅ FIX RAC v0.6.9w (anti-falsos-positivos):
          - Se lee el archivo COMPLETO (antes: `f.read(1500)` truncaba el código
            y el LLM reportaba "clases sin cerrar" e "imports truncados" falsos).
          - Pre-validación con `ast.parse`: si la sintaxis falla se reporta el
            error REAL y NO se llama al LLM para ese archivo.
          - Segmentación en fronteras de nodos AST (nunca a mitad de una clase).
          - Post-filtro que descarta afirmaciones refutadas por la evidencia
            estática (sintaxis verificada / símbolo que sí existe).

        Args:
            scope: 'completa' (src + config) o 'parcial' (core + domain)

        Returns:
            Dict con success, files_analyzed, report_path, message
        """
        project_root = find_project_root(Path(__file__))
        docs_dir = project_root / "docs"
        docs_dir.mkdir(exist_ok=True)
        # ✅ 2026-09-29 (ISSUE-115): RAC incremental. El archivo ya analizado cuyo
        # CONTENIDO no cambió no se vuelve a enviar al LLM: se reutiliza su análisis
        # anterior (mismo código + mismo prompt + mismo modelo ⇒ misma calidad).
        _solo_cambiados = bool(getattr(settings, "rac_solo_cambiados", True))
        _rac_cache_path = project_root / RAC_CACHE_REL
        _cache = _rac_cache_cargar(_rac_cache_path) if _solo_cambiados else _rac_cache_vacia()
        files_reutilizados = 0

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX VRAM LOCK: Bloquear modelos antes de empezar
        # ═══════════════════════════════════════════════════════════════
        try:
            from src.core.vram_lock import acquire_vram_lock, release_vram_lock
            LOCK_AVAILABLE = True
        except ImportError:
            LOCK_AVAILABLE = False

        if LOCK_AVAILABLE:
            acquire_vram_lock(f"RAC {scope}")

        try:  # ⬅️ BLOQUE TRY PRINCIPAL — Todo RAC corre dentro para garantizar liberar el candado
            # ═══════════════════════════════════════════════════════════════
            # ✅ RAC usa SOLO el modelo de código instalado. Nunca cambia de modelo.
            # ═══════════════════════════════════════════════════════════════
            rac_model = getattr(settings, "llm_code_model", "qwen2.5-coder:7b")

            # ✅ 2026-09-29 (ISSUE-115): el MODELO es parte de la huella del análisis
            # cacheado: si cambia el modelo, el análisis guardado queda obsoleto (se
            # reutilizaría texto escrito por otro modelo ⇒ bajaría la calidad).
            if _solo_cambiados:
                if _cache.get("modelo") and _cache["modelo"] != rac_model:
                    logger.warning(f"RAC incremental: caché descartada (modelo "
                                   f"{_cache['modelo']!r} → {rac_model!r}) ⇒ se re-analiza todo.")
                    _cache = _rac_cache_vacia()
                _cache["modelo"] = rac_model

            # ✅ v0.7.5 (RAC): 240 s recortaba la profundidad — medido con el
            # invariante A3a sobre los bloques reales de este repo: **23 de 26
            # bloques** recibían 400-600 de los 600 tokens pedidos (el prefill de
            # un bloque de 6-11 KB cuesta 30-93 s del timeout). Con 600 s: **0
            # bloques recortados**, por ~5 % más de tiempo total.
            _rac_cfg = _rac_config()
            RAC_TIMEOUT = _rac_cfg["timeout"]

            # ✅ FIX VRAM: Batches con purga. ✅ v0.7.5 (RAC): la pausa de lote pasó
            # a ser CONDICIONAL — medido, quitarla baja el total de 52 a 50 min (no
            # era el cuello de botella: el costo por bloque sí), así que esperar 45 s
            # fijos cada 5 archivos era tiempo muerto. Ahora sólo se pausa si hubo
            # fallos en el lote (señal real de presión de VRAM/CB abierto).
            BATCH_SIZE = 5
            BATCH_PAUSE = _rac_cfg["batch_pause"]
            COOLDOWN_FILE = _rac_cfg["cooldown"]

            # ✅ FIX RAC v0.6.9w: 600 tokens — con 300 la propia respuesta del LLM
            # se cortaba a media frase y el informe parecía "truncado".
            RAC_MAX_TOKENS = RAC_NUM_PREDICT

            # ── Paso 1: Pre-cargar el modelo RAC en VRAM ──────────────────
            logger.info(f"RAC: Pre-cargando modelo {rac_model} en VRAM...")
            _log_event(f"RAC: Pre-cargando modelo {rac_model}", extra={"scope": scope})

            preload_ok = False
            if _CLIENT_BASE_AVAILABLE:
                try:
                    preload_client = OllamaClientBase(
                        model=rac_model, timeout=RAC_TIMEOUT, scenario=ScenarioType.FIRST_LOAD,
                    )
                    preload_ok = preload_client.preload_model(timeout=RAC_TIMEOUT)
                except Exception as e:
                    logger.warning(f"RAC: Pre-carga con OllamaClientBase falló: {e}")
            if not preload_ok and _OLLAMA_CLIENT_AVAILABLE:
                try:
                    preload_client = OllamaClient(
                        model=rac_model, timeout=RAC_TIMEOUT, scenario=ScenarioType.FIRST_LOAD,
                    )
                    preload_ok = preload_client.preload_model(timeout=RAC_TIMEOUT)
                except Exception as e:
                    logger.warning(f"RAC: Pre-carga con OllamaClient falló: {e}")

            if preload_ok:
                logger.info(f"RAC: ✅ Modelo {rac_model} cargado en VRAM")
            else:
                logger.warning(f"RAC: ⚠️ Pre-carga falló, se intentará en caliente")

            # ── Paso 2: Crear cliente directo con disable_fallback=True ──
            # Esto garantiza que NUNCA se cambiará a otro modelo.
            # Si qwen3:8b falla, se lanza LLMError y se maneja aquí.
            rac_client = None
            if _CLIENT_BASE_AVAILABLE:
                try:
                    rac_client = OllamaClientBase(
                        model=rac_model,
                        timeout=RAC_TIMEOUT,
                        disable_fallback=True,
                    )
                    logger.info(f"RAC: Cliente directo creado — timeout={RAC_TIMEOUT}s, max_tokens={RAC_MAX_TOKENS}, disable_fallback=True")
                except Exception as e:
                    logger.error(f"RAC: Error creando cliente directo: {e}")

            if rac_client is None:
                logger.error("RAC: No se pudo crear cliente Ollama. Abortando.")
                return {
                    "success": False,
                    "files_analyzed": 0,
                    "report_path": "",
                    "error": "No se pudo crear cliente Ollama para RAC",
                    "message": "Error: cliente Ollama no disponible"
                }

            # ✅ 2026-09-29 (ISSUE-113): el texto vive en `RAC_SYSTEM_PROMPT` (constante
            # de módulo) para poder testearlo. El literal anterior terminaba con la
            # válvula de escape "responde exactamente `SIN HALLAZGOS RELEVANTES`" y,
            # medido en la corrida REAL del usuario, **79 de 79** llamadas devolvieron
            # esos 12 tokens exactos ⇒ el RAC trabajaba horas para no producir nada.
            rac_system_prompt = RAC_SYSTEM_PROMPT

            target_dirs = ["src", "config"] if scope == "completa" else ["src/core", "src/domain"]
            allowed_extensions = ['.py', '.yaml', '.json', '.txt', '.md']

            md_header = f"# Revisión Axioma ({scope.upper()})\n\n"
            md_header += f"> Generado: {datetime.now().isoformat()}\n\n"
            md_header += f"> Modelo: {rac_model} (max_tokens={RAC_MAX_TOKENS}, exclusivo, sin fallback)\n\n"
            md_header += (
                "> ✅ Pre-análisis estático (v0.6.9w): archivo completo + `ast.parse` + segmentación en "
                "fronteras de nodos AST + filtro de afirmaciones refutadas por evidencia.\n\n"
            )
            md_content = ""
            files_analyzed = 0
            files_failed = 0
            files_in_batch = 0
            files_sin_llm = 0          # archivos con error de sintaxis real (no van al LLM)
            bloques_llm = 0            # llamadas al LLM efectivas
            alucinaciones_descartadas = 0
            diagnosticos: List[DiagnosticoEstatico] = []

            consecutive_failures = 0
            total_failures = 0
            MAX_TOTAL_FAILURES = 8

            rac_start_time = time.time()
            # ✅ FIX RAC v0.6.9w: el chunking hace más llamadas por archivo que la
            # versión antigua (que solo mandaba 1500 caracteres), así que el
            # presupuesto es configurable vía entorno (default 45 min).
            RAC_MAX_MINUTES = max(1, _rac_cfg["max_minutes"])
            MAX_RAC_DURATION = RAC_MAX_MINUTES * 60

            # ✅ FIX RAC ABORT: Flag para romper los 3 loops anidados
            rac_abort = False
            abort_en_bloque = False

            # ═══════════════════════════════════════════════════════════════
            # ✅ v0.7.5 (RAC) — PRE-PASO estático de TODO el alcance (gratis, sin LLM)
            # + orden por RIESGO + estado RESUMIBLE.
            #
            # Antes se recorría con `os.walk` (orden arbitrario) y sin estado: con
            # el presupuesto de 45 min sólo entraban ~10 archivos, así que TODAS las
            # corridas repetían el mismo prefijo y el resto del sistema nunca se
            # analizaba. Ahora: (1) se ordena por riesgo (tamaño + bloques +
            # patrones delicados) para gastar el presupuesto donde más importa,
            # (2) se saltean los ya analizados del ciclo y (3) el avance se guarda
            # tras CADA archivo, así una corrida cortada no pierde nada.
            # ═══════════════════════════════════════════════════════════════
            candidatos: List[tuple] = []
            for dir_name in target_dirs:
                dir_path = project_root / dir_name
                if not dir_path.exists():
                    md_content += f"## Directorio: {dir_name}/\n⚠️ No encontrado\n\n---\n\n"
                    continue
                for root, _, files in os.walk(dir_path):
                    if '__pycache__' in root or '.git' in root:
                        continue
                    for file in files:
                        if any(file.endswith(ext) for ext in allowed_extensions):
                            file_path = os.path.join(root, file)
                            rel_path = os.path.relpath(file_path, project_root)
                            try:
                                content, truncado = leer_fuente(file_path)
                                diag_pre = analizar_archivo(
                                    file_path, source=content, truncado=truncado)
                            except Exception as exc:  # noqa: BLE001
                                log_degraded(logger, exc, "RAC: pre-paso estático")
                                continue
                            diagnosticos.append(diag_pre)
                            candidatos.append((_rac_riesgo(diag_pre), rel_path,
                                               file_path, diag_pre))

            candidatos.sort(key=lambda c: (-c[0], c[1]))
            total_alcance = len(candidatos)
            estado_path = project_root / RAC_STATE_PATH
            estado = _rac_cargar_estado(estado_path)
            sc = _rac_scope_estado(estado, scope, [c[1] for c in candidatos])
            completados = set(sc.get("completados") or [])
            pendientes_ahora = _rac_pendientes(candidatos, completados)
            logger.info(
                f"RAC: alcance {total_alcance} archivos · completados en el ciclo "
                f"{len(completados)} · a analizar ahora {len(pendientes_ahora)} "
                f"(ciclo {sc.get('ciclo')})"
            )
            _log_event("RAC: pre-paso", extra={
                "alcance": total_alcance, "completados": len(completados),
                "pendientes": len(pendientes_ahora), "ciclo": sc.get("ciclo"),
            })

            def _escribir_en_vivo(contenido: str) -> None:
                """Reescribe `docs/RAC_<scope>_en_curso.md` (✅ 2026-09-29, ISSUE-113).

                Se llama tras CADA BLOQUE y tras cada archivo. Medido: con granularidad
                por archivo podían pasar >4 min sin nada que mirar, y el informe final
                solo se escribía al terminar (si el proceso moría, se perdía todo).
                """
                try:
                    _hechos = len(sc.get("completados") or [])
                    _transcurrido = (time.time() - rac_start_time) / 60
                    _ritmo = (_transcurrido / _hechos) if _hechos else 0
                    _restantes = max(0, total_alcance - _hechos)
                    with open(docs_dir / RAC_EN_CURSO_TEMPLATE.format(scope=scope),
                              "w", encoding="utf-8") as _f:
                        _f.write(
                            f"# Revisión Axioma ({scope}) — EN CURSO\n\n"
                            f"> ⏱️ Actualizado: {datetime.now().strftime('%H:%M:%S')} · "
                            f"**{_hechos}/{total_alcance} archivos** "
                            f"({_rac_cobertura(total_alcance, _hechos)})\n"
                            f"> Ritmo: **{_ritmo:.2f} min/archivo** · restante estimado: "
                            f"**{_ritmo * _restantes:.0f} min** · último cerrado: "
                            f"`{rel_path}`\n"
                            f"> (Se reescribe tras cada bloque; el informe final se guarda "
                            f"al terminar.)\n\n---\n\n" + md_header + contenido)
                except OSError as _le:
                    log_degraded(logger, _le, "AnalystAgent.review_system (informe en vivo)")

            fallos_antes_de_pausa = 0
            _pausa_avisada = False       # ✅ 2026-09-29: avisar una sola vez por pausa
            for riesgo, rel_path, file_path, diag in pendientes_ahora:
                if rac_abort:
                    break

                # ✅ 2026-09-29: PARADA/PAUSA LIMPIA pedida por el usuario ("no le puedo
                # poner pausa ni detener" y "no sé en qué estado está"). Se consultan
                # centinelas ENTRE archivos: con `rac_stop` el RAC corta ordenadamente y
                # **escribe el informe con lo analizado** (a diferencia de matar el
                # proceso, que perdía todo lo que estaba en memoria); con `rac_pause`
                # espera sin consumir CPU. Las crea `main.py --rac-stop`/`--rac-pause`.
                if RAC_STOP_PATH.exists():
                    md_content += ("\n---\n\n🛑 **RAC detenido a pedido** "
                                   "(`--rac-stop`): el informe cubre lo analizado hasta acá "
                                   "y la próxima corrida continúa desde el estado guardado.\n")
                    logger.warning("RAC: detenido por centinela rac_stop.")
                    _log_event("RAC: detenido por --rac-stop", level="WARNING")
                    try:
                        RAC_STOP_PATH.unlink()   # consumir la orden (no re-dispara)
                    except OSError as _se:
                        log_degraded(logger, _se, "AnalystAgent.review_system (borrar rac_stop)")
                    rac_abort = True
                    break
                while RAC_PAUSE_PATH.exists():
                    if not _pausa_avisada:
                        logger.info("RAC: en pausa por centinela rac_pause (borralo o usá "
                                    "--rac-resume para continuar).")
                        _log_event("RAC: pausado por --rac-pause")
                        _pausa_avisada = True
                    time.sleep(RAC_PAUSE_POLL)

                # Límite de tiempo
                elapsed = time.time() - rac_start_time
                if elapsed > MAX_RAC_DURATION:
                    md_content += f"\n---\n\n⏱️ **RAC interrumpido por límite de tiempo ({int(elapsed/60)} min de {RAC_MAX_MINUTES} min configurados). El informe cubre solo los archivos anteriores.**\n"
                    logger.warning(f"RAC: Límite de tiempo ({int(elapsed/60)} min).")
                    _log_event("RAC: Time limit reached", level="WARNING",
                               extra={"elapsed_seconds": int(elapsed)})
                    rac_abort = True
                    break

                # Límite de fallos
                if total_failures >= MAX_TOTAL_FAILURES:
                    md_content += f"\n---\n\n❌ **RAC interrumpido: {total_failures} fallos. Posible saturación de VRAM.**\n"
                    logger.warning(f"RAC: {total_failures} fallos. Abortando.")
                    _log_event("RAC: Max failures reached", level="WARNING",
                               extra={"total_failures": total_failures})
                    rac_abort = True
                    break

                # ═══════════════════════════════════════════════════════════
                # ✅ FIX VRAM: Lógica de Batches con Purga
                # ═══════════════════════════════════════════════════════════
                if files_in_batch >= BATCH_SIZE:
                    # ✅ v0.7.5 (RAC): la pausa sólo tiene sentido si hubo fallos en
                    # el lote (señal de presión de VRAM / CB abierto). Medido: quitarla
                    # del todo baja 52 → 50 min, o sea que esperar siempre era tiempo
                    # muerto, pero esperar tras un fallo SÍ ayuda a recuperarse.
                    fallos_del_lote = total_failures - fallos_antes_de_pausa
                    if fallos_del_lote > 0:
                        logger.info(f"RAC: lote con {fallos_del_lote} fallo(s) — purgando VRAM ({BATCH_PAUSE}s)...")
                        _log_event(f"RAC: Batch pause {BATCH_PAUSE}s",
                                   extra={"files_processed": files_in_batch,
                                          "fallos_del_lote": fallos_del_lote})
                        time.sleep(BATCH_PAUSE)
                    fallos_antes_de_pausa = total_failures
                    # Reconstruir cliente para forzar limpieza interna y CB fresco
                    try:
                        rac_client = OllamaClientBase(
                            model=rac_model,
                            timeout=RAC_TIMEOUT,
                            disable_fallback=True,
                        )
                        logger.info("RAC: Cliente reconstruido tras pausa de batch.")
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 analyst.py:956] excepción degradada (intencional): {_d2e}")
                    files_in_batch = 0


                # ═══════════════════════════════════════════════════════════
                # ✅ FIX RAC v0.6.9w — PASO 1: lectura COMPLETA + verificación
                # estática. ✅ v0.7.5: las dos ya las hizo el PRE-PASO (arriba),
                # que además ordena por riesgo y saltea los ya analizados.
                # ═══════════════════════════════════════════════════════════

                # ── Error de SINTAXIS REAL: se reporta sin gastar LLM ──
                if diag.es_python and diag.verificado and not diag.sintaxis_ok:
                    files_sin_llm += 1
                    md_content += (
                        f"## {rel_path}\n"
                        f"### ❌ Error de sintaxis REAL — `ast.parse()` en línea {diag.linea_error}\n"
                        f"`{diag.error_sintaxis}`\n\n"
                        f"> Este archivo NO se envió al LLM: con la sintaxis rota, "
                        f"cualquier análisis semántico sería ruido. Corregir primero.\n\n---\n\n"
                    )
                    logger.warning(f"RAC: sintaxis rota en {rel_path} (línea {diag.linea_error})")
                    _log_event("RAC: syntax error", level="WARNING",
                               extra={"file": rel_path, "line": diag.linea_error})
                    files_in_batch += 1
                    time.sleep(COOLDOWN_FILE)
                    continue  # sin LLM para este archivo

                # ── Si 3 fallos consecutivos, pausar para VRAM ──
                if consecutive_failures >= 3:
                    pause_time = 60
                    logger.info(
                        f"RAC: {consecutive_failures} fallos consecutivos. "
                        f"Pausando {pause_time}s para recuperación de VRAM "
                        f"(qwen3:8b permanece, no se cambia modelo)."
                    )
                    time.sleep(pause_time)
                    # Re-crear cliente con CB fresco (CLOSED)
                    try:
                        rac_client = OllamaClientBase(
                            model=rac_model,
                            timeout=RAC_TIMEOUT,
                            disable_fallback=True,
                        )
                    except Exception as e:
                        # R3: se sigue con el cliente anterior (degradación visible).
                        log_degraded(logger, e, "AnalystAgent.review_system (recrear cliente RAC)")
                    consecutive_failures = 0

                # ═══════════════════════════════════════════════════════════
                # ✅ FIX RAC v0.6.9w — PASO 2: bloques sintácticamente íntegros
                # (chunks en fronteras de nodos AST, no cortes de 1500 chars)
                # ═══════════════════════════════════════════════════════════
                # ✅ 2026-09-29 (ISSUE-115): RAC INCREMENTAL — si el CONTENIDO del archivo
                # es idéntico al ya analizado, no se gasta ni un token: se reutiliza el
                # análisis anterior (que ya trae su encabezado) y se deja constancia en el
                # informe. La clave es que el hash cubre el contenido y la caché está atada
                # al prompt y al modelo ⇒ se reutiliza EXACTAMENTE el análisis del mismo
                # código, sin bajar la calidad.
                _entrada = _cache["archivos"].get(rel_path) if _solo_cambiados else None
                if (_entrada and _entrada.get("hash")
                        and _entrada["hash"] == _hash_archivo(Path(file_path))):
                    md_content += (
                        f"{_entrada.get('texto', '')}\n"
                        f"> ♻️ **Análisis REUTILIZADO** (`ISSUE-115`): el archivo no cambió "
                        f"desde {_fecha_legible(_entrada.get('fecha'))} ⇒ mismo código, mismo "
                        f"prompt y mismo modelo, así que el análisis anterior sigue siendo "
                        f"válido (no se gastó cómputo en repetirlo).\n\n---\n\n")
                    files_reutilizados += 1
                    sc["completados"] = sorted(set(sc.get("completados") or []) | {rel_path})
                    sc["ultima_corrida"] = time.time()
                    sc["pid"] = os.getpid()
                    _rac_guardar_estado(estado_path, estado)
                    _escribir_en_vivo(md_content)
                    files_in_batch += 1
                    logger.info(f"RAC incremental: {rel_path} sin cambios ⇒ análisis reutilizado")
                    continue

                _inicio_archivo = len(md_content)   # ✅ ISSUE-115: para cachear su texto
                md_content += (
                    f"## {rel_path}\n"
                    f"> **Verificación estática:** {diag.resumen()} · "
                    f"{diag.total_lineas} líneas · {len(diag.unidades)} bloque(s) RAC\n\n"
                )

                archivo_ok = False
                for unidad in diag.unidades:
                    if time.time() - rac_start_time > MAX_RAC_DURATION:
                        rac_abort = True
                        abort_en_bloque = True
                        break
                    if total_failures >= MAX_TOTAL_FAILURES:
                        rac_abort = True
                        abort_en_bloque = True
                        break

                    prompt = construir_prompt_rac(file, unidad, diag)
                    etiqueta = (
                        f"Bloque {unidad.indice}/{unidad.total} "
                        f"(líneas {unidad.linea_inicio}-{unidad.linea_fin}, "
                        f"modo `{unidad.modo}`)"
                    )
                    try:
                        response = rac_client.generate(
                            prompt=prompt,
                            system=rac_system_prompt,
                            temperature=0.1,
                            timeout=RAC_TIMEOUT,
                            task_type="rac_review",
                            use_cache=False,
                            max_tokens=RAC_MAX_TOKENS,
                            num_ctx=RAC_NUM_CTX,
                            # ✅ ISSUE-102: RAC **es** el dueño del candado de VRAM,
                            # así que sus propias llamadas no deben pasar por el swap:
                            # con `ISSUE-101` corregido (el candado ya bloquea de
                            # verdad), `ModelSwap` esperaba 300 s y rehusaba con
                            # "[ModelSwap] VRAM locked por RAC" ⇒ RAC se auto-bloqueaba
                            # y analizaba 0 archivos. El modelo ya está pre-cargado y
                            # el cliente es `disable_fallback=True`.
                            skip_model_swap=True,
                        )
                        review_text = response.content.strip() if response.content else ""
                        bloques_llm += 1

                        if review_text:
                            review_text, descartes = filtrar_alucinaciones(review_text, diag)
                            alucinaciones_descartadas += len(descartes)
                            md_content += f"**{etiqueta}**\n{review_text}\n\n"
                            _escribir_en_vivo(md_content)
                            if descartes:
                                md_content += (
                                    "<details><summary>Afirmaciones descartadas por evidencia "
                                    f"estática ({len(descartes)})</summary>\n\n"
                                    + "\n".join(f"- {d}" for d in descartes[:10])
                                    + "\n\n</details>\n\n"
                                )
                            archivo_ok = True
                            consecutive_failures = 0
                        else:
                            md_content += f"**{etiqueta}**\n⚠️ Sin observaciones\n\n"
                            consecutive_failures += 1
                            total_failures += 1

                    except LLMError as e:
                        # CB OPEN o timeout — NO cambia de modelo
                        logger.warning(f"RAC: Error en {rel_path}: {e}")
                        md_content += f"**{etiqueta}**\n❌ Error: {e}\n\n"
                        consecutive_failures += 1
                        total_failures += 1

                        logger.warning(f"RAC: Error inesperado en {rel_path}: {e}")
                        md_content += f"**{etiqueta}**\n❌ Error: {e}\n\n"
                        consecutive_failures += 1
                        total_failures += 1

                    time.sleep(COOLDOWN_FILE)

                md_content += "---\n\n"
                if archivo_ok:
                    files_analyzed += 1
                else:
                    files_failed += 1

                # ✅ v0.7.5 (RAC): archivo TERMINADO (DENTRO del bucle) → se marca y
                # se guarda el estado, así un corte por tiempo no pierde el trabajo.
                # Se exige `archivo_ok` (algún bloque respondió): marcar un archivo
                # cuyos bloques fallaron haría que la reanudación NUNCA lo reintente.
                if not abort_en_bloque and archivo_ok:
                    sc["completados"] = sorted(
                        set(sc.get("completados") or []) | {rel_path})
                    sc["ultima_corrida"] = time.time()
                    sc["pid"] = os.getpid()
                    _rac_guardar_estado(estado_path, estado)

                # ✅ 2026-09-29 (ISSUE-115): guardar el análisis de ESTE archivo para
                # reutilizarlo si su contenido no cambia (hash SHA-1 del contenido).
                if not abort_en_bloque and archivo_ok and _solo_cambiados:
                    try:
                        _cache["archivos"][rel_path] = {
                            "hash": _hash_archivo(Path(file_path)),
                            "fecha": time.time(),
                            "texto": md_content[_inicio_archivo:],
                        }
                    except (OSError, TypeError, ValueError) as _ce:
                        log_degraded(logger, _ce, "AnalystAgent.review_system (cachear análisis)")

                _escribir_en_vivo(md_content)

                # Contador de batch y cooldown entre archivos
                files_in_batch += 1
                time.sleep(COOLDOWN_FILE)

            # ✅ ISSUE-115: persistir la caché de análisis por archivo.
            if _solo_cambiados:
                _rac_cache_guardar(_rac_cache_path, _cache)

            # ── Escribir reporte final ──
            rac_duration = time.time() - rac_start_time
            if abort_en_bloque:
                md_content += (
                    f"\n---\n\n⏱️ **RAC detenido antes de completar el alcance** "
                    f"(presupuesto agotado: {RAC_MAX_MINUTES} min o {MAX_TOTAL_FAILURES} fallos). "
                    f"Ajusta `AXIOMA_RAC_MAX_MINUTES` o repite la RAC para continuar.\n"
                )
            total_lineas = sum(d.total_lineas for d in diagnosticos)
            verificados = sum(
                1 for d in diagnosticos if d.es_python and d.verificado and d.sintaxis_ok
            )
            con_error = [d for d in diagnosticos if d.es_python and d.verificado and not d.sintaxis_ok]

            md_estatico = "## 0. Verificación estática previa (determinista, sin LLM)\n\n"
            md_estatico += (
                f"- Archivos pre-analizados: **{len(diagnosticos)}** ({total_lineas} líneas)\n"
                f"- Módulos Python con `ast.parse` OK: **{verificados}**\n"
                f"- Errores de sintaxis REALES: **{len(con_error)}**\n"
                f"- Afirmaciones del LLM descartadas por evidencia estática: **{alucinaciones_descartadas}**\n"
                f"- Llamadas al LLM (bloques): **{bloques_llm}**\n\n"
            )
            if con_error:
                md_estatico += "| Archivo | Error real | Línea |\n|---|---|---|\n"
                for d in con_error:
                    nombre = os.path.relpath(d.ruta, project_root)
                    md_estatico += f"| `{nombre}` | `{d.error_sintaxis}` | {d.linea_error} |\n"
                md_estatico += "\n"
            else:
                md_estatico += (
                    "> Ningún archivo Python analizado tiene errores de sintaxis, clases abiertas, "
                    "imports truncados ni paréntesis desbalanceados. Cualquier afirmación de ese tipo en "
                    "las revisiones siguientes está refutada por esta verificación.\n\n"
                )
            md_estatico += "---\n\n"

            md_content = md_estatico + md_content
            md_content += f"\n---\n\n> **Total archivos analizados:** {files_analyzed}\n"
            md_content += f"> **Total archivos fallidos:** {files_failed}\n"
            md_content += f"> **Archivos con error de sintaxis (sin LLM):** {files_sin_llm}\n"
            md_content += f"> **Bloques enviados al LLM:** {bloques_llm}\n"
            md_content += (
                f"> **RAC incremental:** {'ACTIVO' if _solo_cambiados else 'desactivado'} · "
                f"**re-analizados ahora:** {files_analyzed} · "
                f"**reutilizados (sin cambios):** {files_reutilizados}"
                + (" · para forzar el re-análisis completo: "
                   "`AXIOMA_RAC_SOLO_CAMBIADOS=false`" if _solo_cambiados else "") + "\n")

            md_content += f"> **Afirmaciones descartadas por evidencia estática:** {alucinaciones_descartadas}\n"
            md_content += f"> **Modelo utilizado:** {rac_model} (exclusivo, sin fallback)\n"
            md_content += f"> **Duración:** {int(rac_duration // 60)}m {int(rac_duration % 60)}s\n"
            md_content += f"> **Scope:** {scope}\n"
            # ✅ v0.7.5 (RAC): cobertura del ciclo + pendientes (para ver el avance
            # entre corridas: sin esto, cada RAC repetía el mismo prefijo y parecía
            # que "no servía").
            _completados_final = set(sc.get("completados") or [])
            md_content += f"> {_rac_cobertura(total_alcance, len(_completados_final))}\n"
            md_content += (f"> **Ciclo:** {sc.get('ciclo')} · **Analizados ahora:** "
                           f"{files_analyzed} · **Pendientes del ciclo:** "
                           f"{max(0, total_alcance - len(_completados_final))}\n")
            md_content += f"> **Orden:** por riesgo estático (mayor primero)\n"

            acumulado = docs_dir / (f"RAC_{scope}_acumulado_ciclo{sc.get('ciclo')}.md")
            try:
                _nuevo_ciclo = not acumulado.exists()
                with open(acumulado, "a", encoding="utf-8") as f:
                    if _nuevo_ciclo:
                        f.write(f"# Revisión Axioma ({scope.upper()}) — ciclo "
                                f"{sc.get('ciclo')}\n\n> Documento ACUMULADO: cada corrida "
                                f"agrega los archivos que le tocaron hasta completar el "
                                f"alcance.\n\n")
                    f.write(md_header + md_content + "\n\n")
                logger.info(f"RAC: informe acumulado del ciclo → {acumulado}")
            except Exception as e:
                log_degraded(logger, e, "AnalystAgent.review_system (informe acumulado)")

            report_path = docs_dir / f"RAC_{scope}_{int(time.time())}.md"
            try:
                with open(report_path, 'w', encoding='utf-8') as f:
                    f.write(md_header + md_content)
                logger.info(f"RAC: Reporte guardado en {report_path}")
            except Exception as e:
                logger.error(f"RAC: Error guardando reporte: {e}")
                return {
                    "success": False,
                    "files_analyzed": files_analyzed,
                    "report_path": str(report_path),
                    "error": f"Error guardando reporte: {e}",
                    "message": f"Revisión completada pero error guardando: {e}"
                }

            return {
                "success": True,
                "files_analyzed": files_analyzed,
                "report_path": str(report_path),
                "message": f"Revisión completada. {files_analyzed} archivos analizados en {int(rac_duration // 60)}m {int(rac_duration % 60)}s. Reporte guardado en {report_path}"
            }

        finally:
            # ═══════════════════════════════════════════════════════════════
            # ✅ FIX VRAM LOCK: Liberar candado SIEMPRE, incluso si falla RAC
            # ═══════════════════════════════════════════════════════════════
            if LOCK_AVAILABLE:
                release_vram_lock()
