#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Post-Update: actualización del sistema + re-optimización
# Ruta: /ruta/a/axioma/tools/post_update.py
# Autor: SaintWick
# Fecha: 2026-08-27
# Descripción: Automatiza el flujo post-`pacman -Syu`:
#   Fase 1 (interactiva): pregunta si actualizar (sudo pacman -Syu, la
#     contraseña la escribís vos), y si reiniciar. Antes de reiniciar
#     instala el auto-arranque de la Fase 2.
#   Fase 2 (--post-reboot): al iniciar sesión ejecuta automáticamente
#     tools/axioma_optimizer.sh --status y --apply, más
#     `systemctl status ollama`, analiza problemas y sugiere la acción.
#     Se auto-limpia (quita flag + hooks) al terminar.
#
# Uso:
#   python tools/post_update.py                # Fase 1 (interactiva)
#   python tools/post_update.py --post-reboot  # Fase 2 (tras el reboot)
#   python tools/post_update.py --help
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Rutas ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent          # .../tools
PROJECT_ROOT = SCRIPT_DIR.parent                     # .../axioma
OPTIMIZER = SCRIPT_DIR / "axioma_optimizer.sh"
FLAG_FILE = Path.home() / ".config" / "axioma" / "post_update.flag"
BASHRC = Path.home() / ".bashrc"
AUTOSTART_DIR = Path.home() / ".config" / "autostart"
DESKTOP_FILE = AUTOSTART_DIR / "axioma-post-update.desktop"
LOG_DIR = PROJECT_ROOT / "logs"

# Línea del hook en ~/.bashrc (única, se identifica por su marca única)
BASHRC_MARK = "# AXIOMA post-update hook (auto-removible)"
BASHRC_LINE = (
    f'[ -f "$HOME/.config/axioma/post_update.flag" ] && '
    f'python3 "{SCRIPT_DIR / "post_update.py"}" --post-reboot || true'
)

TERMINALS = ["konsole", "gnome-terminal", "xfce4-terminal", "kitty", "alacritty", "xterm"]


# ============================================================================
# UTILIDADES
# ============================================================================

# ✅ 2026-08-29: un solo ts por corrida — antes log() calculaba el ts en cada
# llamada y una ejecución generaba N archivos post_update_<ts>.log (uno por
# línea), dejando logs incompletos. Ahora cada corrida escribe UN archivo.
_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str = "") -> None:
    """Escribe a stdout (y a logs/post_update_<ts>.log de esta corrida)."""
    print(msg, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"post_update_{_RUN_TS}.log", "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 post_update.py:71] excepción degradada (intencional): {_d2e}")


def run(cmd: list, interactive: bool = False) -> subprocess.CompletedProcess:
    """Ejecuta un comando. interactive=True hereda TTY (sudo pide contraseña)."""
    kwargs = {}
    if interactive:
        kwargs = {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
    else:
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                  "text": True, "encoding": "utf-8", "errors": "replace"}
    return subprocess.run(cmd, **kwargs)


