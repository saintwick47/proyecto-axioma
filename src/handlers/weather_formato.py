"""Formato del clima para el chat (2026-11-08).

Pedido del usuario: la respuesta del clima no decía **de dónde** venía el viento y quería algo
más lindo, estilo Google. Open-Meteo ya devuelve `wind_direction_10m` (grados), así que acá se
traduce a punto cardinal (16 rumbos, en español) y se arma un bloque de texto con sensación
térmica, ráfagas, lluvia y pronóstico de 3 días.

Es **puro** (sin red, sin UI): por eso se puede probar con datos fijos.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # sólo para las anotaciones (con `from __future__ import annotations`)
    from collections.abc import Callable

# 16 rumbos en sentido horario desde el norte. En español: O = oeste, NO = noroeste.
PUNTOS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
          "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO")


def direccion_viento(grados: Any) -> str:
    """`315` → `"NO (315°)"`. Cadena vacía si no es un número válido."""
    try:
        valor = float(grados) % 360
    except (TypeError, ValueError):
        return ""
    punto = PUNTOS[int((valor + 11.25) // 22.5) % 16]
    return f"{punto} ({valor:.0f}°)"


def _num(valor: Any, formato: str = ".1f", unidad: str = "") -> str:
    """Número formateado; `—` si falta (la respuesta nunca se rompe por un campo ausente)."""
    try:
        return f"{float(valor):{formato}}{unidad}"
    except (TypeError, ValueError):
        return "—"


def formatear_clima(
    ciudad: str,
    actual: dict[str, Any],
    diario: dict[str, Any] | None = None,
    emoji: str = "🌡️",
    emoji_de_codigo: Callable[[int], str] | None = None,
) -> str:
    """Arma el bloque de clima que se muestra en el chat.

    Args:
        ciudad: nombre para mostrar.
        actual: bloque `current` de Open-Meteo.
        diario: bloque `daily` (opcional); si falta, no se muestra el pronóstico.
        emoji: emoji de la condición actual.
        emoji_de_codigo: función que traduce un `weather_code` a emoji (para el pronóstico).
    """
    temp = _num(actual.get("temperature_2m"))
    sensacion = _num(actual.get("apparent_temperature"))
    humedad = _num(actual.get("relative_humidity_2m"), ".0f")
    viento = _num(actual.get("wind_speed_10m"))
    rafagas = _num(actual.get("wind_gusts_10m"))
    lluvia = _num(actual.get("precipitation"))
    direccion = direccion_viento(actual.get("wind_direction_10m"))

    lineas = [f"{emoji} Clima en {ciudad}"]
    if sensacion != "—" and sensacion != temp:
        lineas.append(f"🌡️ {temp} °C · sensación {sensacion} °C")
    else:
        lineas.append(f"🌡️ {temp} °C")

    viento_linea = f"💨 Viento {viento} km/h"
    if direccion:
        viento_linea += f" del {direccion}"
    if rafagas != "—":
        viento_linea += f" · ráfagas {rafagas} km/h"
    lineas.append(viento_linea)

    linea_agua = f"💧 Humedad {humedad}%"
    if lluvia != "—":
        linea_agua += f" · 🌧️ Lluvia {lluvia} mm"
    lineas.append(linea_agua)

    if diario:
        tiempos = list(diario.get("time") or [])
        maximos = list(diario.get("temperature_2m_max") or [])
        minimos = list(diario.get("temperature_2m_min") or [])
        probabilidades = list(diario.get("precipitation_probability_max") or [])
        codigos = list(diario.get("weather_code") or [])
        etiquetas = ("Hoy", "Mañana", "Pasado")
        for i in range(min(3, len(tiempos))):
            maximo = _num(maximos[i] if i < len(maximos) else None)
            minimo = _num(minimos[i] if i < len(minimos) else None)
            parte = f"📅 {etiquetas[i]}: {maximo}° / {minimo}°"
            if i < len(codigos) and emoji_de_codigo:
                # Un emoji no puede romper el clima: si el traductor falla, se sigue sin él.
                with contextlib.suppress(Exception):
                    parte += f" · {emoji_de_codigo(codigos[i])}"
            if i < len(probabilidades) and probabilidades[i] is not None:
                parte += f" · lluvia {_num(probabilidades[i], '.0f')}%"
            lineas.append(parte)

    return "\n".join(lineas)
