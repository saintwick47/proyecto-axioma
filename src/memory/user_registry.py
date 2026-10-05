#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — UserRegistry: registro persistente de usuarios (UI de selección)
# Ruta: src/memory/user_registry.py
# Fecha: 2026-08-31
#
# Prerequisito de la UI de AXIOMA (y del plan_rafael): la sesión elige qué
# usuario la usa. El usuario del equipo suele ser admin; los nuevos son normales
# serán "user" (normales, sin privilegios).
#
# Persistencia: data/users.json (JSON simple, versionable, sin DB extra).
# De momento la UI solo habilita el campo "nombre de usuario"; email, lugar y
# país quedan deshabilitados y se guardan vacíos (listos para activarse).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

USERS_FILE_NAME = "users.json"
_lock = threading.Lock()


def _users_path() -> Path:
    try:
        from config.paths import Paths
        return Paths.DATA / USERS_FILE_NAME
    except Exception as e:
        log_degraded(logger, e, "src/memory/user_registry.py:_users_path")
        return Path("data") / USERS_FILE_NAME


def _default_users() -> List[Dict[str, Any]]:
    """Usuarios iniciales: NINGUNO si todavía no hay identidad configurada.

    ✅ `ISSUE-151` (2026-10-05, pedido del usuario): antes se "sembraba" el usuario del autor
    (el usuario del autor, como admin), así que quien instalaba AXIOMA quedaba con ESA identidad sin haber creado
    ninguna. Ahora, con la identidad vacía (instalación nueva) la lista arranca **vacía** y AXIOMA pide
    crear un usuario propio antes de usarse. Si el equipo ya tiene identidad en `.env`, se respeta.
    """
    try:
        from config.settings import settings
        uid = getattr(settings, "axioma_user_id", "") or ""
        role = getattr(settings, "axioma_user_role", "user") or "user"
    except Exception as e:
        log_degraded(logger, e, "src/memory/user_registry.py:_default_users")
        return []
    if not uid.strip():
        return []          # sin identidad: la lista vacía hace que la interfaz pida crear un usuario
    return [{
        "username": uid,
        "email": "",
        "place": "",
        "country": "",
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }]


def _load() -> List[Dict[str, Any]]:
    path = _users_path()
    if not path.exists():
        users = _default_users()
        _save(users)
        return users
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data
        return _default_users()
    except Exception as e:
        logger.warning(f"UserRegistry: no se pudo leer {path}: {e}")
        return _default_users()


def _save(users: List[Dict[str, Any]]) -> None:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    tmp.replace(path)


def list_users() -> List[Dict[str, Any]]:
    """Lista todos los usuarios (copias; el orden de creación se conserva)."""
    with _lock:
        return list(_load())


def get_user(username: str) -> Optional[Dict[str, Any]]:
    """Devuelve un usuario por nombre o None."""
    if not username:
        return None
    with _lock:
        for u in _load():
            if u.get("username") == username:
                return dict(u)
    return None


def create_user(
    username: str,
    email: str = "",
    place: str = "",
    country: str = "",
    role: str = "user",
) -> Dict[str, Any]:
    """
    Crea un usuario nuevo.

    Hoy la UI solo habilita "nombre de usuario" (email/lugar/país
    deshabilitados → vacíos). Valida: nombre no vacío y único.
    Los usuarios creados desde la interfaz son role="user" (el del equipo puede ser admin).
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("El nombre de usuario no puede estar vacío")
    with _lock:
        users = _load()
        if any(u.get("username") == username for u in users):
            raise ValueError(f"El usuario '{username}' ya existe")
        user = {
            "username": username,
            "email": (email or "").strip(),
            "place": (place or "").strip(),
            "country": (country or "").strip(),
            "role": role,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        users.append(user)
        _save(users)
        logger.info(f"UserRegistry: usuario creado: {username!r} (role={role})")
        return dict(user)
