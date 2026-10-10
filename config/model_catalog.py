#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Lector del catálogo de modelos por rol
# ═══════════════════════════════════════════════════════════════
# ✅ Fase 1 del plan de contenedores (`docs/PLAN_CONTENEDORES.md` §3.2 y §5-B).
#
# Para qué: el preflight necesita saber, ANTES de arrancar, qué falta instalar, cuánto
# ocupa, qué se rompe si falta y con qué comando se baja. Eso vive en
# `config/model_catalog.yaml` y este módulo es su ÚNICO lector (R1: nada de configuración
# sin lector y sin prueba).
#
# Lo que este módulo NO hace: decidir el ruteo. El modelo de cada rol se elige en
# `config/models.yaml` / `config/settings.py`; acá sólo hay metadatos de instalación.
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_CATALOGO_POR_DEFECTO = Path(__file__).resolve().parent / "model_catalog.yaml"

# Caché por (ruta, fecha de modificación): leer un YAML en cada consulta del preflight
# sería un peaje repetido (R7: nada de trabajo permanente).
_cache: Dict[tuple, "CatalogoDeModelos"] = {}


@dataclass(frozen=True)
class Modelo:
    """Un modelo instalable del catálogo, con sus números MEDIDOS."""

    nombre: str
    roles: List[str]
    obligatorio: bool
    tipo: str                # "ollama" | "piper"
    disco_gb: float
    ram_gb: float
    para: str
    si_falta: str
    instalar: str
    archivo: Optional[str] = None

    @property
    def rol_principal(self) -> str:
        return self.roles[0] if self.roles else ""


@dataclass(frozen=True)
class RequisitosDeHardware:
    """Memoria POR NIVELES (no "cumple o no cumple").

    Decisión del usuario (2026-10-04): con menos de 16 GB AXIOMA **igual funciona**, con
    restricciones; 16 GB es "funciona bien"; 24-32 GB es lo ideal.
    """

    ram_para_funcionar_gb: float      # por debajo de esto no entra el modelo de chat
    ram_bien_gb: float                # funciona bien (recomendado): 16 GB
    ram_ideal_gb: float               # ideal: 24 GB (32 = máxima comodidad)
    ram_margen_gb: float
    disco_factor: float
    ram_nucleo_mb: float
    restricciones: Dict[str, str] = field(default_factory=dict)
    nota: str = ""

    def nivel(self, ram_gb: float) -> str:
        """Nivel de memoria del equipo: `no_alcanza` | `restringido` | `bien` | `ideal`."""
        if ram_gb <= 0:
            return "desconocido"
        if ram_gb < self.ram_para_funcionar_gb:
            return "no_alcanza"
        if ram_gb < self.ram_bien_gb:
            return "restringido"
        if ram_gb < self.ram_ideal_gb:
            return "bien"
        return "ideal"


@dataclass(frozen=True)
class CatalogoDeModelos:
    por_rol: Dict[str, str]
    modelos: Dict[str, Modelo]
    hardware: RequisitosDeHardware
    version: int = 1

    def modelo_de_rol(self, rol: str) -> Optional[Modelo]:
        """Modelo del catálogo que cumple ese rol (None si el rol no está catalogado)."""
        nombre = self.por_rol.get(rol)
        return self.modelos.get(nombre) if nombre else None

    def obligatorios(self) -> List[Modelo]:
        """Modelos sin los que AXIOMA no puede hacer su trabajo principal."""
        return [m for m in self.modelos.values() if m.obligatorio]

    def espacio_para_descargar_gb(self, faltantes: List[Modelo]) -> float:
        """Espacio en disco que hay que tener libre para bajar esos modelos (con margen)."""
        return round(sum(m.disco_gb for m in faltantes) * self.hardware.disco_factor, 2)


def _numero(datos: Dict[str, Any], clave: str, donde: str) -> float:
    valor = datos.get(clave)
    if not isinstance(valor, (int, float)) or isinstance(valor, bool) or valor <= 0:
        raise ValueError(
            f"catálogo de modelos: '{donde}' necesita '{clave}' como número positivo "
            f"(encontrado: {valor!r})"
        )
    return float(valor)


