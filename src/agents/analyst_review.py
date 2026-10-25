#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — analyst_review.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/agents/analyst_review.py
#
# ✅ 2026-10-22 (refactorización 50/50): la mitad de REVISIÓN del sistema (RAC) se extrae de
#   `src/agents/analyst.py` a este hermano. Es HOJA: **no** importa el original a nivel de
#   modulo, solo DENTRO de `review_system` (diferido) => no hay ciclo de import.
#
#   Qué se movió: SOLO el método `review_system` (→ `AnalystReviewMixin`).
#   Las constantes RAC, las funciones `_rac_*`, `rac_control` y el cliente Ollama se QUEDAN en
#   analyst.py, y este mixin los lee a traves del modulo (`analyst.RAC_PAUSE_PATH`,
#   `analyst.OllamaClientBase`, `time`, …). Motivo: **un solo objeto** => el monkeypatch
#   de los tests llega, y no se construye un cliente real por accidente.
# ═══════════════════════════════════════════════════════════════
import logging
import time
import os
from datetime import datetime
from typing import Dict, Any, List
from pathlib import Path
from config.settings import settings
from src.domain.exceptions import LLMError
from src.utils.degradation import log_degraded

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
    from src.llm.client_base import OllamaClientBase, ScenarioType  # noqa: F401 — disponibilidad
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
    from tools.detailed_logger import get_logger  # noqa: F401 — disponibilidad
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


