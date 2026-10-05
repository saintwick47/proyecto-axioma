#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# workspace_manager.py - Gestor de Workspaces Persistentes
# Ruta: /ruta/a/axioma/src/core/workspace_manager.py
# Autor: SaintWick
# Versión: 1.7.0
#
# Gestiona directorios de trabajo persistentes con entornos virtuales
# aislados por sesión. Permite instalación de paquetes, historial de
# intentos, escaneo de archivos generados y limpieza de workspaces
# obsoletos. Incluye defensas contra shell injection y auditoría de
# instalaciones.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ✅ FIX AUDITORÍA (S3): whitelist de formato — nombre de paquete PyPI válido,
# con extras opcionales ([extra1,extra2]) y un especificador de versión opcional
# (==, >=, <=, ~=, !=). Rechaza cualquier cosa con espacios, $, `, ;, |, &, etc.
# ✅ FIX: el primer carácter ahora DEBE ser alfanumérico — la versión anterior
# permitía que "-" o "." abrieran el string, así que valores como "-e",
# "--upgrade", "--user", "--pre" pasaban la validación como "nombre de paquete
# válido" y llegaban tal cual a `pip install <esto> --quiet` (subprocess sin
# shell, así que no es inyección de shell, pero sí inyección de flags de pip —
# ningún nombre real de paquete PyPI empieza con "-" o ".").
_PACKAGE_NAME_RE = re.compile(
    r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*(\[[a-zA-Z0-9_,-]+\])?(==|>=|<=|~=|!=)?[a-zA-Z0-9._-]*$'
)


