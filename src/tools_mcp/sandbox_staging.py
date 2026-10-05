#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# sandbox_staging.py - Sandbox Staging FS (Escritura Segura con Revert)
# Ruta: /ruta/a/axioma/src/tools_mcp/sandbox_staging.py
# Autor: SaintWick
# Versión: 0.5.3
#
# Proporciona staging de archivos para escrituras seguras: stage_write(),
# commit() y revert() con checksum SHA256, backup y restauración < 3s.
# Usa ~/.axioma/{staging,backup}/ por defecto con permisos 0o700.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# STAGING ENTRY — Registro de una operación de staging
# ============================================================================

class StagingEntry:
    """
    Entrada en el registro de staging: path + checksum + backup.

    Attributes:
        path            : Ruta destino final
        checksum        : SHA256 del contenido staged (primeros 16 chars)
        backup_path     : Ruta al backup del archivo original (None si no existía)
        staged_at       : Timestamp de la operación de staging
        committed       : True si ya se hizo commit permanente
        _staged_content : Contenido en memoria para commit
        _staging_path   : Ruta al archivo .staged temporal
        _encoding       : Encoding del contenido
        _errors         : Manejo de errores de encode/decode ('strict'/'surrogateescape'/...)
    """
    # ✅ CORREGIDO v0.5.1: Agregar atributos internos a __slots__
    # ✅ NUEVO (agente filesystem) v1.6.0: _errors — ver stage_write().
    __slots__ = [
        "path", "checksum", "backup_path", "staged_at", "committed",
        "_staged_content", "_staging_path", "_encoding", "_errors",
    ]

    def __init__(
        self,
        path: Path,
        checksum: str,
        backup_path: Optional[Path],
    ) -> None:
        self.path = path
        self.checksum = checksum
        self.backup_path = backup_path
        self.staged_at = time.time()
        self.committed = False
        # ✅ CORREGIDO v0.5.1: Inicializar nuevos slots
        self._staged_content: Optional[str] = None
        self._staging_path: Optional[Path] = None
        self._encoding: str = "utf-8"
        self._errors: str = "strict"


# ============================================================================
# STAGING FS — Sistema de escritura segura con checksum + revert < 3s
# ============================================================================