class AnalystReviewMixin:
    """Revisión Axioma Completa (RAC): extraída de AnalystAgent (refactorización 50/50)."""

    def review_system(self, scope: str = "completa") -> Dict[str, Any]:
        import src.agents.analyst as analyst  # noqa: F401  (diferido: un solo objeto, sin ciclo)
        from src.agents.analyst import (  # noqa: F401  (helpers no parcheados)
            _fecha_legible, _hash_archivo, _log_event, _rac_cache_cargar, _rac_cache_guardar,
            _rac_cache_vacia, _rac_cargar_estado, _rac_cobertura, _rac_config, _rac_estado_texto,
            _rac_guardar_estado, _rac_pendientes, _rac_riesgo, _rac_scope_estado,
        )
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
        project_root = analyst.find_project_root(Path(__file__))
        docs_dir = project_root / "docs"
        docs_dir.mkdir(exist_ok=True)
        # ✅ 2026-09-29 (ISSUE-115): RAC incremental. El archivo ya analizado cuyo
        # CONTENIDO no cambió no se vuelve a enviar al LLM: se reutiliza su análisis
        # anterior (mismo código + mismo prompt + mismo modelo ⇒ misma calidad).
        _solo_cambiados = bool(settings.rac_solo_cambiados)
        analyst._rac_cache_path = project_root / analyst.RAC_CACHE_REL
        _cache = _rac_cache_cargar(analyst._rac_cache_path) if _solo_cambiados else _rac_cache_vacia()
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
            rac_model = settings.llm_code_model

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
            analyst._rac_cfg = _rac_config()
            RAC_TIMEOUT = analyst._rac_cfg["timeout"]

            # ✅ FIX VRAM: Batches con purga. ✅ v0.7.5 (RAC): la pausa de lote pasó
            # a ser CONDICIONAL — medido, quitarla baja el total de 52 a 50 min (no
            # era el cuello de botella: el costo por bloque sí), así que esperar 45 s
            # fijos cada 5 archivos era tiempo muerto. Ahora sólo se pausa si hubo
            # fallos en el lote (señal real de presión de VRAM/CB abierto).
            BATCH_SIZE = 5
            BATCH_PAUSE = analyst._rac_cfg["batch_pause"]
            COOLDOWN_FILE = analyst._rac_cfg["cooldown"]

            # ✅ FIX RAC v0.6.9w: 600 tokens — con 300 la propia respuesta del LLM
            # se cortaba a media frase y el informe parecía "truncado".
            RAC_MAX_TOKENS = analyst.RAC_NUM_PREDICT

            # ── Paso 1: Pre-cargar el modelo RAC en VRAM ──────────────────
            logger.info(f"RAC: Pre-cargando modelo {rac_model} en VRAM...")
            _log_event(f"RAC: Pre-cargando modelo {rac_model}", extra={"scope": scope})

            preload_ok = False
            if analyst._CLIENT_BASE_AVAILABLE:
                try:
                    preload_client = analyst.OllamaClientBase(
                        model=rac_model, timeout=RAC_TIMEOUT, scenario=ScenarioType.FIRST_LOAD,
                    )
                    preload_ok = preload_client.preload_model(timeout=RAC_TIMEOUT)
                except Exception as e:
                    logger.warning(f"RAC: Pre-carga con analyst.OllamaClientBase falló: {e}")
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
                logger.warning("RAC: ⚠️ Pre-carga falló, se intentará en caliente")

            # ── Paso 2: Crear cliente directo con disable_fallback=True ──
            # Esto garantiza que NUNCA se cambiará a otro modelo.
            # Si qwen3:8b falla, se lanza LLMError y se maneja aquí.
            rac_client = None
            if analyst._CLIENT_BASE_AVAILABLE:
                try:
                    rac_client = analyst.OllamaClientBase(
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

            # ✅ 2026-09-29 (ISSUE-113): el texto vive en `analyst.RAC_SYSTEM_PROMPT` (constante
            # de módulo) para poder testearlo. El literal anterior terminaba con la
            # válvula de escape "responde exactamente `SIN HALLAZGOS RELEVANTES`" y,
            # medido en la corrida REAL del usuario, **79 de 79** llamadas devolvieron
            # esos 12 tokens exactos ⇒ el RAC trabajaba horas para no producir nada.
            rac_system_prompt = analyst.RAC_SYSTEM_PROMPT

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
            RAC_MAX_MINUTES = max(1, analyst._rac_cfg["max_minutes"])
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
            estado_path = project_root / analyst.RAC_STATE_PATH
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
                    with open(docs_dir / analyst.RAC_EN_CURSO_TEMPLATE.format(scope=scope),
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
                if analyst.RAC_STOP_PATH.exists():
                    md_content += ("\n---\n\n🛑 **RAC detenido a pedido** "
                                   "(`--rac-stop`): el informe cubre lo analizado hasta acá "
                                   "y la próxima corrida continúa desde el estado guardado.\n")
                    logger.warning("RAC: detenido por centinela rac_stop.")
                    _log_event("RAC: detenido por --rac-stop", level="WARNING")
                    try:
                        analyst.RAC_STOP_PATH.unlink()   # consumir la orden (no re-dispara)
                    except OSError as _se:
                        log_degraded(logger, _se, "AnalystAgent.review_system (borrar rac_stop)")
                    rac_abort = True
                    break
                _pausa_esperas = 0
                while analyst.RAC_PAUSE_PATH.exists():
                    if not _pausa_avisada:
                        logger.info("RAC: en pausa por centinela rac_pause (borralo o usá "
                                    "--rac-resume para continuar).")
                        _log_event("RAC: pausado por --rac-pause")
                        _pausa_avisada = True
                    _pausa_esperas += 1
                    if _pausa_esperas > analyst.RAC_PAUSA_MAX_ESPERAS:
                        # Tope de seguridad: ni un centinela perdido puede colgar el RAC (ni la
                        # suite) para siempre. La pausa normal (un humano que la levante) sigue igual.
                        logger.warning(
                            "RAC: tope de pausa alcanzado (%s esperas); se abandona la pausa y sigue.",
                            _pausa_esperas,
                        )
                        _log_event("RAC: pausa abandonada por tope de seguridad")
                        break
                    analyst.time.sleep(analyst.RAC_PAUSE_POLL)

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
                        analyst.time.sleep(BATCH_PAUSE)
                    fallos_antes_de_pausa = total_failures
                    # Reconstruir cliente para forzar limpieza interna y CB fresco
                    try:
                        rac_client = analyst.OllamaClientBase(
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
                    analyst.time.sleep(COOLDOWN_FILE)
                    continue  # sin LLM para este archivo

                # ── Si 3 fallos consecutivos, pausar para VRAM ──
                if consecutive_failures >= 3:
                    pause_time = 60
                    logger.info(
                        f"RAC: {consecutive_failures} fallos consecutivos. "
                        f"Pausando {pause_time}s para recuperación de VRAM "
                        f"(qwen3:8b permanece, no se cambia modelo)."
                    )
                    analyst.time.sleep(pause_time)
                    # Re-crear cliente con CB fresco (CLOSED)
                    try:
                        rac_client = analyst.OllamaClientBase(
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
                            num_ctx=analyst.RAC_NUM_CTX,
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

                    analyst.time.sleep(COOLDOWN_FILE)

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
                analyst.time.sleep(COOLDOWN_FILE)

            # ✅ ISSUE-115: persistir la caché de análisis por archivo.
            if _solo_cambiados:
                _rac_cache_guardar(analyst._rac_cache_path, _cache)

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
            md_content += "> **Orden:** por riesgo estático (mayor primero)\n"

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
