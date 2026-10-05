#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/utils/provider_circuit.py
# Autor: SaintWick
# AXIOMA — src/utils/provider_circuit.py
# Circuit breaker por proveedor externo (consolidado).
# ✅ 2026-08-25: Unificadas 4 copias idénticas de `_ProviderCircuit`
#   que vivían en researcher_sources.py, searcher.py, external_apis.py
#   y classifier_api.py (detectadas por axioma_inspector como duplicado real).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import time
from typing import Optional


class ProviderCircuit:
    """
    Circuit breaker por proveedor de red/API.

    Sin esto, un proveedor CONFIGURADO (con API key) pero caído a nivel red
    hacía que cada búsqueda pagara su timeout completo antes de caer al
    siguiente — sin memoria de fallos recientes.

    Tras `fail_threshold` fallos consecutivos, el proveedor queda "abierto"
    (se saltea sin red) por `cooldown_seconds`. Pasado ese tiempo se deja
    pasar un intento de prueba; si funciona, se cierra el circuito.
    """

    __slots__ = ["fail_count", "opened_at"]

    def __init__(self) -> None:
        self.fail_count = 0
        self.opened_at: Optional[float] = None

    def is_open(self, cooldown_seconds: float) -> bool:
        if self.opened_at is None:
            return False
        if (time.time() - self.opened_at) >= cooldown_seconds:
            return False  # cooldown venció — dejar pasar un intento de prueba
        return True

    def record_success(self) -> None:
        self.fail_count = 0
        self.opened_at = None

    def record_failure(self, fail_threshold: int) -> None:
        self.fail_count += 1
        if self.fail_count >= fail_threshold and self.opened_at is None:
            self.opened_at = time.time()
