#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# test_rafael_bridge.py - Test de diagnóstico del puente Widget ↔ Daemon
# Ruta: test_rafael_bridge.py (raíz del proyecto)
# Autor: SaintWick
# Versión: 2.0.3
#
# Verifica la comunicación entre el widget y el daemon, el wiring de
# handlers, el ciclo SystemAgent+PromotionGate, y la clasificación de
# intents. Modo rápido por defecto; --full para pruebas con LLM real,
# red y captura de pantalla. Genera un informe persistente en logs/.
# ═══════════════════════════════════════════════════════════════
import asyncio
import inspect
import json
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

# Asegurar que el proyecto está en el path
_PROJECT_ROOT = Path(__file__).resolve().parent
# ✅ 2026-08-25: localizado en tests/ → subir un nivel si falta tools/
if not (_PROJECT_ROOT / "tools").exists():
    _PROJECT_ROOT = _PROJECT_ROOT.parent
sys.path.insert(0, str(_PROJECT_ROOT))

FULL_MODE = "--full" in sys.argv

# ✅ v2.0.3: activa detailed_logger (mismo patrón que test_rafael_e2e.py) —
# sin esto, _log_router() en classifier.py/classifier_core.py no escribe
# nada en logs/reg_error.txt, y la traza PASO a PASO (SPECIAL/llm/
# keywords_fallback/embeddings_fallback/CACHE) de cada clasificación se
# pierde. Necesaria para diagnosticar POR QUÉ una query cae en un
# task_type inesperado, en vez de asumirlo por el resultado final.
try:
    from tools.detailed_logger import initialize_logger
    initialize_logger()
    print("ℹ️  detailed_logger activo — ver logs/reg_error.txt para la traza completa del router\n")
except ImportError:
    pass

_REPORT_LINES: List[str] = []


def _log(line: str = "") -> None:
    print(line)
    _REPORT_LINES.append(line)


def print_header(title: str) -> None:
    _log(f"\n{'═' * 60}")
    _log(f"  {title}")
    _log(f"{'═' * 60}")


def print_result(test: str, passed: bool, detail: str = "") -> None:
    icon = "✅" if passed else "❌"
    _log(f"  {icon} {test}")
    if detail:
        _log(f"     └─ {detail}")


def print_skipped(test: str, reason: str) -> None:
    _log(f"  ⏭️  {test}")
    _log(f"     └─ SKIPPED — {reason}")


async def test_1_dispatcher_init() -> bool:
    """Test 1: Dispatcher se inicializa correctamente."""
    print_header("TEST 1: Dispatcher Inicialización")
    try:
        from src.core.dispatcher import Dispatcher
        d = Dispatcher()
        print_result("Dispatcher creado", True, f"type={type(d).__name__}")
        print_result("Classifier lazy", hasattr(d, 'classifier'), "")
        print_result("Coder lazy", hasattr(d, 'coder'), "")
        print_result("Searcher lazy", hasattr(d, 'searcher'), "")
        print_result("Researcher lazy", hasattr(d, 'researcher'), "")
        print_result("DataHandler lazy", hasattr(d, 'data_handler'), "")
        print_result("JarvisEngine lazy", hasattr(d, 'jarvis_engine'), "")
        return True
    except Exception as e:
        print_result("Dispatcher init", False, str(e))
        return False


async def test_2_session_context() -> bool:
    """Test 2: SessionContext se crea y actualiza."""
    print_header("TEST 2: SessionContext")
    try:
        from src.domain.context import SessionContext
        sid = f"test_{uuid.uuid4().hex[:8]}"
        ctx = SessionContext(session_id=sid)
        print_result("SessionContext creado", True, f"session_id={sid[:20]}")

        ctx.update("general_chat")
        print_result("update(general_chat)", True, f"turns={ctx.turns}")

        ctx.update("code_generation")
        print_result("update(code_generation)", True, f"turns={ctx.turns}, last={ctx.last_intent}")

        ctx.update("web_search")
        print_result("update(web_search)", True, f"recent={ctx.recent_intents}")

        return ctx.turns == 3
    except ImportError:
        print_result("SessionContext import", False, "src.domain.context no disponible")
        return False
    except Exception as e:
        print_result("SessionContext", False, str(e))
        return False


