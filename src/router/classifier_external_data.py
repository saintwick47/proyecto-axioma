#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — classifier_external_data.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/router/classifier_external_data.py
#
# ✅ v0.6.9v (REFACTOR): acceso a datos externos (weather/news/finance/time)
#   extraído de src/router/classifier.py.
#
#   Qué se movió desde classifier.py (sin cambios de comportamiento):
#     - `get_weather_data` / `get_news_data` / `get_finance_data` / `get_time_info`
#       (4 delegadores de 1 línea a `self._api`, el APIDetector).
#
#   Nota de hallazgo: estes 4 métodos eran DELEGADORES (no duplicaban lógica —
#   la lógica real vive encapsulada en APIDetector, composición correcta). Se
#   movieron a este mixin porque eran wrappers sin consumidores (código muerto
#   dentro de IntentClassifier) y para dejar el original más cohesivo.
#
#   `IntentClassifier` hereda ahora `ClassifierExternalDataMixin`, así la API
#   pública (los 4 métodos siguen disponibles en el classifier) no cambia.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

from typing import Any, Dict, Optional


class ClassifierExternalDataMixin:
    """Acceso a datos externos vía APIDetector (`self._api`).

    Extraído de IntentClassifier (50/50): estos 4 delegadores exponen la
    integración de APIs externas (weather/news/finance/time) para testing y
    uso por el consumidor. La lógica real está en APIDetector; acá solo se
    delega a `self._api` (que el Dispatcher/classifier construye).
    """

    def get_weather_data(self, location: str) -> Optional[Dict[str, Any]]:
        """
        Obtiene datos climáticos vía API (expuesto para testing).

        Args:
            location: Nombre de la ubicación

        Returns:
            Dict con datos climáticos o None si falla
        """
        return self._api._get_weather_from_api(location)

    def get_news_data(self, query: str, location: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Obtiene noticias vía API (expuesto para testing).

        Args:
            query: Término de búsqueda
            location: País/región para filtrar

        Returns:
            Dict con noticias o None si falla
        """
        return self._api._get_news_from_api(query, location)

    def get_finance_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Obtiene datos financieros vía API (expuesto para testing).

        Args:
            symbol: Símbolo financiero (ej: USD, BTC, AAPL)

        Returns:
            Dict con datos financieros o None si falla
        """
        return self._api._get_finance_from_api(symbol)

    def get_time_info(self, location: Optional[str] = None) -> Dict[str, Any]:
        """
        Obtiene información de hora/zona horaria.

        Args:
            location: Ubicación opcional para zona horaria

        Returns:
            Dict con información de hora
        """
        return self._api._get_time_info(location)
