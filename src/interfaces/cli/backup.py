#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.0.1 — Interfaz CLI: BACKUP (hermano de main.py)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/cli/backup.py
# Autor: SaintWick
# Fecha: 2026-10-24
# Propósito: backup estructural y completo (JSON + .tar.gz) del estado de AXIOMA.
# ✅ PARTICIÓN 50/50 (2026-10-24): `main.py` había llegado a 1215 líneas (>1000)
# y la regla del proyecto es partir en EXACTAMENTE 2 archivos: el original y un
# hermano en la misma carpeta. Este hermano se lleva TODA la lógica del backup:
#   _BACKUP_MAX/_BACKUP_LOCK, _directorio_backups, _agrupar_backups,
#   _timestamp_unico, _escribir_json, _crear_tar, _aplicar_retencion, _pid_activo,
#   _lock_activo, _procesos_axioma, _avisar_si_axioma_corre, crear_backup_json,
#   cmd_backup — y los helpers COMPARTIDOS _log_cli_event/_log_error, que el
#   original re-importa con los MISMOS nombres (API pública intacta).
# Corte elegido: backup/persistencia vs. el resto del CLI (check/chat/voz/info).
# Dependencia en UN SOLO SENTIDO a nivel de módulo: main.py → backup.py (este
# módulo NO importa main a nivel de módulo; `_log_*` leen LOGGER_AVAILABLE y
# get_logger del ORIGINAL con import diferido dentro de la función, porque
# tests/test_axioma_23_observabilidad.py los parchea sobre `main` — mismo patrón
# que analyst y memory/gateway). Estado mutable compartido: ninguno propio; el
# único estado de módulo del backup (_BACKUP_MAX/_BACKUP_LOCK) es inmutable y
# vive acá. `settings`/`Paths` son los MISMOS objetos importados por main.py.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import logging
import os
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from config.paths import Paths
from config.settings import settings

if TYPE_CHECKING:  # pragma: no cover - solo para type checkers
    from .main import CLIConsole


def _log_cli_event(category: str, message: str, extra: dict = None,
                   level: str = "INFO"):
    """
    Helper para logging de eventos CLI.

    ✅ ISSUE-144 (MEDIDO 2026-10-04): faltaba el parámetro `level` y **12 llamadas**
    de este mismo módulo se lo pasaban (`level="ERROR"`/`"WARNING"`, justo en los
    caminos de error y aviso). El error de argumentos salta ANTES de entrar a la
    función, así que el `try/except` de acá no podía atraparlo: medido, con Ollama
    caído `main.py --check` imprimía `TypeError: _log_cli_event() got an unexpected
    keyword argument 'level'` en lugar de decir qué faltaba instalar. Ninguna prueba
    cubría esa función.

    Args:
        category: Categoría del evento (CLI, CHECK, CHAT, BACKUP, INFO)
        message: Mensaje descriptivo
        extra: Información adicional para el log
        level: Nivel del evento (INFO por defecto; ERROR/WARNING en los avisos)
    """
    import src.interfaces.cli.main as _orig
    if not _orig.LOGGER_AVAILABLE:
        return
    try:
        log = _orig.get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_event(category, message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:69] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra: dict = None):
    """
    Helper para logging de errores.

    Args:
        error: Excepción capturada
        context: Contexto donde ocurrió el error
        extra: Información adicional para el log
    """
    import src.interfaces.cli.main as _orig
    if not _orig.LOGGER_AVAILABLE:
        return
    try:
        log = _orig.get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:88] excepción degradada (intencional): {_d2e}")


# ═══════════════════════════════════════════════════════════════
# COMANDO: --backup
# ═══════════════════════════════════════════════════════════════
_BACKUP_MAX = 3
_BACKUP_LOCK = ("axioma.lock", ".axioma.lock", "axioma.pid", ".axioma.pid")


def _directorio_backups() -> Path:
    return Paths.BACKUPS if hasattr(Paths, "BACKUPS") else Paths.DATA / "backups"