async def test_3_dispatcher_process() -> bool:
    """Test 3: Dispatcher.process() clasifica y rutea correctamente."""
    print_header("TEST 3: Dispatcher.process() — Clasificación Real")
    try:
        from src.core.dispatcher import Dispatcher
        from src.domain.entities import Message, MessageRole
        from src.domain.context import SessionContext

        d = Dispatcher()
        sid = f"test_{uuid.uuid4().hex[:8]}"
        ctx = SessionContext(session_id=sid)

        test_queries = [
            ("¿Qué hora es?", "time_query"),
            ("¿Cómo está el clima en Córdoba?", "weather_query"),
            ("Generá una función factorial en Python", "code_generation"),
            ("Buscá noticias sobre IA hoy", "news_query"),  # _is_news_query() en PASO 1 tiene precedencia sobre web_search — correcto
            ("Hola, ¿cómo estás?", "general_chat"),
            ("Analizá este código: def foo(): pass", "code_explanation"),  # coder.py: CODE_REVIEW="revisa y sugiere mejoras" (no aplica a un stub); CODE_EXPLANATION="explica el código" — matchea "analizá"
            ("¿Dónde estamos?", "location_query"),
        ]

        all_passed = True
        for query, expected_type in test_queries:
            msg = Message(content=query, role=MessageRole.USER, metadata={"source": "test"})
            try:
                # ✅ FIX: 30s era un límite arbitrario mío. Confirmado en
                # dispatcher_handlers.py — _handle_analysis (cubre
                # ANALYSIS_TASKS y code_review) usa base_timeout=420.0
                # internamente; code_generation pasa por _handle_code con
                # ollama_timeout_code_level_1..4 (180-1200s en settings.py).
                # 60s sigue siendo un límite de smoke test, no el real —
                # para las tareas más pesadas puede no alcanzar igual, eso
                # es esperado en este hardware, no una falla de Rafael.
                response = await asyncio.wait_for(
                    d.process(msg, session_id=sid, context=ctx),
                    timeout=60.0,
                )
                actual_type = getattr(response, "task_type", "unknown")
                passed = actual_type == expected_type
                detail = (
                    f"query='{query[:40]}' → "
                    f"expected={expected_type}, actual={actual_type}, "
                    f"model={getattr(response, 'model_used', '?')}, "
                    f"latency={getattr(response, 'latency_ms', 0):.0f}ms"
                )
                print_result(f"Clasificación: {query[:35]}", passed, detail)
                if not passed:
                    all_passed = False
            except asyncio.TimeoutError:
                print_result(f"Clasificación: {query[:35]}", False, "TIMEOUT 60s (límite de smoke test — ver nota v2.0.2)")
                all_passed = False
            except Exception as e:
                print_result(f"Clasificación: {query[:35]}", False, str(e)[:100])
                all_passed = False

        return all_passed
    except Exception as e:
        print_result("Dispatcher.process()", False, str(e))
        return False


async def test_4_ipc_files() -> bool:
    """Test 4: Archivos IPC dialog_in/dialog_out funcionan."""
    print_header("TEST 4: Archivos IPC")
    dialog_in = Path("/tmp/.axioma_rafael_dialog_in")
    dialog_out = Path("/tmp/.axioma_rafael_dialog_out")

    try:
        dialog_in.unlink(missing_ok=True)
        dialog_out.unlink(missing_ok=True)
        print_result("Archivos limpiados", True, "")

        test_data = {"id": str(uuid.uuid4()), "text": "test", "ts": time.time()}
        dialog_in.write_text(json.dumps(test_data), encoding="utf-8")
        print_result("Escritura dialog_in", True, f"id={test_data['id'][:12]}")

        read_data = json.loads(dialog_in.read_text(encoding="utf-8"))
        print_result("Lectura dialog_in", True, f"text={read_data.get('text')}")

        response_data = {"id": test_data["id"], "text": "respuesta test", "ts": time.time()}
        dialog_out.write_text(json.dumps(response_data), encoding="utf-8")
        print_result("Escritura dialog_out", True, "")

        dialog_in.unlink(missing_ok=True)
        dialog_out.unlink(missing_ok=True)
        print_result("Limpieza final", True, "")

        return True
    except Exception as e:
        print_result("IPC files", False, str(e))
        return False