def ask(question: str, default: str = "n") -> bool:
    """Pregunta s/n. default: 'n' → Enter = no; 's' → Enter = sí."""
    hint = "s/N" if default != "s" else "S/n"
    try:
        resp = input(f"  {question} [{hint}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default == "s"
    if not resp:
        return default == "s"
    return resp in ("s", "y", "si", "yes")


# ============================================================================
# HOOKS DE AUTO-ARRANQUE (Fase 2)
# ============================================================================

def install_autostart() -> None:
    """Instala el auto-arranque de la Fase 2: hook en ~/.bashrc + .desktop."""
    # 1) Hook en ~/.bashrc (garantiza TTY → sudo puede pedir contraseña)
    try:
        content = BASHRC.read_text(encoding="utf-8") if BASHRC.exists() else ""
        if BASHRC_MARK not in content:
            with open(BASHRC, "a", encoding="utf-8") as fh:
                fh.write(f"\n{BASHRC_MARK}\n{BASHRC_LINE}\n")
            log(f"  ✓ Hook instalado en {BASHRC}")
        else:
            log(f"  · Hook ya presente en {BASHRC}")
    except OSError as e:
        log(f"  ⚠️ No se pudo escribir {BASHRC}: {e}")

    # 2) Autostart de escritorio (abre una terminal con la Fase 2)
    term = next((t for t in TERMINALS if shutil.which(t)), None)
    if term:
        try:
            AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
            cmd = f"{term} -e python3 {SCRIPT_DIR / 'post_update.py'} --post-reboot"
            if term == "konsole":
                cmd = f"konsole -e python3 {SCRIPT_DIR / 'post_update.py'} --post-reboot"
            elif term == "gnome-terminal":
                cmd = f"gnome-terminal -- python3 {SCRIPT_DIR / 'post_update.py'} --post-reboot"
            elif term == "kitty":
                cmd = f"kitty python3 {SCRIPT_DIR / 'post_update.py'} --post-reboot"
            elif term == "alacritty":
                cmd = f"alacritty -e python3 {SCRIPT_DIR / 'post_update.py'} --post-reboot"
            DESKTOP_FILE.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name=AXIOMA Post-Update\n"
                f"Exec={cmd}\n"
                "X-GNOME-Autostart-enabled=true\n",
                encoding="utf-8",
            )
            log(f"  ✓ Autostart instalado ({term})")
        except OSError as e:
            log(f"  ⚠️ No se pudo instalar autostart: {e}")
    else:
        log("  ⚠️ Sin terminal detectada — la Fase 2 correrá al abrir cualquier terminal (hook .bashrc)")


def uninstall_autostart() -> None:
    """Quita hook de .bashrc y autostart (auto-limpieza al terminar la Fase 2)."""
    try:
        if BASHRC.exists():
            lines = BASHRC.read_text(encoding="utf-8").splitlines()
            cleaned = [l for l in lines if l != BASHRC_LINE and l != BASHRC_MARK]
            # quitar la línea en blanco sobrante si quedó al final
            while cleaned and cleaned[-1] == "":
                cleaned.pop()
            BASHRC.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
            log("  ✓ Hook de .bashrc removido")
    except OSError as e:
        log(f"  ⚠️ No se pudo limpiar {BASHRC}: {e}")
    try:
        if DESKTOP_FILE.exists():
            DESKTOP_FILE.unlink()
            log("  ✓ Autostart removido")
    except OSError as e:
        log(f"  ⚠️ No se pudo remover autostart: {e}")


def set_flag() -> None:
    FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAG_FILE.write_text(datetime.now().isoformat(), encoding="utf-8")


def clear_flag() -> None:
    try:
        if FLAG_FILE.exists():
            FLAG_FILE.unlink()
    except OSError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 post_update.py:175] excepción degradada (intencional): {_d2e}")


# ============================================================================
# ANÁLISIS DE PROBLEMAS (Fase 2)
# ============================================================================

def analyze_output(status_out: str, apply_out: str, ollama_out: str) -> list:
    """Analiza el output de los 3 comandos → lista de (tipo, mensaje, accion)."""
    issues: list = []
    s = status_out or ""
    a = apply_out or ""
    o = ollama_out or ""

    # ── Status: scheduler ─────────────────────────────────────────
    if re.search(r"Scheduler:\s*none", s):
        issues.append(("AVISO", "Scheduler scx NO activo (CFS default).",
                       "Revisar por qué --apply no lo reactivó (suele requerir reinicio o config scx)."))
    elif re.search(r"Scheduler:\s*scx", s):
        pass  # OK
    # ── Status: override Ollama ───────────────────────────────────
    if "Variables en override pero NO aplicadas" in s:
        issues.append(("AVISO", "Variables del override de Ollama no cargadas en el proceso.",
                       "sudo systemctl restart ollama"))
    if "Proceso Ollama no detectado" in s:
        issues.append(("PROBLEMA", "El proceso Ollama no está corriendo.",
                       "sudo systemctl start ollama && sudo systemctl enable ollama"))
    # ── Status: elementos faltantes ───────────────────────────────
    for label in ["Sysctl", "Ananicy rules"]:
        if f"{label}:" in s and "no configurado" in s.split(label)[1][:60]:
            issues.append(("PROBLEMA", f"{label} no configurado tras el update.",
                           "Revisar --apply (debió re-aplicarlo)."))
    # ── Apply: errores ────────────────────────────────────────────
    for pattern, msg, accion in [
        (r"ERROR", "El optimizador reportó errores durante --apply.",
         "Revisar logs/optimizer_*.log y ejecutar --apply manualmente con sudo."),
        (r"fall[oó]", "Algo falló durante --apply.", "Revisar logs/optimizer_*.log."),
        (r"denied|permiso denegado", "Problema de permisos en --apply.",
         "Ejecutar: sudo ./tools/axioma_optimizer.sh --apply"),
    ]:
        if re.search(pattern, a, re.IGNORECASE):
            issues.append(("PROBLEMA", msg, accion))
            break
    # ── Ollama status ─────────────────────────────────────────────
    if re.search(r"Active:\s*failed", o):
        issues.append(("CRÍTICO", "El servicio Ollama quedó FAILED.",
                       "journalctl -u ollama --no-pager -n 50"))
    elif re.search(r"Active:\s*inactive", o):
        issues.append(("PROBLEMA", "Ollama está inactivo (no corriendo).",
                       "sudo systemctl start ollama"))
    elif re.search(r"Active:\s*active \(running\)", o):
        pass  # OK

    if not issues:
        issues.append(("OK", "Sin problemas detectados — sistema y Ollama OK.", ""))
    return issues