def _agrupar_backups(backup_dir: Path) -> dict[str, dict[str, Path]]:
    """Agrupa por timestamp: {"20261024_120000": {"json": Path, "tar": Path}}.

    Sólo reconoce el nombre canónico `axioma_backup_<YYYYmmdd_HHMMSS>`. Un backup
    estructural sin `.tar.gz` cuenta igual: la retención rota el par completo.
    """
    grupos: dict[str, dict[str, Path]] = {}
    if not backup_dir.is_dir():
        return grupos
    for ruta in backup_dir.iterdir():
        nombre = ruta.name
        if not nombre.startswith("axioma_backup_"):
            continue
        resto = nombre[len("axioma_backup_"):]
        if resto.endswith(".json"):
            ts, clase = resto[:-len(".json")], "json"
        elif resto.endswith(".tar.gz"):
            ts, clase = resto[:-len(".tar.gz")], "tar"
        else:
            continue
        if len(ts) == 15 and ts[8] == "_" and ts.replace("_", "").isdigit():
            grupos.setdefault(ts, {})[clase] = ruta
    return grupos


def _timestamp_unico(backup_dir: Path) -> tuple[str, datetime]:
    """Timestamp del nombre, corrido en segundos si ya existe (no pisa un backup).

    Conserva el formato histórico `%Y%m%d_%H%M%S`; si dos comandos caen en el
    mismo segundo, el segundo usa el siguiente libre. Así la retención no puede
    borrar por "empate" el backup recién creado.
    """
    base = datetime.now().replace(microsecond=0)
    ocupados = _agrupar_backups(backup_dir)
    for delta in range(86400):
        momento = base + timedelta(seconds=delta)
        ts = momento.strftime("%Y%m%d_%H%M%S")
        if ts not in ocupados:
            return ts, momento
    raise RuntimeError("No hay timestamp libre para el backup")  # pragma: no cover