async def test_5_jarvis_fallback() -> bool:
    """Test 5: Fallback Jarvis funciona cuando process() falla."""
    print_header("TEST 5: Fallback Jarvis")
    try:
        from src.multimodal.pipeline_core import _classify_jarvis_intent

        test_cases = [
            ("¿Qué ves en la pantalla?", "screen_describe"),
            ("Abrí Firefox", "autonomous_task"),
            ("¿Qué ves en la cámara?", "camera_describe"),
            ("Hola", "jarvis_query"),
        ]

        all_passed = True
        for text, expected in test_cases:
            result = _classify_jarvis_intent(text)
            passed = result == expected
            print_result(f"_classify_jarvis_intent('{text[:30]}')", passed,
                         f"expected={expected}, actual={result}")
            if not passed:
                all_passed = False

        return all_passed
    except Exception as e:
        print_result("Jarvis fallback", False, str(e))
        return False


async def test_6_daemon_init() -> bool:
    """Test 6: RafaelDaemon se inicializa con session_id persistente."""
    print_header("TEST 6: RafaelDaemon Inicialización")
    try:
        from src.core.rafael_daemon import RafaelDaemon

        daemon = RafaelDaemon(mode="jarvis")
        print_result("RafaelDaemon creado", True, f"modo={daemon._mode}")
        print_result(
            "Dialog poll loop wireado",
            inspect.iscoroutinefunction(daemon._dialog_poll_loop),
            "IPC por archivo (msg_id por mensaje) — v1.1.0, sin session_id persistente"
        )
        print_result(
            "Widget supervisor wireado",
            inspect.iscoroutinefunction(daemon._widget_supervisor),
            "lanza rafael_widget.py como subprocess — no comparte memoria con el daemon"
        )
        print_result(
            "Dispatcher asignado",
            daemon._dispatcher is not None,
            f"type={type(daemon._dispatcher).__name__}"
        )
        print_result("VoicePipeline asignado", daemon._voice_pipeline is not None, "")
        print_result("ProactiveEngine asignado", daemon._proactive_engine is not None, "")

        return True
    except Exception as e:
        print_result("RafaelDaemon init", False, str(e))
        return False


# ═══════════════════════════════════════════════════════════════
# ✅ NUEVO v2.0.0 — Test 7: smoke test de wiring por task_type
# ═══════════════════════════════════════════════════════════════

# Queries de referencia por task_type. No pretenden ser el mejor
# ejemplo posible de cada tarea — el objetivo es forzar que el
# handler y el agente correspondiente se ejecuten de punta a punta,
# no medir calidad de respuesta.
_SMOKE_MESSAGES = {
    # DATA_TASKS — sin LLM, deberían ser rápidas y estables
    "weather_query":   "clima en Córdoba",
    "finance_query":    "cotización del dólar",
    "time_query":       "qué hora es",
    "news_query":       "noticias de hoy",
    "location_query":   "dónde estamos",
    # CODE_TASKS — requieren LLM real (solo con --full)
    "code_generation":    "generá una función que sume dos números",
    "code_correction":    "corregí este código: def f(x) reutrn x+1",
    "code_explanation":   "explicá qué hace: def f(x): return x + 1",
    "code_review":        "revisá este código: def f(x): return x+1",
    "code_optimization":  "optimizá: for i in range(len(a)): print(a[i])",
    "code_testing":       "escribí un test para: def suma(a, b): return a + b",
    "code_documentation": "documentá: def suma(a, b): return a + b",
    "code_migration":     "migrá a Python 3: print 'hola'",
    "code_deployment":    "cómo despliego una app Flask simple",
    "code_debugging":     "por qué falla: def f(x): return x/0",
    "code_autonomous":    "ejecutá código que calcule el factorial de 5",
    # SEARCH_TASKS — requieren red real (solo con --full)
    "web_search":       "buscá noticias sobre inteligencia artificial",
    "news_search":      "noticias sobre Argentina",
    "academic_search":  "papers sobre transformers",
    # ANALYSIS_TASKS — requieren LLM real (solo con --full)
    "data_analysis":      "analizá esta serie de datos: 1,2,3,4,100",
    "document_analysis":  "analizá este documento: informe trimestral de ventas",
    "sentiment_analysis": "analizá el sentimiento: estoy muy contento hoy",
    "trend_analysis":     "analizá la tendencia de ventas del último trimestre",
    "risk_analysis":      "analizá el riesgo de este proyecto de software",
    # VALIDATION_TASKS — requieren LLM real (solo con --full)
    "fact_checking":       "verificá: la Tierra es plana",
    "quality_validation":  "validá la calidad de esta respuesta: 2+2=4",
    "security_audit":      "auditá seguridad de: eval(input())",
    # VISION_TASKS — requieren pantalla real (solo con --full)
    "screenshot_analysis": "describí la pantalla",
}