def print_summary(issues: list) -> None:
    log("\n" + "=" * 60)
    log("📋 RESUMEN POST-ACTUALIZACIÓN")
    log("=" * 60)
    for tipo, msg, accion in issues:
        icono = {"OK": "✅", "AVISO": "⚠️", "PROBLEMA": "❌", "CRÍTICO": "🚨"}.get(tipo, "•")
        log(f"  {icono} [{tipo}] {msg}")
        if accion:
            log(f"       → {accion}")


# ============================================================================
# FASE 1 — Interactiva (actualizar + reiniciar)
# ============================================================================

def fase1() -> int:
    log("=" * 60)
    log("🔄 AXIOMA POST-UPDATE — Fase 1 (actualización del sistema)")
    log("=" * 60)
    log(f"  Proyecto: {PROJECT_ROOT}")
    log(f"  Optimizador: {OPTIMIZER}")
    log("")

    # ── 1. ¿Actualizar? ────────────────────────────────────────────
    if not ask("¿Actualizar el sistema ahora? (sudo pacman -Syu)", default="s"):
        log("\n  Ok, sin actualización. Podés ejecutar la Fase 1 de nuevo cuando quieras.")
        return 0

    if not shutil.which("pacman"):
        log("  ❌ pacman no encontrado — esto no es Arch/CachyOS.")
        return 1

    log("\n  ▶ Ejecutando: sudo pacman -Syu (escribí tu contraseña cuando la pida)\n")
    res = run(["sudo", "pacman", "-Syu"], interactive=True)
    log("")
    if res.returncode != 0:
        log(f"  ❌ pacman -Syu terminó con código {res.returncode}. Revisá el error arriba.")
        return res.returncode

    log("  ✅ Actualización completada correctamente.")
    log("  ⚠️ Si hubo actualización de kernel, es OBLIGATORIO reiniciar.\n")

    # ── 2. ¿Reiniciar? ─────────────────────────────────────────────
    if not ask("¿Reiniciar ahora? (la Fase 2 correrá automáticamente al volver)", default="s"):
        log("\n  Ok, sin reinicio. Cuando reinicies, la Fase 2 correrá sola al abrir la terminal.")
        log("  Si querés ejecutarla manualmente: python tools/post_update.py --post-reboot")
        return 0

    log("\n  ▶ Instalando auto-arranque de la Fase 2...")
    set_flag()
    install_autostart()
    log("  ▶ Reiniciando...")
    run(["sudo", "systemctl", "reboot"], interactive=True)
    # Si llegamos acá, el reboot no se ejecutó (sudo cancelado)
    log("  ⚠️ El reinicio no se ejecutó (¿contraseña cancelada?). Reiniciá manualmente:")
    log("      sudo systemctl reboot")
    return 0


# ============================================================================
# FASE 2 — Post-reboot (auto-ejecución)
# ============================================================================

