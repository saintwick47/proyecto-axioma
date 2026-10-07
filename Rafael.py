#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Rafael: entry point del daemon autónomo
# Ruta: /ruta/a/axioma/Rafael.py
# Uso: python Rafael.py --mode jarvis [--mode jarvis|voice_only|silent]
#
# Doble modo:
#   - Bajo systemd (INVOCATION_ID en env): corre el daemon real.
#   - Ejecución manual: NO instancia el daemon en este proceso —
#     solo hace `systemctl --user start` y sigue los logs. El daemon
#     real corre en el proceso que systemd lanza para el service.
#     Evita doble instancia (dos pipelines de voz simultáneos).
#   - **Modo directo** (`--directo` o `AXIOMA_RAFAEL_DIRECTO=1`, `ISSUE-150`): corre el daemon en
#     ESTE proceso, sin systemd. Es lo que hace falta para el contenedor (no hay `systemctl`) y para
#     cualquier sistema sin systemd. Medido: sin esto el daemon abortaba con
#     "No se pudo start rafael.service: [Errno 2] No such file or directory: 'systemctl'".
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging

import argparse
import asyncio
import os
import signal
import sys
from typing import Optional
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ✅ 2026-10-01 (ISSUE-125): si se corre con el Python del SISTEMA en vez del venv,
# el error nativo es un `ModuleNotFoundError` entre decenas de imports (parece un
# bug del código). La implementación ÚNICA vive en `src/utils/degradation.py`
# (ahí y no duplicada: el auditor marca MEDIUM el código duplicado).
from src.utils.degradation import verificar_interprete as _verificar_interprete
_verificar_interprete("Rafael.py")

from src.core.rafael_daemon import RafaelDaemon, setup_logging

SERVICE_NAME = "rafael.service"


def _is_running_under_systemd() -> bool:
    """Detecta si este proceso fue lanzado por systemd (vs ejecución manual)."""
    return "INVOCATION_ID" in os.environ


def _modo_directo(argv: Optional[list] = None) -> bool:
    """✅ `ISSUE-150`: correr el daemon ACÁ, sin systemd (contenedor u otro sistema).

    Se activa con `--directo` o con `AXIOMA_RAFAEL_DIRECTO=1`. En el contenedor no existe
    `systemctl`, así que el camino "manual" (que arranca la unidad de usuario) no puede usarse:
    medido, el daemon abortaba. En modo directo el proceso ES el daemon (y en el contenedor es el
    proceso principal, así que se detiene con el contenedor).
    """
    args = sys.argv[1:] if argv is None else argv
    return "--directo" in args or os.environ.get("AXIOMA_RAFAEL_DIRECTO") == "1"


def _sin_audio(entorno: Optional[dict] = None) -> bool:
    """`AXIOMA_SIN_AUDIO=1`: el equipo (o el contenedor) no tiene audio utilizable."""
    from config.multimodal_loader import audio_deshabilitado
    return audio_deshabilitado(entorno)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rafael — daemon autónomo de AXIOMA")
    parser.add_argument(
        "--mode",
        choices=["jarvis", "voice_only", "silent"],
        default="jarvis",
        help="Modo de operación del daemon (default: jarvis)",
    )
    parser.add_argument(
        "--directo", action="store_true",
        help="Correr el daemon en este proceso, sin systemd (contenedores y sistemas sin systemd)",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="✅ plan_rafael (2026-08-31): muestra el resumen de los últimos turnos "
             "de voz desde logs/rafael.log (métricas de VoicePipelineStats por turno)",
    )
    return parser.parse_args()


def _show_stats() -> None:
    """Resume los turnos de voz recientes registrados en logs/rafael.log."""
    from config.paths import Paths
    log_file = Paths.LOGS / "rafael.log"
    if not log_file.exists():
        print("📭 rafael.log no existe todavía — no hay turnos registrados.")
        return
    lines = [
        ln.strip() for ln in log_file.read_text(encoding="utf-8").splitlines()
        if "[VoicePipeline] turno" in ln
    ]
    if not lines:
        print("📭 No hay turnos de voz registrados en rafael.log.")
        return
    print(f"🎤 Últimos {len(lines)} turnos de voz:\n")
    for ln in lines[-15:]:
        # extraer la parte después de '[VoicePipeline] '
        idx = ln.find("[VoicePipeline] ")
        if idx >= 0:
            print("  " + ln[idx + len("[VoicePipeline] "):])
    print(f"\n📄 Archivo: {log_file}")


async def _run_daemon(mode: str) -> None:
    """Ejecuta el daemon real. Solo debe correr bajo systemd."""
    daemon = RafaelDaemon(mode=mode)
    loop = asyncio.get_running_loop()
    main_task = asyncio.ensure_future(daemon.run())

    def _request_shutdown() -> None:
        main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 Rafael.py:87] excepción degradada (intencional): {_d2e}")

    try:
        await main_task
    except asyncio.CancelledError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 Rafael.py:92] excepción degradada (intencional): {_d2e}")


async def _run_systemctl(action: str) -> bool:
    """systemctl --user <action> rafael.service, sin bloquear el event loop."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", action, SERVICE_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10.0)
        return proc.returncode == 0
    except Exception as e:
        print(f"⚠️  No se pudo {action} {SERVICE_NAME}: {e}", file=sys.stderr)
        return False


async def _run_manual_control() -> None:
    """
    Ejecución manual: arranca el service, sigue sus logs con journalctl,
    y lo detiene al recibir Ctrl+C. El daemon corre en el proceso que
    systemd instancia, no en este.
    """
    print(f"🚀 Ejecución manual detectada — iniciando {SERVICE_NAME}...")
    if not await _run_systemctl("start"):
        print(f"❌ No se pudo iniciar {SERVICE_NAME}. Abortando.", file=sys.stderr)
        return

    print(f"✅ {SERVICE_NAME} iniciado — logs en vivo (Ctrl+C para detener)\n")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 Rafael.py:133] excepción degradada (intencional): {_d2e}")

    log_proc = await asyncio.create_subprocess_exec(
        "journalctl", "--user", "-u", SERVICE_NAME, "-f", "-n", "0",
    )

    try:
        await stop_event.wait()
    finally:
        if log_proc.returncode is None:
            log_proc.terminate()
            try:
                await asyncio.wait_for(log_proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log_proc.kill()

        print(f"\n🛑 Deteniendo {SERVICE_NAME}...")
        if await _run_systemctl("stop"):
            print(f"✅ {SERVICE_NAME} detenido")
        else:
            print(f"⚠️  No se pudo detener {SERVICE_NAME}", file=sys.stderr)


async def _main() -> None:
    args = _parse_args()
    if args.stats:
        _show_stats()
        return
    setup_logging()

    if _sin_audio():
        print("⚠️  AXIOMA_SIN_AUDIO=1: no hay audio utilizable en este entorno.")
        print("    El daemon de voz no puede arrancar (el chat y el texto funcionan igual).")
        print("    Si estás en el contenedor, la voz necesita el perfil `voz` (Linux) o correr fuera de él.")
        return 4
    if _is_running_under_systemd() or args.directo or _modo_directo():
        # Bajo systemd, o en modo directo (contenedor): el daemon corre en ESTE proceso.
        await _run_daemon(args.mode)
    else:
        await _run_manual_control()


if __name__ == "__main__":
    asyncio.run(_main())