async def test_7_handler_wiring(d) -> bool:
    """
    Test 7: Smoke test de wiring — bypassea el clasificador y llama
    Dispatcher._dispatch() directo por cada task_type. Objetivo: pescar
    errores de firma/wiring entre handler y agente sin depender de que
    el clasificador acierte el intent y sin esperar la clasificación
    real (~5-6s con LLM). Informativo, no gate: un fallo en CODE/
    SEARCH/ANALYSIS/VALIDATION/VISION no es necesariamente un bug de
    AXIOMA (dependen de LLM/red/pantalla) — igual queda en el informe.
    """
    print_header("TEST 7: Wiring de handlers (bypass clasificador)")
    try:
        from src.domain.entities import Message, MessageRole

        buckets: List[Tuple[str, bool]] = [("DATA_TASKS", False)]
        if FULL_MODE:
            buckets += [
                ("CODE_TASKS", True), ("SEARCH_TASKS", True),
                ("ANALYSIS_TASKS", True), ("VALIDATION_TASKS", True),
                ("VISION_TASKS", True),
            ]

        for bucket_name, informational in buckets:
            task_types = sorted(getattr(d, bucket_name, set()))
            for task_type in task_types:
                query = _SMOKE_MESSAGES.get(task_type)
                if not query:
                    print_skipped(f"[{bucket_name}] {task_type}", "sin query de referencia")
                    continue

                msg = Message(content=query, role=MessageRole.USER, metadata={"source": "diagnostic"})
                sid = f"diag_{uuid.uuid4().hex[:8]}"
                t0 = time.perf_counter()
                try:
                    response = await asyncio.wait_for(
                        d._dispatch(msg, task_type, sid, None), timeout=90.0,
                    )
                    latency = (time.perf_counter() - t0) * 1000
                    err = getattr(response, "error", None) if response is not None else "response=None"
                    ok = response is not None and not err
                    detail = (
                        f"query='{query[:40]}' latency={latency:.0f}ms "
                        f"model={getattr(response, 'model_used', '?')}"
                    )
                    if not ok:
                        detail += f" | error={str(err)[:150]}"
                    tag = "" if not informational else " (informativo)"
                    print_result(f"[{bucket_name}]{tag} {task_type}", ok, detail)
                except Exception as e:
                    latency = (time.perf_counter() - t0) * 1000
                    tag = "" if not informational else " (informativo)"
                    print_result(
                        f"[{bucket_name}]{tag} {task_type}", False,
                        f"EXCEPCIÓN ({latency:.0f}ms): {type(e).__name__}: {e}",
                    )

        if not FULL_MODE:
            _log("")
            _log("  ℹ️  CODE/SEARCH/ANALYSIS/VALIDATION/VISION_TASKS no corridos —"
                 " usá --full para incluirlos (requieren LLM/red/pantalla).")

        return True  # informativo — ver detalle de cada task_type arriba

    except Exception as e:
        print_result("Wiring de handlers (harness)", False, f"{type(e).__name__}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# ✅ NUEVO v2.0.0 — Test 8: SystemAgent + PromotionGate, ciclo real
# ═══════════════════════════════════════════════════════════════

DIAG_SCRATCH = Path("/tmp/rafael_diagnostic_test")


async def test_8_system_promotion_cycle(d) -> bool:
    """
    Test 8: Ciclo completo SystemAgent + PromotionGate sobre un
    directorio scratch en /tmp — fuera del árbol de AXIOMA, nunca
    dispara el copytree de proyecto completo ni toca nada real del
    proyecto. Cubre list_files, write_file (aprobar y descartar),
    move_file (aprobar), delete_file (aprobar), verificando el efecto
    REAL en disco — no solo que la respuesta diga éxito. A diferencia
    del Test 7, este SÍ es gate: es 100% local y determinístico.
    """
    print_header("TEST 8: SystemAgent + PromotionGate — ciclo completo")
    try:
        from src.domain.entities import Message, MessageRole

        DIAG_SCRATCH.mkdir(parents=True, exist_ok=True)
        sid = f"diag_promo_{uuid.uuid4().hex[:8]}"
        all_passed = True

        async def _dispatch_system(text: str, timeout: float = 20.0):
            msg = Message(content=text, role=MessageRole.USER, metadata={"source": "diagnostic"})
            return await asyncio.wait_for(
                d._dispatch(msg, "system_control", sid, None), timeout=timeout,
            )

        # ── list_files ───────────────────────────────────────────────
        resp = await _dispatch_system(f"listá {DIAG_SCRATCH}")
        ok = resp is not None and not getattr(resp, "error", None)
        print_result("list_files", ok, ((resp.content or "") if ok else str(resp.error))[:150])
        all_passed = all_passed and ok

        # ── write_file → aprobar ─────────────────────────────────────
        target_a = DIAG_SCRATCH / "diag_write.txt"
        target_a.unlink(missing_ok=True)
        content_a = "contenido de prueba diagnostico rafael"
        resp = await _dispatch_system(f"escribí {target_a} {content_a}")
        proposed_ok = resp is not None and not getattr(resp, "error", None) \
            and "aprobar" in (resp.content or "").lower()
        print_result("write_file → propuesta generada", proposed_ok, (resp.content or "")[:150])

        resp = await _dispatch_system("aprobar")
        resolved_ok = resp is not None and not getattr(resp, "error", None)
        real_ok = target_a.exists() and target_a.read_text(encoding="utf-8") == content_a
        print_result(
            "write_file → aprobar (efecto real en disco)", resolved_ok and real_ok,
            f"respuesta_ok={resolved_ok} archivo_real_correcto={real_ok} path={target_a} "
            f"resp={(resp.content or resp.error or '')[:200] if resp else 'None'}",
        )
        all_passed = all_passed and proposed_ok and resolved_ok and real_ok

        # ── write_file → descartar (el archivo real NO debe crearse) ─
        target_b = DIAG_SCRATCH / "diag_descartado.txt"
        target_b.unlink(missing_ok=True)
        resp = await _dispatch_system(f"escribí {target_b} este archivo no debería existir")
        proposed_ok = resp is not None and not getattr(resp, "error", None) \
            and "aprobar" in (resp.content or "").lower()
        print_result("write_file(descartable) → propuesta generada", proposed_ok, (resp.content or resp.error or "")[:150])

        resp = await _dispatch_system("descartar")
        resolved_ok = resp is not None and not getattr(resp, "error", None)
        real_ok = not target_b.exists()
        print_result(
            "write_file → descartar (real intacto)", resolved_ok and real_ok,
            f"respuesta_ok={resolved_ok} archivo_NO_existe={real_ok} path={target_b} "
            f"resp={(resp.content or resp.error or '')[:150] if resp else 'None'}",
        )
        all_passed = all_passed and proposed_ok and resolved_ok and real_ok

        # ── move_file → aprobar ──────────────────────────────────────
        target_c = DIAG_SCRATCH / "diag_movido.txt"
        target_c.unlink(missing_ok=True)
        resp = await _dispatch_system(f"moví {target_a} a {target_c}")
        proposed_ok = resp is not None and not getattr(resp, "error", None) \
            and "aprobar" in (resp.content or "").lower()
        print_result("move_file → propuesta generada", proposed_ok, (resp.content or resp.error or "")[:150])

        resp = await _dispatch_system("aprobar")
        resolved_ok = resp is not None and not getattr(resp, "error", None)
        real_ok = target_c.exists() and not target_a.exists()
        print_result(
            "move_file → aprobar (efecto real)", resolved_ok and real_ok,
            f"respuesta_ok={resolved_ok} destino_existe={target_c.exists()} "
            f"origen_removido={not target_a.exists()} "
            f"resp={(resp.content or resp.error or '')[:200] if resp else 'None'}",
        )
        all_passed = all_passed and proposed_ok and resolved_ok and real_ok

        # ── delete_file → aprobar ────────────────────────────────────
        resp = await _dispatch_system(f"eliminá {target_c}")
        proposed_ok = resp is not None and not getattr(resp, "error", None) \
            and "aprobar" in (resp.content or "").lower()
        print_result("delete_file → propuesta generada", proposed_ok, (resp.content or resp.error or "")[:200])

        resp = await _dispatch_system("aprobar")
        resolved_ok = resp is not None and not getattr(resp, "error", None)
        real_ok = not target_c.exists()
        print_result(
            "delete_file → aprobar (efecto real)", resolved_ok and real_ok,
            f"respuesta_ok={resolved_ok} archivo_eliminado={real_ok} "
            f"resp={(resp.content or resp.error or '')[:300] if resp else 'None'}",
        )
        all_passed = all_passed and proposed_ok and resolved_ok and real_ok

        return all_passed

    except Exception as e:
        print_result("Ciclo SystemAgent/PromotionGate", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        shutil.rmtree(DIAG_SCRATCH, ignore_errors=True)


def _write_report_file() -> Path:
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = logs_dir / f"rafael_diagnostic_{ts}.txt"
    out_path.write_text("\n".join(_REPORT_LINES), encoding="utf-8")
    print(f"\n📄 Informe completo guardado en: {out_path.resolve()}")
    return out_path


async def main() -> None:
    print("\n" + "╔" + "═" * 58 + "╗")
    print("║" + "  AXIOMA — Test de Diagnóstico: Puente Widget ↔ Daemon  ".center(58) + "║")
    print("║" + "  Versión 2.0.0 — wiring smoke test + PromotionGate      ".center(58) + "║")
    print("╚" + "═" * 58 + "╝")
    if not FULL_MODE:
        print("\n  Modo rápido (default). Usá --full para incluir CODE/SEARCH/"
              "ANALYSIS/VALIDATION/VISION (LLM real, red, pantalla).\n")

    from src.core.dispatcher import Dispatcher
    d = Dispatcher()

    results = {}
    results["1. Dispatcher Init"] = await test_1_dispatcher_init()
    results["2. SessionContext"] = await test_2_session_context()
    results["3. Dispatcher.process()"] = await test_3_dispatcher_process()
    results["4. IPC Files"] = await test_4_ipc_files()
    results["5. Jarvis Fallback"] = await test_5_jarvis_fallback()
    results["6. Daemon Init"] = await test_6_daemon_init()
    results["7. Wiring de handlers"] = await test_7_handler_wiring(d)
    results["8. SystemAgent + PromotionGate"] = await test_8_system_promotion_cycle(d)

    print_header("RESUMEN")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        icon = "✅" if ok else "❌"
        _log(f"  {icon} {name}")

    _log(f"\n  {'═' * 40}")
    _log(f"  Total: {passed}/{total} tests pasaron")
    if passed == total:
        _log("  🎉 TODOS LOS TESTS PASARON — Rafael está listo")
    else:
        _log(f"  ⚠️  {total - passed} tests fallaron — revisar errores arriba")
    _log(f"  {'═' * 40}\n")

    _write_report_file()


if __name__ == "__main__":
    asyncio.run(main())
