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
from config.paths import Paths
from src.domain.types import TaskType
from src.domain.entities import Message
from src.domain.exceptions import AgentError, LLMError
from src.utils.degradation import log_degraded
from .base import BaseAgent, AgentResult
from .analyst_file_handlers import AnalystFileHandlersMixin
from .analyst_review import AnalystReviewMixin

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
# ✅ Rutas ABSOLUTAS (`Paths.ROOT`): antes eran relativas al CWD, así que el estado
# del RAC cambiaba según desde qué directorio se ejecutara (y la caché incremental
# podía "desaparecer" al arrancar desde otro directorio).
RAC_STATE_PATH = Paths.ROOT / "data" / "rac_state.json"

# ✅ 2026-09-29: control del RAC desde afuera (pedido del usuario: no podía pausar
# ni detenerlo, y no tenía forma de saber en qué estado estaba). Son CENTINELAS:
# si el archivo existe, el bucle actúa en el próximo límite entre archivos.
RAC_STOP_PATH = Paths.ROOT / "data" / "rac_stop"      # detener ordenadamente (escribe informe)
RAC_PAUSE_PATH = Paths.ROOT / "data" / "rac_pause"    # pausar sin consumir CPU
RAC_PAUSA_MAX_ESPERAS = 360                                     # tope de pausa (5 s x 360 = 30 min)
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
        "max_minutes": int(settings.rac_max_minutes),
        "timeout": int(settings.rac_timeout),
        "batch_pause": int(settings.rac_batch_pause),
        "cooldown": int(settings.rac_cooldown),
    }


RAC_CACHE_REL = Paths.ROOT / "data" / "cache" / "rac_analisis.json"   # análisis por archivo (✅ ISSUE-115)


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


class AnalystAgent(AnalystReviewMixin, AnalystFileHandlersMixin, BaseAgent):
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
            model=model or (settings.llm_default_model),
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