def _escribir_json(backup_file: Path, state: dict) -> None:
    with open(backup_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _crear_tar(backup_dir: Path, timestamp: str) -> tuple[Path, dict[str, dict]]:
    """Empaqueta `data/memory` y `data/knowledge` (sin cache ni logs).

    Los directorios ausentes se omiten y quedan registrados en el JSON. Se escribe
    a un temporal y se renombra al final: si algo falla no queda un `.tar.gz` a
    medias que parezca válido.
    """
    destino = backup_dir / f"axioma_backup_{timestamp}.tar.gz"
    temporal = destino.with_name(f".{destino.name}.tmp")
    info: dict[str, dict] = {}
    try:
        with tarfile.open(temporal, "w:gz") as tar:
            for nombre, origen in (("memory", Paths.MEMORY), ("knowledge", Paths.KNOWLEDGE)):
                if not origen.is_dir():
                    info[nombre] = {"incluido": False, "bytes": 0, "motivo": "no existe"}
                    continue
                try:
                    tar.add(str(origen), arcname=nombre, recursive=False)
                except OSError as exc:
                    _log_cli_event("BACKUP", f"No pude agregar {origen}: {exc}", level="WARNING")
                bytes_totales = archivos = 0
                for ruta in origen.rglob("*"):
                    rel = ruta.relative_to(origen).as_posix()
                    try:
                        tar.add(str(ruta), arcname=f"{nombre}/{rel}", recursive=False)
                    except OSError as exc:
                        _log_cli_event("BACKUP", f"Omitido {ruta}: {exc}", level="WARNING")
                        continue
                    if ruta.is_file():
                        try:
                            bytes_totales += ruta.stat().st_size
                        except OSError as exc:
                            _log_cli_event("BACKUP", f"Sin tamaño para {ruta}: {exc}",
                                           level="WARNING")
                        archivos += 1
                info[nombre] = {"incluido": True, "bytes": bytes_totales, "archivos": archivos}
        temporal.replace(destino)
    finally:
        if temporal.exists():
            try:
                temporal.unlink()
            except OSError as exc:
                _log_cli_event("BACKUP", f"No pude borrar el temporal {temporal}: {exc}",
                               level="WARNING")
    return destino, info


def _aplicar_retencion(backup_dir: Path, creado_ts: str) -> list[str]:
    """Deja como máximo `_BACKUP_MAX` backups (json+tar juntos) y devuelve los rotados.

    El recién creado NUNCA se borra: entra fijo en el grupo conservado. Los demás
    se ordenan por timestamp y se conservan los más nuevos hasta completar 3.
    """
    grupos = _agrupar_backups(backup_dir)
    otros = sorted((ts for ts in grupos if ts != creado_ts), reverse=True)
    conservar = {creado_ts, *otros[:_BACKUP_MAX - 1]}
    borrados: list[str] = []
    for ts in otros:
        if ts in conservar:
            continue
        fallo = False
        for ruta in grupos[ts].values():
            try:
                ruta.unlink()
            except OSError as exc:
                _log_cli_event("BACKUP", f"No pude borrar {ruta.name}: {exc}", level="WARNING")
                fallo = True
        if not fallo:
            borrados.append(ts)
    if borrados:
        _log_cli_event("BACKUP", f"Retención: {len(borrados)} backup(s) borrado(s); "
                                 f"quedan {len(conservar)}", extra={"borrados": borrados})
    return borrados


def _pid_activo(pid: int) -> bool:
    """¿El PID existe y está vivo? (psutil si está; si no, POSIX `os.kill(pid, 0)`)."""
    if pid <= 0:
        return False
    try:
        import psutil
    except ImportError:
        logging.getLogger(__name__).debug("[backup] psutil no disponible; uso os.kill")
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            logging.getLogger(__name__).debug(
                "[backup] sin permiso para sondear el PID %s; lo asumo vivo", pid)
            return True
        except OSError as exc:
            logging.getLogger(__name__).debug(f"[backup] os.kill({pid}): {exc}")
            return False
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception as exc:
        logging.getLogger(__name__).debug(f"[backup] psutil no pudo ver el PID {pid}: {exc}")
        return False


def _lock_activo(data_dir: Path) -> str | None:
    """Busca un lock/PID de AXIOMA en `data/` (el proyecto no usaba uno; es un extra)."""
    for nombre in _BACKUP_LOCK:
        ruta = data_dir / nombre
        try:
            if not ruta.is_file():
                continue
            texto = ruta.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError as exc:
            _log_cli_event("BACKUP", f"Lock ilegible {ruta}: {exc}", level="WARNING")
            return f"{nombre} (ilegible)"
        if not texto:
            return f"{nombre} (sin PID)"
        try:
            pid = int(texto.split()[0])
        except (ValueError, IndexError) as exc:
            logging.getLogger(__name__).debug(f"[backup] PID inválido en {nombre}: {exc}")
            return f"{nombre} (contenido inesperado)"
        if _pid_activo(pid):
            return f"{nombre} (PID {pid})"
    return None


def _procesos_axioma() -> list[str]:
    """Procesos que delatan la app corriendo (psutil; sin psutil no hay chequeo).

    Criterio: se excluyen este proceso y sus padres; se busca por nombre de
    ejecutable empaquetado (`axioma`, `axioma-web`) o por argumentos que sólo usa
    la app web (`src.interfaces.web`, `uvicorn ... axioma`, `main.py --web`).
    No alcanza con que la ruta contenga "axioma": los procesos del checkout
    (node/esbuild) viven ahí y darían un falso positivo.
    """
    try:
        import psutil
    except ImportError:
        logging.getLogger(__name__).debug("[backup] psutil no disponible: no detecto procesos")
        return []
    actual = psutil.Process()
    propios = {actual.pid}
    try:
        propios.update(p.pid for p in actual.parents())
    except Exception as exc:
        logging.getLogger(__name__).debug(f"[backup] sin árbol de padres: {exc}")
    senales = ("src.interfaces.web", "src/interfaces/web", "axioma-web", "axioma_web")
    nombres = {"axioma", "axioma-web", "axioma_web", "axioma.exe"}
    hallados: list[str] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid in propios:
                continue
            nombre = (proc.info.get("name") or "").lower()
            if nombre in nombres:
                hallados.append(f"PID {proc.pid} ({nombre})")
                continue
            args = [str(a) for a in (proc.info.get("cmdline") or [])]
            if any(s in arg for arg in args for s in senales):
                hallados.append(f"PID {proc.pid} ({' '.join(args[:3])})")
            elif any(a.endswith("main.py") for a in args) and "--web" in args:
                hallados.append(f"PID {proc.pid} (main.py --web)")
        except psutil.Error as exc:
            logging.getLogger(__name__).debug(f"[backup] proceso {proc.pid} inaccesible: {exc}")
            continue
    return hallados


def _avisar_si_axioma_corre(console: CLIConsole) -> None:
    """Avisa (no aborta) si AXIOMA parece estar corriendo: el tar puede ser inconsistente."""
    motivos: list[str] = []
    lock = _lock_activo(Paths.DATA)
    if lock:
        motivos.append(f"lock/PID en data/: {lock}")
    motivos.extend(_procesos_axioma())
    if not motivos:
        return
    _log_cli_event("BACKUP", "AXIOMA parece estar corriendo: " + "; ".join(motivos),
                   level="WARNING")
    console.print_warning(
        "AXIOMA parece estar corriendo (" + "; ".join(motivos) + "). "
        "LanceDB (data/memory) puede estar en uso: el .tar.gz puede quedar inconsistente. "
        "Para un backup consistente, detené AXIOMA y volvé a ejecutar --backup --full."
    )


def crear_backup_json(full: bool = False) -> Path:
    """Crea el backup y devuelve la ruta del JSON — **implementación ÚNICA**.

    Sin `full` guarda sólo el estado estructural, igual que siempre. Con `full`
    agrega al lado `axioma_backup_<ts>.tar.gz` con `Paths.MEMORY` y
    `Paths.KNOWLEDGE` (cache y logs quedan afuera: son regenerables) y registra
    en el JSON qué se incluyó y cuánto pesa cada cosa.

    Después de crear, rota los backups viejos dejando como máximo `_BACKUP_MAX`.
    """
    backup_dir = _directorio_backups()
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp, momento = _timestamp_unico(backup_dir)
    backup_file = backup_dir / f"axioma_backup_{timestamp}.json"
    state = {
        "version": "1.0.0",
        "timestamp": timestamp,
        "backup_created_at": momento.isoformat(),
        "paths": {
            "root": str(Paths.ROOT),
            "data": str(Paths.DATA),
            "logs": str(Paths.LOGS),
            "memory": str(getattr(Paths, "MEMORY", Paths.DATA / "memory")),
        },
        "settings": {
            "default_model": settings.llm_default_model,
            "code_model": settings.llm_code_model,
            "ollama_timeout": getattr(settings, "ollama_timeout_standard", 45),
            "max_retries": settings.max_retries,
        },
        "note": "Backup estructural — datos de memoria no incluidos",
    }
    _escribir_json(backup_file, state)
    if full:
        state["note"] = ("Backup completo — incluye data/memory y data/knowledge "
                         "(sin cache ni logs)")
        state["full"] = True
        state["paths"]["knowledge"] = str(getattr(Paths, "KNOWLEDGE", Paths.DATA / "knowledge"))
        tar_file, archivos = _crear_tar(backup_dir, timestamp)
        state["backup_tar"] = str(tar_file)
        state["archivos"] = archivos
        _escribir_json(backup_file, state)
    _aplicar_retencion(backup_dir, timestamp)
    return backup_file


def cmd_backup(console: CLIConsole, full: bool = False) -> int:
    """Crea el backup (JSON; con `full`, también el tar de memoria y conocimiento).

    Con `full` avisa —sin abortar— si AXIOMA parece estar corriendo: LanceDB son
    archivos abiertos y comprimirlos en caliente puede dar un backup inconsistente.
    """
    start = time.time()
    _log_cli_event("BACKUP", f"Backup process started (full={full})")
    console.print_panel("BACKUP", "Iniciando respaldo completo..." if full else "Iniciando respaldo...",
                        style="cyan")
    try:
        backup_dir = _directorio_backups()
        antes = set(_agrupar_backups(backup_dir))
        if full:
            _avisar_si_axioma_corre(console)
        backup_file = crear_backup_json(full=full)
        file_size = backup_file.stat().st_size
        duration = time.time() - start
        _log_cli_event("BACKUP", f"Backup completed in {duration:.4f}s",
                       extra={"duration": round(duration, 4), "full": full,
                              "file_path": str(backup_file), "file_size": file_size})
        console.print_success(f"Guardado: {backup_file}")
        console.print_info(f"Tamaño: {file_size} bytes")
        if full:
            tar_file = backup_file.with_suffix(".tar.gz")
            if tar_file.exists():
                console.print_success(f"Datos comprimidos: {tar_file}")
                console.print_info(f"Tamaño del tar: {tar_file.stat().st_size} bytes")
        borrados = antes - set(_agrupar_backups(backup_dir))
        console.print_info(f"Retención: {len(borrados)} backup(s) borrado(s); "
                           f"se conservan como máximo {_BACKUP_MAX}.")
        return 0
    except Exception as e:
        _log_error(e, "CLI.cmd_backup", extra={"duration": time.time() - start})
        _log_cli_event("BACKUP", f"Backup failed: {type(e).__name__}: {str(e)}", level="ERROR")
        console.print_error(f"{type(e).__name__}: {e}")
        return 3
