#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_web.py  (REFACTORIZACION POR PARTES)
# Ruta: config/settings_web.py
#
# 2026-10-13: dominio **SERVIDOR WEB ... APIs EXTERNAS** extraido de `config/settings.py`, sin cambios de
# comportamiento. Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase
# comun no recolecta anotaciones como campos; el mixin de METODOS que ya existia,
# `settings_computations.py`, es una clase comun a proposito).
#
# La indentacion del bloque NO se toca: va al cuerpo de OTRA clase.
# ===============================================================
from typing import Optional

from pydantic import BaseModel, Field


class SettingsWeb(BaseModel):
    """Dominio SERVIDOR WEB ... APIs EXTERNAS extraido de `Settings`."""

    # ═══════════════════════════════════════════════════════════
    # === SERVIDOR WEB ===
    # ═══════════════════════════════════════════════════════════
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1024, le=65535)
    debug: bool = False
    reload: bool = False

    # 🔐 Token de las rutas que ESCRIBEN configuración por la API
    # (`/api/v1/preflight/claves` y `/modelos`). **No hace falta generarlo a mano**:
    # si está vacío, AXIOMA usa (o genera) `data/.web_token` mediante
    # `config.acceso_web.token_de_web()`, con permisos 0600. Este campo queda como
    # opción AVANZADA para fijarlo desde `AXIOMA_WEB_TOKEN` (entorno o `.env`);
    # si tiene valor, manda sobre el archivo. Se manda en el header
    # `X-AXIOMA-Token` (ver `routes_preflight.exigir_token_de_escritura`).
    axioma_web_token: str = Field(
        default="",
        description="Override AVANZADO del token de escritura de la API "
                    "(header X-AXIOMA-Token). Vacío = se usa/genera data/.web_token",
    )

    # ═══════════════════════════════════════════════════════════
    # === APIs EXTERNAS ===
    # ═══════════════════════════════════════════════════════════
    tavily_api_key: Optional[str] = None
    serper_api_key: Optional[str] = None
    serpapi_api_key: Optional[str] = None  # ✅ v0.6.9q: SerpAPI (serpapi.com)
    news_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    search_engine_id: Optional[str] = None