def _catalogo_desde_dict(datos: Dict[str, Any], ruta: Path) -> CatalogoDeModelos:
    """Valida y arma el catálogo. Falla con mensaje claro si el YAML está mal.

    Se valida al leer y NO se degrada en silencio: un catálogo mal escrito haría que el
    preflight mienta sobre lo que falta (que es justo lo que hay que evitar).
    """
    if not isinstance(datos, dict):
        raise ValueError(f"catálogo de modelos: el YAML de {ruta} no es un diccionario")
    por_rol = datos.get("por_rol")
    crudos = datos.get("modelos")
    if not isinstance(por_rol, dict) or not por_rol:
        raise ValueError(f"catálogo de modelos: falta la sección 'por_rol' en {ruta}")
    if not isinstance(crudos, dict) or not crudos:
        raise ValueError(f"catálogo de modelos: falta la sección 'modelos' en {ruta}")

    modelos: Dict[str, Modelo] = {}
    for nombre, m in crudos.items():
        if not isinstance(m, dict):
            raise ValueError(f"catálogo de modelos: '{nombre}' no es un diccionario")
        roles = m.get("roles")
        if not isinstance(roles, list) or not roles:
            raise ValueError(f"catálogo de modelos: '{nombre}' no declara 'roles'")
        desconocidos = [r for r in roles if r not in por_rol]
        if desconocidos:
            raise ValueError(
                f"catálogo de modelos: '{nombre}' tiene roles que no están en 'por_rol': "
                f"{desconocidos}"
            )
        for obligatoria in ("obligatorio", "tipo", "para", "si_falta", "instalar"):
            if obligatoria not in m:
                raise ValueError(f"catálogo de modelos: a '{nombre}' le falta '{obligatoria}'")
        modelos[nombre] = Modelo(
            nombre=nombre,
            roles=list(roles),
            obligatorio=bool(m["obligatorio"]),
            tipo=str(m["tipo"]),
            disco_gb=_numero(m, "disco_gb", nombre),
            ram_gb=_numero(m, "ram_gb", nombre),
            para=str(m["para"]),
            si_falta=str(m["si_falta"]),
            instalar=str(m["instalar"]),
            archivo=m.get("archivo"),
        )

    # Todo rol declarado tiene que existir en 'modelos' (si no, el preflight no sabría
    # qué bajar para ese rol).
    sin_modelo = [r for r, n in por_rol.items() if n not in modelos]
    if sin_modelo:
        raise ValueError(
            f"catálogo de modelos: estos roles apuntan a un modelo que no está en "
            f"'modelos': {sin_modelo}"
        )

    hw = datos.get("hardware") or {}
    restricciones = hw.get("restricciones") or {}
    if not isinstance(restricciones, dict):
        raise ValueError("catálogo de modelos: 'hardware.restricciones' tiene que ser un mapa")
    hardware = RequisitosDeHardware(
        ram_para_funcionar_gb=_numero(hw, "ram_para_funcionar_gb", "hardware"),
        ram_bien_gb=_numero(hw, "ram_bien_gb", "hardware"),
        ram_ideal_gb=_numero(hw, "ram_ideal_gb", "hardware"),
        ram_margen_gb=_numero(hw, "ram_margen_gb", "hardware"),
        disco_factor=_numero(hw, "disco_factor", "hardware"),
        ram_nucleo_mb=float(hw.get("ram_nucleo_mb", 0) or 0),
        restricciones={str(k): str(v) for k, v in restricciones.items()},
        nota=str(hw.get("nota", "")),
    )
    if not (hardware.ram_para_funcionar_gb <= hardware.ram_bien_gb <= hardware.ram_ideal_gb):
        raise ValueError(
            "catálogo de modelos: los niveles de memoria están al revés "
            f"(tienen que ir de menor a mayor: funcionar={hardware.ram_para_funcionar_gb}, "
            f"bien={hardware.ram_bien_gb}, ideal={hardware.ram_ideal_gb})")
    version = int(datos.get("version", 1) or 1)
    return CatalogoDeModelos(por_rol=dict(por_rol), modelos=modelos, hardware=hardware,
                             version=version)