def fase2() -> int:
    log("=" * 60)
    log("🔄 AXIOMA POST-UPDATE — Fase 2 (post-reboot)")
    log("=" * 60)

    if not FLAG_FILE.exists():
        log("  · Sin flag de post-update pendiente — nada que hacer.")
        log("  · (La Fase 2 se ejecuta solo si la Fase 1 dejó el flag antes de reiniciar.)")
        return 0

    log(f"  Flag detectado: {FLAG_FILE}")
    log(f"  Fecha del flag: {FLAG_FILE.read_text(encoding='utf-8') if FLAG_FILE.exists() else '?'}")
    log("  Optimizador: " + str(OPTIMIZER))
    if not OPTIMIZER.exists():
        log("  ❌ No se encontró tools/axioma_optimizer.sh — ruta incorrecta.")
        clear_flag()
        uninstall_autostart()
        return 1
    log("")

    # ── 1. --status ────────────────────────────────────────────────
    log("▶ 1/3 tools/axioma_optimizer.sh --status\n")
    r1 = run(["bash", str(OPTIMIZER), "--status"])
    status_out = (r1.stdout or "") if isinstance(r1.stdout, str) else ""
    print(status_out, flush=True)

    # ── 2. --apply (requiere sudo — la contraseña la ponés acá) ──
    log("\n▶ 2/3 tools/axioma_optimizer.sh --apply (escribí tu contraseña si la pide)\n")
    r2 = run(["bash", str(OPTIMIZER), "--apply"], interactive=True)
    apply_out = ""
    if not isinstance(r2.stdout, str):
        # ejecución interactiva → stdout no capturado; intentar leer el log del optimizer
        log_dir = PROJECT_ROOT / "logs"
        if log_dir.exists():
            logs = sorted(log_dir.glob("optimizer_*.log"), key=lambda p: p.stat().st_mtime)
            if logs:
                try:
                    apply_out = logs[-1].read_text(encoding="utf-8", errors="replace")
                except OSError as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 post_update.py:335] excepción degradada (intencional): {_d2e}")
    else:
        apply_out = r2.stdout

    # ── 3. systemctl status ollama ─────────────────────────────────
    log("\n▶ 3/3 sudo systemctl status ollama --no-pager\n")
    r3 = run(["sudo", "systemctl", "status", "ollama", "--no-pager"], interactive=True)
    ollama_out = (r3.stdout or "") if isinstance(r3.stdout, str) else ""

    # ── Análisis y resumen ─────────────────────────────────────────
    issues = analyze_output(status_out, apply_out, ollama_out)
    print_summary(issues)

    # ── Limpieza ───────────────────────────────────────────────────
    log("\n  ▶ Limpieza de auto-arranque...")
    clear_flag()
    uninstall_autostart()
    log("  ✅ Listo. El flag y los hooks fueron removidos (no volverá a ejecutarse).")

    if any(t in ("PROBLEMA", "CRÍTICO") for t, _, _ in issues):
        log("\n  ⚠️ Hay problemas pendientes — seguí las acciones indicadas arriba.")
        return 2
    return 0


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="AXIOMA Post-Update: actualiza el sistema y re-aplica las optimizaciones.",
    )
    parser.add_argument("--post-reboot", action="store_true",
                        help="Fase 2: se ejecuta automáticamente tras el reinicio (no usar a mano salvo re-intento).")
    args = parser.parse_args()

    result = fase2() if args.post_reboot else fase1()

    # ✅ 2026-08-29: conservar solo el log post_update más reciente. Se poda
    # DESPUÉS de la corrida para que el log nuevo de esta ejecución exista y
    # sea el que quede (podar antes conservaba el viejo y dejaba 2 archivos).
    _prune_old_logs()
    return result


def _prune_old_logs(keep: int = 1) -> None:
    """Conserva solo los N logs post_update_*.log más recientes."""
    try:
        logs = sorted(
            LOG_DIR.glob("post_update_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in logs[keep:]:
            old.unlink(missing_ok=True)
    except OSError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 post_update.py:392] excepción degradada (intencional): {_d2e}")


if __name__ == "__main__":
    sys.exit(main())
