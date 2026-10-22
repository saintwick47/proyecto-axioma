#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_paths.py  (REFACTORIZACION POR PARTES)
# Ruta: config/settings_paths.py
#
# 2026-10-13: dominio **RUTAS (absolutas, resueltas) ... MEMORIA VECTORIAL** extraido de `config/settings.py`, sin cambios de
# comportamiento. Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase
# comun no recolecta anotaciones como campos; el mixin de METODOS que ya existia,
# `settings_computations.py`, es una clase comun a proposito).
#
# La indentacion del bloque NO se toca: va al cuerpo de OTRA clase.
# ===============================================================
import logging
from pathlib import Path
from typing import Union

from pydantic import BaseModel, Field, field_validator, model_validator

_REPO_ROOT = Path(__file__).resolve().parents[1]

class SettingsPaths(BaseModel):
    """Dominio RUTAS (absolutas, resueltas) ... MEMORIA VECTORIAL extraido de `Settings`."""

    # ═══════════════════════════════════════════════════════════
    # === RUTAS (absolutas, resueltas) ===
    # ═══════════════════════════════════════════════════════════
    base_dir: Path = Field(default_factory=lambda: _REPO_ROOT)
    data_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "data")
    logs_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "logs")
    config_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "config")
    cache_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "cache")

    @field_validator("base_dir", "data_dir", "logs_dir", "config_dir", "cache_dir", mode="before")
    @classmethod
    def resolve_absolute_path(cls, v: Union[str, Path, None]) -> Path:
        """Convierte rutas relativas a absolutas."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return _REPO_ROOT
        path = Path(v)
        if not path.is_absolute():
            path = _REPO_ROOT / path
        return path.resolve() if path.exists() else path.absolute()

    @model_validator(mode="after")
    def ensure_directories(self) -> "Settings":
        """Crea directorios críticos si no existen."""
        for dir_path in [self.data_dir, self.logs_dir, self.cache_dir]:
            if not dir_path.exists():
                try:
                    dir_path.mkdir(parents=True, exist_ok=True)
                    logging.info(f"Directorio creado: {dir_path}")
                except PermissionError:
                    logging.warning(f"Sin permisos para crear: {dir_path}")
        return self

    # ═══════════════════════════════════════════════════════════
    # === MEMORIA VECTORIAL ===
    # ═══════════════════════════════════════════════════════════
    vector_db_path: Path = Field(default_factory=lambda: _REPO_ROOT / "data" / "memory" / "lancedb")
    db_path: Path = Field(default_factory=lambda: _REPO_ROOT / "data" / "memory" / "axioma.db")

    @field_validator("vector_db_path", "db_path", mode="before")
    @classmethod
    def resolve_db_paths(cls, v: Union[str, Path, None]) -> Path:
        """Resuelve y crea directorio para DBs."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return _REPO_ROOT / "data" / "memory"
        path = Path(v)
        if path.suffix == '.db':
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
        return path.resolve() if path.exists() else path.absolute()