def cargar_catalogo(ruta: Optional[Path] = None, usar_cache: bool = True) -> CatalogoDeModelos:
    """Lee y valida el catálogo de modelos.

    Args:
        ruta: YAML a leer (por defecto `config/model_catalog.yaml`).
        usar_cache: reutiliza la lectura si el archivo no cambió.

    Raises:
        FileNotFoundError: si el catálogo no existe.
        ValueError: si está mal escrito (con el detalle de qué falta).
    """
    archivo = Path(ruta) if ruta else _CATALOGO_POR_DEFECTO
    if usar_cache:
        try:
            clave = (str(archivo), archivo.stat().st_mtime_ns)
        except OSError:
            clave = None
        if clave and clave in _cache:
            return _cache[clave]

    if not archivo.exists():
        raise FileNotFoundError(f"catálogo de modelos: no existe {archivo}")
    inicio = time.time()
    with archivo.open(encoding="utf-8") as fh:
        datos = yaml.safe_load(fh)
    catalogo = _catalogo_desde_dict(datos, archivo)
    logger.debug(f"[catálogo] {len(catalogo.modelos)} modelos leídos en "
                 f"{(time.time() - inicio) * 1000:.1f} ms")
    if usar_cache and clave:
        _cache[clave] = catalogo
    return catalogo


def modelos_efectivos(catalogo: "CatalogoDeModelos"):
    """[(rol, modelo)] según lo que el USUARIO configuró, no los nombres fijos del catálogo.

    ✅ 2026-10-10: MEDIDO: el catálogo tenía los nombres fijos, así que si alguien configuraba otro modelo
    en su `.env` —que es la idea del proyecto: escanear el equipo, sugerir y ELEGIR— el preflight seguía
    exigiéndole `qwen3:8b`. Esto devuelve los roles apuntando a lo configurado; lo que el catálogo no
    conoce se agrega como entrada **estimada** (y así lo dice su texto), para que el preflight pueda
    informar qué falta sin mentir con un número medido que no tiene.
    """
    elegidos = {
        "chat": getattr(_settings(), "llm_default_model", ""),
        "codigo": getattr(_settings(), "llm_code_model", ""),
        "codigo_respaldo": getattr(_settings(), "llm_code_model_fallback", ""),
        "vision": getattr(_settings(), "llm_vision_model", ""),
    }
    salida = []
    for rol, por_defecto in catalogo.por_rol.items():
        elegido = str(elegidos.get(rol, "") or "").strip()
        nombre = elegido or por_defecto
        modelo = catalogo.modelos.get(nombre)
        if modelo is None and elegido:
            modelo = _modelo_estimado(nombre, rol, obligatorio=bool(
                (catalogo.modelos.get(por_defecto) or Modelo("", [], False, "", 0.0, 0.0, "", "", "")).obligatorio))
        if modelo is not None:
            salida.append((rol, modelo))
    return salida


def _settings():
    """La configuración, importada tarde (evita dependencias circulares al cargar este módulo)."""
    from config.settings import settings
    return settings


def _modelo_estimado(nombre: str, rol: str, obligatorio: bool) -> "Modelo":
    """Modelo elegido por el usuario que el catálogo NO conoce: se ESTIMA por el nombre (`:7b` ≈ 4,5 GB)."""
    import re
    m = re.search(r"[:\-_](\d+(?:\.\d+)?)b\b", nombre.lower())
    params = float(m.group(1)) if m else 0.0
    ram = round(params * 0.65 + 0.8, 2) if params else 0.0
    return Modelo(nombre=nombre, roles=[rol], obligatorio=obligatorio, tipo="ollama",
                  disco_gb=ram, ram_gb=ram,
                  para=f"el que elegiste para {rol} (ESTIMADO por el nombre: sólo se sabe midiéndolo)",
                  si_falta=f"las tareas de {rol} no van a funcionar",
                  instalar=f"ollama pull {nombre}")