class WorkspaceManager:
    """
    Gestiona directorios de trabajo persistentes con venv aislado por sesión.

    Estructura:
        ~/.axioma/workspaces/{session_id}/
            script.py          ← último código ejecutado
            attempt_1.py       ← historial de intentos (v1.4.0)
            attempt_2.py       ← historial de intentos (v1.4.0)
            output.csv         ← archivos generados por el código
            install_log.jsonl  ← auditoría de instalaciones (v1.6.0)
            venv/              ← entorno virtual aislado
                bin/python     ← ejecutable del venv
                lib/           ← paquetes instalados
    """

    def __init__(self, base_path: Optional[str] = None) -> None:
        self._base = Path(base_path) if base_path else Path.home() / ".axioma" / "workspaces"
        self._base.mkdir(parents=True, exist_ok=True)
        logger.info(f"[WorkspaceManager] Base: {self._base}")

    def get_workspace(self, session_id: str) -> str:
        """Retorna path al workspace de la sesión. Lo crea si no existe."""
        sid = session_id or "default"
        ws = self._base / sid
        ws.mkdir(parents=True, exist_ok=True)
        return str(ws)

    def get_venv_python(self, session_id: str) -> str:
        """
        Retorna path absoluto al binario python del venv de la sesión.
        Crea el venv si no existe usando sys.executable del host.
        """
        sid = session_id or "default"
        ws = self.get_workspace(sid)
        venv_dir = os.path.join(ws, "venv")

        if not os.path.exists(venv_dir):
            logger.info(f"[WorkspaceManager] Creando venv para sesión '{sid}' en {venv_dir}")
            result = subprocess.run(
                [sys.executable, "-m", "venv", venv_dir],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"No se pudo crear venv en {venv_dir}: {result.stderr}"
                )

        # Binario correcto según OS
        if os.name == "nt":
            python_bin = os.path.join(venv_dir, "Scripts", "python.exe")
        else:
            python_bin = os.path.join(venv_dir, "bin", "python")

        if not os.path.exists(python_bin):
            raise RuntimeError(
                f"Venv creado pero python no encontrado en {python_bin}"
            )

        return python_bin

    def install_package(self, session_id: str, package: str, timeout: float = 120.0) -> dict:
        """
        Instala un paquete en el venv de la sesión usando pip.
        Retorna dict con success, stdout, stderr.
        """
        sid = session_id or "default"
        pkg = package.strip()
        if not _PACKAGE_NAME_RE.match(pkg):
            return {
                "success": False, "package": package, "stdout": "",
                "stderr": f"Nombre de paquete inválido o no permitido: {package!r}",
            }
        python_bin = self.get_venv_python(sid)
        pip_bin = os.path.join(os.path.dirname(python_bin), "pip")
        try:
            result = subprocess.run(
                [pip_bin, "install", pkg, "--quiet"],
                capture_output=True, text=True, timeout=timeout,
            )
            # Auditoría: registrar instalación exitosa
            if result.returncode == 0:
                self._log_install(sid, pkg)
            return {
                "success": result.returncode == 0,
                "package": package,
                "stdout": result.stdout[-500:] if result.stdout else "",
                "stderr": result.stderr[-500:] if result.stderr else "",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "package": package, "stdout": "", "stderr": "Timeout instalando"}
        except Exception as e:
            return {"success": False, "package": package, "stdout": "", "stderr": str(e)}

    def install_packages(self, session_id: str, packages: list, timeout: float = 180.0) -> dict:
        """
        Instala múltiples paquetes en un solo comando pip.
        Más eficiente que instalar uno por uno — una sola resolución de dependencias.
        """
        if not packages:
            return {"success": True, "packages": [], "stdout": "", "stderr": ""}

        pkgs = [p.strip() for p in packages]
        invalid = [p for p in pkgs if not _PACKAGE_NAME_RE.match(p)]
        if invalid:
            return {
                "success": False, "packages": packages, "stdout": "",
                "stderr": f"Paquetes inválidos o no permitidos: {invalid}",
            }

        sid = session_id or "default"
        python_bin = self.get_venv_python(sid)
        pip_bin = os.path.join(os.path.dirname(python_bin), "pip")
        try:
            result = subprocess.run(
                [pip_bin, "install", *pkgs, "--quiet"],
                capture_output=True, text=True, timeout=timeout,
            )
            success = result.returncode == 0
            # Auditoría: registrar cada paquete exitoso
            if success:
                for pkg in pkgs:
                    self._log_install(sid, pkg)
            return {
                "success": success,
                "packages": packages,
                "stdout": result.stdout[-500:] if result.stdout else "",
                "stderr": result.stderr[-500:] if result.stderr else "",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "packages": packages, "stdout": "", "stderr": "Timeout instalando batch"}
        except Exception as e:
            return {"success": False, "packages": packages, "stdout": "", "stderr": str(e)}

    def _log_install(self, session_id: str, package: str) -> None:
        """
        Registra instalación exitosa en install_log.jsonl dentro del workspace.
        Cada línea es un JSON con paquete y timestamp.
        """
        import json
        from datetime import datetime
        sid = session_id or "default"
        log_file = os.path.join(self.get_workspace(sid), "install_log.jsonl")
        try:
            entry = {"package": package, "timestamp": datetime.now().isoformat()}
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.debug(f"[WorkspaceManager] No se pudo escribir install_log: {e}")

    def scan_workspace(self, session_id: str) -> List[dict]:
        """
        Lista archivos en el workspace (excluye venv/ y script.py).
        Retorna lista de dicts con name, size_bytes, extension.
        """
        sid = session_id or "default"
        ws = self._base / sid
        if not ws.is_dir():
            return []
        files = []
        for p in ws.iterdir():
            if p.is_file() and p.name != "script.py":
                files.append({
                    "name": p.name,
                    "size_bytes": p.stat().st_size,
                    "extension": p.suffix.lower(),
                })
        return files

    def get_attempt_history(self, session_id: str) -> List[dict]:
        """Lista archivos attempt_N.py con sus tamaños."""
        sid = session_id or "default"
        ws = self._base / sid
        if not ws.is_dir():
            return []
        history = []
        for p in sorted(ws.iterdir()):
            if p.is_file() and p.name.startswith("attempt_") and p.name.endswith(".py"):
                n = int(p.stem.split("_")[1])
                history.append({"attempt": n, "file": str(p), "size_bytes": p.stat().st_size})
        return history

    def cleanup_stale(self, max_age_hours: float = 24.0) -> List[str]:
        """
        Elimina workspaces cuyo archivo script.py tenga más de max_age_hours
        sin modificación. También elimina workspaces huérfanos (sin script.py).
        Retorna lista de session_ids eliminados.
        """
        cutoff = time.time() - (max_age_hours * 3600)
        deleted = []
        for ws_dir in self._base.iterdir():
            if not ws_dir.is_dir():
                continue
            script = ws_dir / "script.py"
            if script.exists():
                mtime = script.stat().st_mtime
                if mtime < cutoff:
                    sid = ws_dir.name
                    self.cleanup(sid)
                    deleted.append(sid)
            else:
                # Sin script.py → workspace huérfano, eliminar
                sid = ws_dir.name
                self.cleanup(sid)
                deleted.append(sid)
        if deleted:
            logger.info(f"[WorkspaceManager] Cleanup: {len(deleted)} workspaces eliminados")
        return deleted

    def cleanup(self, session_id: str) -> bool:
        """Elimina el workspace y su venv de una sesión."""
        sid = session_id or "default"
        ws = self._base / sid
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)
            logger.info(f"[WorkspaceManager] Workspace '{sid}' eliminado")
            return True
        return False

    def list_workspaces(self) -> List[str]:
        """Lista session_ids de workspaces existentes."""
        return [p.name for p in self._base.iterdir() if p.is_dir()]

    def workspace_exists(self, session_id: str) -> bool:
        """True si el workspace de la sesión ya existe."""
        return (self._base / (session_id or "default")).is_dir()

    def venv_exists(self, session_id: str) -> bool:
        """True si el venv de la sesión ya existe."""
        venv_dir = os.path.join(self.get_workspace(session_id), "venv")
        return os.path.isdir(venv_dir)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "base_path": str(self._base),
            "workspaces_count": len(self.list_workspaces()),
            "workspaces": self.list_workspaces(),
        }


# ============================================================================
# SINGLETON
# ============================================================================

_wm: Optional[WorkspaceManager] = None


def get_workspace_manager(base_path: Optional[str] = None) -> WorkspaceManager:
    global _wm
    if _wm is None:
        _wm = WorkspaceManager(base_path=base_path)
    return _wm


def reset_workspace_manager() -> None:
    global _wm
    _wm = None
