#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# warmup_models.py - Model Warm-up & Keep-alive
# Ruta: tools/warmup_models.py
# Autor: SaintWick
# Versión: 1.3.0
#
# Pre-carga modelos en memoria al arrancar para eliminar cold-start,
# y realiza pings periódicos para mantenerlos cargados (evita que
# Ollama los descargue por inactividad). Solo calienta el modelo
# default; el modelo de código se carga on-demand vía ModelSwap
# para evitar VRAM thrashing.
# ═══════════════════════════════════════════════════════════════
import asyncio
import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from config.settings import settings
    _OLLAMA_HOST = settings.ollama_host
    _DEFAULT_MODEL = settings.llm_default_model
    _CODE_MODEL = settings.llm_code_model
except Exception:
    _OLLAMA_HOST = "http://127.0.0.1:11434"
    _DEFAULT_MODEL = "qwen3:8b"
    _CODE_MODEL = "qwen2.5-coder:7b"

# ═══════════════════════════════════════════════════════════════
# ✅ FIX VRAM LOCK: Importar candado para no interferir con modelos activos
# ═══════════════════════════════════════════════════════════════
try:
    from src.core.vram_lock import is_vram_locked, get_vram_lock_reason, wait_for_vram_unlock
    _VRAM_LOCK_AVAILABLE = True
except ImportError:
    _VRAM_LOCK_AVAILABLE = False

logger = logging.getLogger("axioma.warmup")

# ── Configuración ────────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════
# ✅ v1.3.0: SOLO modelo default en warmup.
# El modelo de código se carga on-demand vía ModelSwap.ensure()
# cuando una tarea lo requiere. Calentar ambos modelos causaba
# VRAM thrashing: ~4.5GB (8B) + ~8.5GB (14B) = ~13GB total
# > ~8-12GB VRAM disponible en Ryzen 5 8500G con iGPU compartida.
#
# ANTES (v1.2.1):
#   MODELS_TO_WARM = [_DEFAULT_MODEL, _CODE_MODEL]  # Ambos → OOM
#
# DESPUÉS (v1.3.0):
#   MODELS_TO_WARM = [_DEFAULT_MODEL]  # Solo default → safe
# ═══════════════════════════════════════════════════════════════
MODELS_TO_WARM: List[str] = [
    _DEFAULT_MODEL,   # Chat general — SIEMPRE cargar al inicio
]

# Todos los modelos configurados (para --status y referencia)
ALL_CONFIGURED_MODELS: List[str] = [
    _DEFAULT_MODEL,
    _CODE_MODEL,
]

# Prompt mínimo para forzar carga del modelo sin generar tokens innecesarios
WARMUP_PROMPT = " "

# Keep-alive: cada cuántos segundos hacer ping (< TTL Ollama de 5 min)
KEEPALIVE_INTERVAL_SECONDS = 240  # 4 min

# Timeout para el request de warm-up (modelos grandes tardan en cargarse)
WARMUP_TIMEOUT_SECONDS = 120

# ✅ v1.2.0 FIX: Timeout para ping de keep-alive igualado al warm-up.
# Si Ollama descargó el modelo por inactividad, /api/generate debe
# recargarlo completo (8.4GB para qwen2.5-coder:14b = 30-90s).
# Con 30s el request siempre timeouteaba en ese escenario.
KEEPALIVE_TIMEOUT_SECONDS = 120


# ── Core ─────────────────────────────────────────────────────────────────────

async def warm_up_model(model: str, client: httpx.AsyncClient) -> bool:
    """
    Fuerza la carga del modelo en memoria enviando un prompt mínimo.
    Usa num_predict=1 para generar exactamente 1 token y salir.
    """
    # ═══════════════════════════════════════════════════════════════
    # ✅ FIX VRAM LOCK: No cargar modelos si RAC está corriendo
    # ═══════════════════════════════════════════════════════════════
    if _VRAM_LOCK_AVAILABLE and is_vram_locked():
        reason = get_vram_lock_reason()
        logger.info(f"🔒 Warm-up cancelado — VRAM bloqueada por {reason}")
        return False

    start = time.perf_counter()
    try:
        resp = await client.post(
            f"{_OLLAMA_HOST}/api/generate",
            json={
                "model": model,
                "prompt": WARMUP_PROMPT,
                "stream": False,
                "options": {
                    "num_predict": 1,
                    "temperature": 0.0,
                },
                "keep_alive": "10m",
            },
            timeout=WARMUP_TIMEOUT_SECONDS,
        )
        elapsed = time.perf_counter() - start
        if resp.status_code == 200:
            logger.info(f"✅ Warm-up OK: {model} ({elapsed:.1f}s)")
            return True
        else:
            logger.warning(f"⚠️  Warm-up HTTP {resp.status_code}: {model}")
            return False
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - start
        logger.error(f"❌ Warm-up timeout ({elapsed:.1f}s): {model}")
        return False
    except Exception as e:
        logger.error(f"❌ Warm-up error: {model} — {e}")
        return False