class StagingFS:
    """
    Sistema de staging para escrituras seguras en filesystem.

    Filosofía: todo write va primero a staging. Si el proceso posterior
    falla, revert() restaura el estado original en < 3s.
    Si todo sale bien, commit() hace el write permanente.

    Pipeline:
        stage_write(path, content)  → copia backup + escribe en staging
        commit(path)                → mueve staging → destino final
        revert(path)                → restaura backup → destino (< 3s)
        revert_all()                → revierte todos los cambios de la sesión

    Thread-safe. Una instancia por sesión de ejecución.

    Uso desde ToolExecutor / agentes:
        staging = StagingFS(session_id="sess_001")
        ok, err = staging.stage_write("src/core/dispatcher.py", new_content)
        if ok:
            # validar contenido...
            staging.commit("src/core/dispatcher.py")
        else:
            staging.revert("src/core/dispatcher.py")
    """

    def __init__(
        self,
        session_id: str = "",
        staging_base: Optional[Path] = None,
        backup_base: Optional[Path] = None,
    ) -> None:
        """
        Inicializa el sistema de staging.

        Args:
            session_id   : Identificador de sesión para aislar operaciones
            staging_base : Directorio base para archivos .staged
            backup_base  : Directorio base para archivos .bak

        ✅ FIX AUDITORÍA (S7): antes la base por defecto era
        /tmp/axioma_staging|backup/{session_id}. /tmp es world-writable en
        la mayoría de sistemas Linux — otro usuario local podría leer
        archivos staged (contenido de código propuesto, potencialmente
        sensible) o plantar un symlink en el path esperado antes de que
        AXIOMA escriba ahí. Ahora la base por defecto es
        ~/.axioma/{staging,backup}/{session_id} con permisos 0o700 (solo
        el dueño) — mismo patrón que WorkspaceManager. Se verifica que el
        directorio no sea un symlink antes de usarlo. Si ~/.axioma no es
        escribible, cae a /tmp con un warning explícito en el log.
        """
        self._session_id = session_id or f"staging_{int(time.time())}"

        default_staging = Path.home() / ".axioma" / "staging" / self._session_id
        default_backup = Path.home() / ".axioma" / "backup" / self._session_id
        fallback_staging = Path(f"/tmp/axioma_staging/{self._session_id}")
        fallback_backup = Path(f"/tmp/axioma_backup/{self._session_id}")

        self._staging_base = self._secure_dir(
            Path(staging_base) if staging_base else default_staging,
            fallback_staging,
        )
        self._backup_base = self._secure_dir(
            Path(backup_base) if backup_base else default_backup,
            fallback_backup,
        )
        self._registry: Dict[str, StagingEntry] = {}
        self._lock = threading.RLock()
        logger.info(
            f"[StagingFS] Inicializado — session={self._session_id} "
            f"staging={self._staging_base}"
        )

    @staticmethod
    def _secure_dir(primary: Path, fallback: Path) -> Path:
        """
        ✅ FIX AUDITORÍA (S7): crea `primary` con permisos 0o700, rechazando
        symlinks. Si falla (no escribible, o es un symlink plantado), cae a
        `fallback` — que se asegura con el mismo criterio, sin más niveles
        de fallback debajo para evitar recursión.
        """
        for candidate, is_fallback in ((primary, False), (fallback, True)):
            try:
                if candidate.exists() and candidate.is_symlink():
                    logger.warning(
                        f"[StagingFS] {candidate} es un symlink — rechazado"
                        + ("" if is_fallback else ", probando fallback")
                    )
                    if is_fallback:
                        raise RuntimeError(f"Fallback también es symlink: {candidate}")
                    continue
                candidate.mkdir(parents=True, exist_ok=True)
                # mkdir(mode=...) queda sujeto al umask del proceso — chmod
                # explícito para garantizar 0o700 exacto sin depender de eso.
                os.chmod(candidate, 0o700)
                return candidate
            except OSError as e:
                if is_fallback:
                    raise RuntimeError(
                        f"No se pudo crear directorio de staging seguro "
                        f"(ni primario ni fallback): {e}"
                    ) from e
                logger.warning(
                    f"[StagingFS] No se pudo usar {candidate} ({e}) — "
                    f"fallback a {fallback}"
                )
        raise RuntimeError("No se pudo inicializar directorio de staging")

    # ── Staging ───────────────────────────────────────────────────────────

    def stage_write(
        self,
        path: Union[str, Path],
        content: str,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> Tuple[bool, Optional[str]]:
        """
        Escribe content en staging (no toca el archivo original todavía).

        1. Si el archivo original existe → hace backup con checksum
        2. Escribe content en directorio de staging
        3. Registra la entrada para commit/revert posterior

        Args:
            path     : Ruta destino final
            content  : Contenido a escribir
            encoding : Encoding del contenido
            errors   : ✅ NUEVO (agente filesystem) v1.6.0 — manejo de
                       encode/decode ('strict' por defecto, sin cambio de
                       comportamiento). PromotionGate._promote() pasa
                       'surrogateescape' para "move" de archivos binarios —
                       decodificados con el mismo modo del lado read(),
                       hace round-trip exacto byte a byte en vez de
                       corromper silenciosamente (como haría 'replace') o
                       crashear (como hacía 'strict' sin este parámetro).

        Returns:
            (True, None) si OK
            (False, "error message") si falló
        """
        path = Path(path)
        path_key = str(path.resolve())

        with self._lock:
            try:
                # 1. Backup del original si existe
                backup_path = None
                original_checksum = ""
                if path.exists():
                    backup_path = (
                        self._backup_base
                        / f"{path.name}_{int(time.time() * 1000)}.bak"
                    )
                    shutil.copy2(str(path), str(backup_path))
                    original_checksum = self._checksum_file(path)
                    logger.debug(
                        f"[StagingFS] Backup: {path.name} "
                        f"→ {backup_path.name}"
                    )

                # 2. Escribir en staging
                staging_path = (
                    self._staging_base
                    / f"{path.name}_{int(time.time() * 1000)}.staged"
                )
                staging_path.write_text(content, encoding=encoding, errors=errors)
                staged_checksum = self._checksum_str(content)

                # 3. Registrar
                entry = StagingEntry(
                    path=path,
                    checksum=staged_checksum,
                    backup_path=backup_path,
                )
                self._registry[path_key] = entry

                logger.debug(
                    f"[StagingFS] Staged: {path} "
                    f"checksum={staged_checksum[:8]}... "
                    f"backup={'sí' if backup_path else 'no'}"
                )
                # Guardar content en staging para commit posterior
                entry._staged_content = content
                entry._staging_path = staging_path
                entry._encoding = encoding
                entry._errors = errors
                return True, None

            except Exception as e:
                logger.error(
                    f"[StagingFS] Error en stage_write({path}): {e}"
                )
                return False, str(e)

    # ── Commit ────────────────────────────────────────────────────────────

    def commit(
        self, path: Union[str, Path]
    ) -> Tuple[bool, Optional[str]]:
        """
        Mueve el contenido staged al destino final.
        Solo llegar aquí si la validación posterior fue exitosa.

        Args:
            path : Ruta destino a commitear

        Returns:
            (True, None) si OK
            (False, "error") si falló (auto-revert si falla el commit)
        """
        path = Path(path)
        path_key = str(path.resolve())

        with self._lock:
            entry = self._registry.get(path_key)
            if entry is None:
                return False, f"No hay staging para: {path}"
            if entry.committed:
                return True, None  # Ya fue committed

            try:
                # Verificar integridad del staged content
                staged_checksum = self._checksum_str(entry._staged_content)
                if staged_checksum != entry.checksum:
                    logger.error(
                        f"[StagingFS] Checksum mismatch en commit de {path}"
                    )
                    self._do_revert(entry)
                    return False, "Checksum mismatch — revert aplicado"

                # Crear directorios si no existen
                path.parent.mkdir(parents=True, exist_ok=True)

                # Escribir al destino final
                # ✅ NUEVO (agente filesystem) v1.6.0: errors=entry._errors
                # — mismo modo con el que se decodificó en stage_write(),
                # necesario para que 'surrogateescape' haga round-trip
                # exacto (ver docstring de stage_write()).
                path.write_text(
                    entry._staged_content, encoding=entry._encoding, errors=entry._errors
                )
                entry.committed = True

                logger.info(f"[StagingFS] Committed: {path}")
                return True, None

            except Exception as e:
                logger.error(
                    f"[StagingFS] Error en commit({path}): {e} "
                    f"— intentando revert"
                )
                self._do_revert(entry)
                return False, f"Commit fallido, revert aplicado: {e}"

    # ── Revert ────────────────────────────────────────────────────────────

    def revert(
        self, path: Union[str, Path]
    ) -> Tuple[bool, Optional[str]]:
        """
        Restaura el archivo original desde backup.
        Objetivo: completar en < 3s.

        Args:
            path : Ruta a restaurar

        Returns:
            (True, None) si OK o si no había backup (archivo nuevo)
            (False, "error") si el revert falló
        """
        path = Path(path)
        path_key = str(path.resolve())

        with self._lock:
            entry = self._registry.get(path_key)
            if entry is None:
                return True, None  # Nada que revertir
            return self._do_revert(entry)

    def revert_all(self) -> Dict[str, Any]:
        """
        Revierte todos los cambios de la sesión.

        Returns:
            Reporte: {path: "ok"|"error", ...} con métricas
        """
        t0 = time.perf_counter()
        report: Dict[str, Any] = {}

        with self._lock:
            for path_key, entry in self._registry.items():
                if entry.committed:
                    report[path_key] = "already_committed"
                    continue
                ok, err = self._do_revert(entry)
                report[path_key] = "ok" if ok else f"error: {err}"

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            f"[StagingFS] revert_all — {len(report)} paths en "
            f"{elapsed:.0f}ms"
        )
        return {
            "paths": report,
            "duration_ms": round(elapsed, 2),
            "session": self._session_id,
        }

    def _do_revert(
        self, entry: StagingEntry
    ) -> Tuple[bool, Optional[str]]:
        """
        Lógica interna de revert para una entrada.

        Restaura desde backup si existe, o elimina si fue creado nuevo.
        Verifica checksum post-revert para garantizar integridad.
        """
        try:
            t0 = time.perf_counter()

            if entry.backup_path and entry.backup_path.exists():
                # Restaurar desde backup
                shutil.copy2(str(entry.backup_path), str(entry.path))
                # Verificar checksum post-revert
                reverted_checksum = self._checksum_file(entry.path)
                backup_checksum = self._checksum_file(entry.backup_path)
                if reverted_checksum != backup_checksum:
                    logger.error(
                        f"[StagingFS] Checksum mismatch post-revert: "
                        f"{entry.path}"
                    )
                    return False, "Checksum mismatch post-revert"
            elif entry.path.exists() and not entry.backup_path:
                # El archivo fue creado por staging — eliminarlo
                entry.path.unlink()

            elapsed = (time.perf_counter() - t0) * 1000
            if elapsed > 3000:
                logger.warning(
                    f"[StagingFS] Revert tardó {elapsed:.0f}ms > 3s "
                    f"objetivo: {entry.path}"
                )
            else:
                logger.info(
                    f"[StagingFS] Revert OK: {entry.path} "
                    f"({elapsed:.0f}ms)"
                )

            return True, None

        except Exception as e:
            logger.error(
                f"[StagingFS] Revert fallido: {entry.path} — {e}"
            )
            return False, str(e)

    # ── Utilidades ────────────────────────────────────────────────────────

    @staticmethod
    def _checksum_file(path: Path) -> str:
        """SHA256 del contenido de un archivo (primeros 16 chars)."""
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except Exception:
            return ""

    @staticmethod
    def _checksum_str(content: str) -> str:
        """SHA256 de un string (primeros 16 chars)."""
        return (
            hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        )

    def get_status(self) -> Dict[str, Any]:
        """Estado actual del staging para /sandbox en CLI."""
        with self._lock:
            return {
                "session_id": self._session_id,
                "staged_count": len(self._registry),
                "committed": sum(
                    1 for e in self._registry.values() if e.committed
                ),
                "pending": sum(
                    1 for e in self._registry.values() if not e.committed
                ),
                "staging_dir": str(self._staging_base),
                "backup_dir": str(self._backup_base),
                "paths": {
                    k: {
                        "committed": e.committed,
                        "has_backup": e.backup_path is not None,
                        "checksum": e.checksum[:8] + "...",
                    }
                    for k, e in self._registry.items()
                },
            }

    def cleanup(self) -> None:
        """Limpia directorios de staging y backup. Llamar en shutdown."""
        try:
            if self._staging_base.exists():
                shutil.rmtree(str(self._staging_base))
            if self._backup_base.exists():
                shutil.rmtree(str(self._backup_base))
            logger.debug(
                f"[StagingFS] Limpieza completada: {self._session_id}"
            )
        except Exception as e:
            logger.warning(f"[StagingFS] Error en cleanup: {e}")


# ============================================================================
# SINGLETON DE STAGING POR SESIÓN
# ============================================================================

_staging_instances: Dict[str, StagingFS] = {}
_staging_lock = threading.Lock()


def get_staging(session_id: str = "default") -> StagingFS:
    """
    Retorna instancia de StagingFS para la sesión dada.

    Thread-safe. Crea la instancia en el primer llamado.

    Args:
        session_id : Identificador de sesión

    Returns:
        StagingFS para la sesión especificada
    """
    with _staging_lock:
        if session_id not in _staging_instances:
            _staging_instances[session_id] = StagingFS(
                session_id=session_id
            )
        return _staging_instances[session_id]


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = ["StagingEntry", "StagingFS", "get_staging"]
