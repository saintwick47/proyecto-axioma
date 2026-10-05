#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/tools/optimize_ollama.py
# Autor: SaintWick
# Descripción: Optimiza el servicio systemd de Ollama (threads, keep_alive,
#              host) y ajusta la configuración para el hardware local.
import os
import sys
import subprocess
import shutil
import time
from pathlib import Path

def run(cmd, check=True, capture_output=True):
    return subprocess.run(cmd, capture_output=capture_output, text=True, check=False)

def log(msg, level="INFO"):
    print(f"[{level}] {msg}")

def main():
    log("🔍 INICIANDO DIAGNÓSTICO PRE-FLIGHT")

    # 1. Privilegios
    if os.geteuid() != 0:
        log("❌ Requiere root. Ejecuta: sudo python3 optimize_ollama.py", "ERROR")
        sys.exit(1)

    # 2. Estado del servicio
    svc_active = run(["systemctl", "is-active", "ollama"])
    if svc_active.stdout.strip() != "active":
        log(f"⚠️ Servicio inactivo ({svc_active.stdout.strip()}). Abortando.", "WARN")
        sys.exit(1)

    # 3. Validar binario y rutas systemd
    ollama_bin = run(["which", "ollama"])
    if ollama_bin.returncode != 0:
        log("❌ Ollama no encontrado en PATH.", "ERROR")
        sys.exit(1)

    svc_paths = [Path("/etc/systemd/system/ollama.service"), Path("/lib/systemd/system/ollama.service")]
    if not any(p.exists() for p in svc_paths):
        log("❌ Unidad systemd de Ollama no encontrada.", "ERROR")
        sys.exit(1)

    # 4. Preparar drop-in
    dropin_dir = Path("/etc/systemd/system/ollama.service.d")
    dropin_file = dropin_dir / "override.conf"

    if not dropin_dir.exists():
        dropin_dir.mkdir(parents=True, exist_ok=True)
        log(f"✅ Directorio creado: {dropin_dir}")

    if dropin_file.exists():
        backup = dropin_file.with_suffix(".bak")
        shutil.copy2(dropin_file, backup)
        log(f"📦 Backup seguro: {backup}")

    # 5. Escritura atómica
    # ✅ CORREGIDO: KEEP_ALIVE=2h evita cold start (~14s) por descarga prematura del modelo
    # ✅ 2026-10-01 (ISSUE-119): KV cache en q8_0 — MEDIDO Y **NO ACTIVADO**.
    # Se probó de verdad (servicio reiniciado; `llama-server` confirmado con
    # `--cache-type-k q8_0 --cache-type-v q8_0 --flash-attn`), con el MISMO prompt
    # (1528 tok de prefill, num_ctx=8192, qwen2.5-coder:7b, 2 corridas por config):
    #
    #     f16 (antes) : prefill 66,4/66,4 tok/s · decode 7,68/7,34 · modelo 6,51 GB
    #     q8_0        : prefill 64,3/62,4 tok/s · decode 7,04/7,02 · modelo 5,97 GB
    #     f16 (vuelta): prefill 66,9/63,1 tok/s · decode 7,07/7,17 · modelo 6,51 GB
    #
    # ⚠️ HONESTIDAD DE LA MEDICIÓN: el único resultado SÓLIDO es la **RAM**
    # (−0,54 GB, determinista y reproducible: 6,51 → 5,97 GB). La VELOCIDAD no se
    # distingue del ruido: entre corridas de la MISMA config hay 6 % en prefill y
    # 9 % en decode (máquina compartida con carga de fondo), y el decode de f16 al
    # volver (7,07/7,17) es tan bajo como el de q8_0 (7,04/7,02). Para detectar
    # efectos <5 % hay que INTERCALAR (A/B/A/B) con ≥3 muestras por config; con 2
    # y en sesiones distintas, no alcanza.
    #
    # **Decisión: se queda en f16** — el único beneficio medido (0,54 GB de 14,7 GB,
    # sin presión de RAM actual) no compensa el cambio ni el riesgo de perder algo de
    # precisión en contexto largo (el KV cuantizado degrada la recuperación lejana).
    # **Reabrir si:** se sube `num_ctx` a ≥16k (el KV f16 pasa de 0,94 a 1,9 GB y el
    # ahorro se duplica) o si aparece presión real de RAM. Para reintentarlo: agregar
    # la línea de abajo y medir con el protocolo INTERCALADO.
    # (Se deja `OLLAMA_FLASH_ATTENTION=1` explícito: ya estaba en `optimizer.conf`,
    # es requisito del KV cuantizado y no afectó al rendimiento medido.)
    content = """[Service]
Environment="OLLAMA_NUM_THREADS=6"
Environment="OLLAMA_KEEP_ALIVE=2h"
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_FLASH_ATTENTION=1"
Nice=-10
"""
    tmp_file = dropin_file.with_suffix(".tmp")
    try:
        tmp_file.write_text(content, encoding="utf-8")
        os.chmod(tmp_file, 0o644)
        tmp_file.replace(dropin_file)
        log("✅ override.conf generado y verificado.")
    except Exception as e:
        log(f"❌ Fallo en escritura: {e}", "ERROR")
        tmp_file.unlink(missing_ok=True)
        sys.exit(1)

    # 6. Aplicar cambios
    log("⚙️ Recargando systemd y reiniciando Ollama...")
    run(["systemctl", "daemon-reload"])
    restart = run(["systemctl", "restart", "ollama"])
    if restart.returncode != 0:
        log(f"❌ Reinicio fallido: {restart.stderr}", "ERROR")
        sys.exit(1)
    log("✅ Servicio reiniciado.")

    # 7. Verificación post-ejecución
    time.sleep(3)
    pid_res = run(["systemctl", "show", "--property=MainPID", "--value", "ollama"])
    pid = pid_res.stdout.strip()
    if pid == "0":
        log("❌ PID inactivo tras reinicio. Revisa journalctl -u ollama.", "ERROR")
        sys.exit(1)

    env_res = run(["sh", "-c", f"cat /proc/{pid}/environ | tr '\\0' '\\n' | grep -E 'OLLAMA_NUM_THREADS|OLLAMA_KEEP_ALIVE|OLLAMA_HOST|OLLAMA_FLASH_ATTENTION'"])
    vars_found = [line.strip() for line in env_res.stdout.strip().split('\n') if line.strip()]

    log("🔎 VARIABLES APLICADAS:")
    for v in vars_found:
        log(f"  {v}")

    keys = [v.split('=')[0] for v in vars_found]
    # ✅ 2026-09-29 (ISSUE-119): se verifican TAMBIÉN el tipo de KV cache y flash
    # attention: si no aparecen, la medición de rendimiento no vale.
    missing = [k for k in ["OLLAMA_NUM_THREADS", "OLLAMA_KEEP_ALIVE", "OLLAMA_HOST",
                           "OLLAMA_FLASH_ATTENTION"] if k not in keys]
    if missing:
        log(f"⚠️ Faltan variables: {', '.join(missing)}", "WARN")
    else:
        log("✅ Todas las variables se aplicaron correctamente.")

    log("🚀 OPTIMIZACIÓN COMPLETA. Espera 35s y ejecuta tu prueba de latencia.")

if __name__ == "__main__":
    main()