async def ping_model(model: str, client: httpx.AsyncClient) -> bool:
    """
    Keep-alive ping: refresca el TTL del modelo en Ollama.

    Flujo:
      1. /api/show → verifica que el modelo existe (no lo carga, instantáneo)
      2. /api/generate → carga/recarga el modelo y resetea el TTL

    Si Ollama descargó el modelo por inactividad, el paso 2 debe
    recargarlo desde disco (8.4GB para qwen2.5-coder:14b → 30-90s).
    Por eso el timeout es igual al de warm-up (120s), no 30s.
    """
    # ═══════════════════════════════════════════════════════════════
    # ✅ FIX VRAM LOCK: No hacer ping si RAC está corriendo
    # ═══════════════════════════════════════════════════════════════
    if _VRAM_LOCK_AVAILABLE and is_vram_locked():
        # No loguear warning para no spamear cada 4 min, solo debug
        logger.debug(f"🔒 Keep-alive cancelado — VRAM bloqueada por {get_vram_lock_reason()}")
        return False

    try:
        # Paso 1: verificar que el modelo existe en Ollama
        resp = await client.post(
            f"{_OLLAMA_HOST}/api/show",
            json={"name": model},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning(f"⚠  Keep-alive: modelo no encontrado: {model}")
            return False

        # Paso 2: generar 1 token + keep_alive para resetear TTL
        # Este paso RECARGA el modelo si fue descargado de memoria
        resp2 = await client.post(
            f"{_OLLAMA_HOST}/api/generate",
            json={
                "model": model,
                "prompt": WARMUP_PROMPT,
                "stream": False,
                "options": {"num_predict": 1, "temperature": 0.0},
                "keep_alive": "10m",
            },
            timeout=KEEPALIVE_TIMEOUT_SECONDS,
        )
        if resp2.status_code == 200:
            logger.debug(f"🔄 Keep-alive OK: {model}")
            return True
        else:
            logger.warning(f"⚠  Keep-alive HTTP {resp2.status_code}: {model}")
            return False
    except httpx.TimeoutException:
        logger.warning(f"⚠  Keep-alive timeout ({KEEPALIVE_TIMEOUT_SECONDS}s): {model} — modelo demasiado grande para recargar a tiempo")
        return False
    except Exception as e:
        logger.warning(f"⚠  Keep-alive error: {model} — {repr(e)}")
        return False


async def get_loaded_models(client: httpx.AsyncClient) -> List[str]:
    """Retorna modelos actualmente cargados en memoria según Ollama."""
    try:
        resp = await client.get(f"{_OLLAMA_HOST}/api/ps", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 warmup_models.py:201] excepción degradada (intencional): {_d2e}")
    return []


async def ollama_available(client: httpx.AsyncClient) -> bool:
    """Verifica que Ollama esté corriendo."""
    try:
        resp = await client.get(f"{_OLLAMA_HOST}/api/tags", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


# ── Comandos públicos ─────────────────────────────────────────────────────────

async def warm_up_all(models: Optional[List[str]] = None) -> dict:
    """
    Warm-up de todos los modelos configurados.
    Retorna dict con resultados por modelo.

    Llamar desde main.py antes de inicializar el Dispatcher:
        from tools.warmup_models import warm_up_all
        await warm_up_all()

    ✅ v1.3.0: Solo calienta MODELS_TO_WARM (default model).
    El modelo de código se carga on-demand vía ModelSwap.ensure().
    Después del warmup exitoso, registra el modelo en ModelSwap
    para que ModelSwap sepa qué está en VRAM sin consultar /api/ps.
    """
    models = models or MODELS_TO_WARM
    results = {}

    # ═══════════════════════════════════════════════════════════════
    # ✅ FIX VRAM LOCK: Esperar a que se libere la VRAM si está bloqueada
    # ═══════════════════════════════════════════════════════════════
    if _VRAM_LOCK_AVAILABLE and is_vram_locked():
        reason = get_vram_lock_reason()
        logger.warning(f"🔒 Warm-up pausado — VRAM bloqueada por {reason}. Esperando liberación...")
        # Esperar hasta 10 minutos a que RAC termine
        await asyncio.to_thread(wait_for_vram_unlock, timeout=600.0)
        logger.info("🔓 VRAM liberada. Continuando warm-up...")

    async with httpx.AsyncClient() as client:
        if not await ollama_available(client):
            logger.error("❌ Ollama no disponible en %s", _OLLAMA_HOST)
            return {"error": "ollama_unavailable"}

        loaded = await get_loaded_models(client)
        logger.info(f"Modelos ya en memoria: {loaded or 'ninguno'}")

        # Warm-up SECUENCIAL — evita OOM con <16GB RAM
        results_list = []
        for m in models:
            result = await warm_up_model(m, client)
            results_list.append(result)
            # Pausa entre modelos para que el OS libere cache de páginas
            await asyncio.sleep(2)

        for model, result in zip(models, results_list):
            results[model] = result is True

    # ═══════════════════════════════════════════════════════════════
    # ✅ v1.3.0: Registrar modelo cargado en ModelSwap
    # ModelSwap es el árbitro de VRAM — necesita saber qué modelo
    # está cargado después del warmup para que ensure() funcione
    # correctamente en llamadas posteriores.
    # register_loaded() es declarativo: NO hace API call,
    # solo actualiza _current_model internamente.
    # ═══════════════════════════════════════════════════════════════
    if any(v for v in results.values()):
        try:
            from src.core.model_swap import ModelSwap
            # Registrar el primer modelo que se cargó exitosamente
            loaded_model = None
            for model, success in results.items():
                if success:
                    loaded_model = model
                    break
            if loaded_model:
                ModelSwap.instance().register_loaded(loaded_model)
                logger.info(f"[WARMUP] ModelSwap registered: {loaded_model}")
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 warmup_models.py:283] excepción degradada (intencional): {_d2e}")

    return results


async def keep_alive_daemon(
    models: Optional[List[str]] = None,
    interval: int = KEEPALIVE_INTERVAL_SECONDS,
) -> None:
    """
    Loop infinito que hace ping a los modelos cada `interval` segundos.
    Lanzar como background task: asyncio.create_task(keep_alive_daemon())

    ✅ v1.3.0: Solo pingea el modelo que ModelSwap dice está en VRAM.
    Antes pingeaba TODOS los modelos configurados, lo que causaba
    que Ollama recargara el modelo de código si había expirado,
    resultando en 2 modelos en VRAM simultáneamente (~13GB > ~8-12GB).

    Ahora consulta ModelSwap.current_model y solo mantiene vivo
    ese modelo. Si ModelSwap no está disponible, fallback al
    comportamiento anterior (pingear models parameter).
    """
    models = models or MODELS_TO_WARM
    logger.info(
        f"🔄 Keep-alive daemon iniciado — intervalo: {interval}s"
    )

    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(interval)

            # ═══════════════════════════════════════════════════════════════
            # ✅ FIX VRAM LOCK: Saltar ciclo si RAC está corriendo
            # ═══════════════════════════════════════════════════════════════
            if _VRAM_LOCK_AVAILABLE and is_vram_locked():
                reason = get_vram_lock_reason()
                logger.info(f"🔒 Keep-alive omitido — VRAM bloqueada por {reason}. Próximo intento en {interval}s.")
                continue

            if not await ollama_available(client):
                logger.warning("⚠️  Ollama no disponible — reintentando en %ds", interval)
                continue

            # ═══════════════════════════════════════════════════════════════
            # ✅ v1.3.0: Solo pingear el modelo que ModelSwap dice está en VRAM
            #
            # ANTES (v1.2.1):
            #   tasks = [ping_model(m, client) for m in models]
            #   → Pingeaba default + code → si code expiró, Ollama lo
            #     recarga → 2 modelos en VRAM → OOM
            #
            # DESPUÉS (v1.3.0):
            #   current = ModelSwap.instance().current_model
            #   → Solo pingea el modelo que debería estar → 1 modelo en VRAM
            # ═══════════════════════════════════════════════════════════════
            try:
                from src.core.model_swap import ModelSwap
                current = ModelSwap.instance().current_model
                models_to_ping = [current] if current else []
            except ImportError:
                models_to_ping = models

            if not models_to_ping:
                logger.debug("Keep-alive: no model to ping (VRAM empty?)")
                continue

            tasks = [ping_model(m, client) for m in models_to_ping]
            await asyncio.gather(*tasks, return_exceptions=True)


async def print_status() -> None:
    """Muestra modelos cargados en Ollama actualmente."""
    async with httpx.AsyncClient() as client:
        if not await ollama_available(client):
            print(f"❌ Ollama no disponible en {_OLLAMA_HOST}")
            return
        loaded = await get_loaded_models(client)
        print(f"\n{'='*50}")
        print(f"Ollama: {_OLLAMA_HOST}")
        print(f"Modelos en memoria: {len(loaded)}")
        for m in loaded:
            status = "✅" if m in ALL_CONFIGURED_MODELS else "  "
            print(f"  {status} {m}")

        # ═══════════════════════════════════════════════════════════════
        # ✅ v1.3.0: Mostrar modelos no cargados con explicación
        # Antes mostraba ❌ como si fueran un error. Ahora indica
        # claramente que se cargan on-demand vía ModelSwap.
        # ═══════════════════════════════════════════════════════════════
        not_loaded = [m for m in ALL_CONFIGURED_MODELS if m not in loaded]
        if not_loaded:
            print(f"\nModelos configurados no cargados (on-demand vía ModelSwap):")
            for m in not_loaded:
                print(f"  ⏳ {m} (se carga automáticamente al necesitarlo)")

        # ═══════════════════════════════════════════════════════════════
        # ✅ v1.3.0: Mostrar estado de ModelSwap
        # ═══════════════════════════════════════════════════════════════
        try:
            from src.core.model_swap import ModelSwap
            ms = ModelSwap.instance()
            status = ms.get_status()
            print(f"\nModelSwap status:")
            print(f"  Current: {status['current_model']}")
            print(f"  Default: {status['default_model']}")
            if status.get('last_swap_ago_s') is not None:
                print(f"  Last swap: {status['last_swap_ago_s']}s ago")
            print(f"  Swap in progress: {status['swap_in_progress']}")
        except ImportError:
            print(f"\n⚠️  ModelSwap no disponible (v1.0.6 no integrado)")

        print(f"{'='*50}\n")


# ── CLI ──────────────────────────────────────────────────────────────────────

async def async_main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="AXIOMA — Model Warm-up & Keep-alive")
    parser.add_argument("--daemon", action="store_true",
                        help="Warm-up + keep-alive loop continuo")
    parser.add_argument("--status", action="store_true",
                        help="Muestra modelos actualmente cargados")
    parser.add_argument("--interval", type=int, default=KEEPALIVE_INTERVAL_SECONDS,
                        help=f"Intervalo keep-alive en segundos (default: {KEEPALIVE_INTERVAL_SECONDS})")
    args = parser.parse_args()

    if args.status:
        await print_status()
        return 0

    print(f"\n{'='*50}")
    print("AXIOMA — Model Warm-up")
    print(f"Host: {_OLLAMA_HOST}")
    print(f"Modelos: {MODELS_TO_WARM}")
    print(f"{'='*50}\n")

    results = await warm_up_all()

    if "error" in results:
        return 1

    success = sum(1 for v in results.values() if v)
    print(f"\n✅ {success}/{len(results)} modelos listos\n")

    if args.daemon:
        print(f"🔄 Iniciando keep-alive cada {args.interval}s — Ctrl+C para salir\n")
        try:
            await keep_alive_daemon(interval=args.interval)
        except KeyboardInterrupt:
            print("\nKeep-alive detenido.")

    return 0 if success == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(async_main()))
