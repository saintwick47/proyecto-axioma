#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.8 — Resolución Centralizada de Rutas Absolutas
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/config/paths.py
# Autor: SaintWick
# Fecha: 2026-03-14  # ✅ VERIFICADO: Rutas correctas
# Propósito: Rutas absolutas resueltas y validadas para todo el sistema
# ═══════════════════════════════════════════════════════════════
import sys
from pathlib import Path
from typing import Union, Dict, Any

# Añadir el directorio raíz al path para importaciones
ROOT_DIR = Path(__file__).parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from config.settings import settings
except ImportError as e:
    print(f"❌ Error importando settings: {e}")
    print(f"📂 ROOT_DIR: {ROOT_DIR}")
    print(f"📂 sys.path: {sys.path}")
    raise


class Paths:
    """
    Rutas absolutas resueltas y validadas para todo el sistema AXIOMA.

    Uso:
        >>> from config.paths import Paths
        >>> print(Paths.ROOT)      # la carpeta donde está instalado AXIOMA
        >>> print(Paths.DATA)      # <raíz>/data
        >>> print(Paths.MEMORY)    # <raíz>/data/memory
        >>> print(Paths.LOGS)      # <raíz>/logs

    Nota: Los directorios se crean explícitamente desde main.py, no al importar.
    """

    # ═══════════════════════════════════════════════════════════
    # RAÍZ DEL PROYECTO
    # ═══════════════════════════════════════════════════════════
    ROOT: Path = settings.base_dir

    # ═══════════════════════════════════════════════════════════
    # DIRECTORIOS PRINCIPALES
    # ═══════════════════════════════════════════════════════════
    # ✅ VERIFICADO: Sin espacios en strings
    SRC: Path = ROOT / "src"
    CONFIG: Path = settings.config_dir
    DATA: Path = settings.data_dir
    LOGS: Path = settings.logs_dir
    TESTS: Path = ROOT / "tests"
    TOOLS: Path = ROOT / "tools"
    DOCS: Path = ROOT / "docs"

    # ═══════════════════════════════════════════════════════════
    # SUBDIRECTORIOS DE DATA
    # ═══════════════════════════════════════════════════════════
    # ✅ VERIFICADO: MEMORY = DATA / "memory" → /axioma/data/memory
    MEMORY: Path = DATA / "memory"
    BACKUPS: Path = DATA / "backups"
    KNOWLEDGE: Path = DATA / "knowledge"
    CACHE: Path = DATA / "cache"

    # ═══════════════════════════════════════════════════════════
    # SUBDIRECTORIOS DE LOGS
    # ═══════════════════════════════════════════════════════════
    AUDIT_LOG: Path = LOGS / "audit.json"

    # ═══════════════════════════════════════════════════════════
    # INTERFACES
    # ═══════════════════════════════════════════════════════════
    # ✅ VERIFICADO: Sin espacios en strings
    WEB_STATIC: Path = SRC / "interfaces" / "web" / "static"

    # ═══════════════════════════════════════════════════════════
    # MÉTODOS DE UTILIDAD
    # ═══════════════════════════════════════════════════════════
    @classmethod
    def ensure_exists(cls, *paths: Path) -> None:
        """
        Crea directorios si no existen.

        Args:
            *paths: Paths a crear

        Example:
            >>> Paths.ensure_exists(Paths.DATA, Paths.LOGS)
        """
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def resolve(cls, path: Union[str, Path]) -> Path:
        """
        Resuelve ruta relativa a absoluta desde ROOT.

        Args:
            path: Ruta relativa o absoluta

        Returns:
            Ruta absoluta resuelta

        Example:
            >>> Paths.resolve("data/memory")
            PosixPath('<raíz del proyecto>/data/memory')
        """
        p = Path(path)
        return p if p.is_absolute() else (cls.ROOT / p).resolve()

    @classmethod
    def validate_writable(cls, path: Path) -> bool:
        """
        Verifica que una ruta sea escribible.

        Args:
            path: Ruta a validar

        Returns:
            True si es escribible, False si no

        Example:
            >>> Paths.validate_writable(Paths.DATA)
            True
        """
        try:
            test_file = path / ".axioma_write_test"
            test_file.touch(exist_ok=True)
            test_file.unlink()
            return True
        except (OSError, PermissionError):
            return False

    @classmethod
    def verify_paths(cls) -> Dict[str, Any]:
        """
        ✅ NUEVO: Verifica que todas las rutas sean correctas.

        Returns:
            Dict con estado de cada ruta

        Example:
            >>> status = Paths.verify_paths()
            >>> print(status['MEMORY']['correct'])
            True
        """
        results: Dict[str, Any] = {}
        expected_memory = settings.base_dir / "data" / "memory"

        results['ROOT'] = {
            'value': str(cls.ROOT),
            'expected': str(settings.base_dir),
            'correct': cls.ROOT == settings.base_dir
        }

        results['DATA'] = {
            'value': str(cls.DATA),
            'expected': str(settings.data_dir),
            'correct': cls.DATA == settings.data_dir
        }

        results['MEMORY'] = {
            'value': str(cls.MEMORY),
            'expected': str(expected_memory),
            'correct': cls.MEMORY == expected_memory
        }

        results['LOGS'] = {
            'value': str(cls.LOGS),
            'expected': str(settings.logs_dir),
            'correct': cls.LOGS == settings.logs_dir
        }

        # Verificar rutas adicionales
        results['SRC'] = {
            'value': str(cls.SRC),
            'expected': str(settings.base_dir / "src"),
            'correct': cls.SRC == settings.base_dir / "src"
        }

        results['CONFIG'] = {
            'value': str(cls.CONFIG),
            'expected': str(settings.config_dir),
            'correct': cls.CONFIG == settings.config_dir
        }

        results['all_correct'] = all(
            r['correct'] for r in results.values()
            if isinstance(r, dict) and 'correct' in r
        )

        return results

    @classmethod
    def get_all_paths(cls) -> Dict[str, Path]:
        """
        Retorna un diccionario con todas las rutas definidas.

        Returns:
            Dict con nombre de ruta y su Path correspondiente
        """
        return {
            name: value for name, value in cls.__dict__.items()
            if isinstance(value, Path) and not name.startswith('_')
        }


# ═══════════════════════════════════════════════════════════════
# VERIFICACIÓN AL EJECUTAR COMO SCRIPT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Ejecutar verificación si se ejecuta como script
    print("═" * 60)
    print("🔍 VERIFICACIÓN DE RUTAS - AXIOMA paths.py")
    print("═" * 60)
    print(f"📂 Directorio raíz detectado: {ROOT_DIR}")
    print(f"📂 Config dir: {Paths.CONFIG}")
    print("═" * 60)

    status = Paths.verify_paths()

    for key, info in status.items():
        if isinstance(info, dict) and key != 'all_correct':
            icon = "✅" if info['correct'] else "❌"
            print(f"{icon} {key}:")
            print(f"   Valor:    {info['value']}")
            print(f"   Esperado: {info['expected']}")
            print(f"   Estado:   {'CORRECTO' if info['correct'] else 'INCORRECTO'}")
            print()

    print("═" * 60)
    if status['all_correct']:
        print("✅ TODAS LAS RUTAS SON CORRECTAS")
    else:
        print("❌ HAY RUTAS INCORRECTAS - REVISAR settings.py")
    print("═" * 60)

    # Mostrar todas las rutas disponibles
    print("\n📁 RUTAS DISPONIBLES:")
    for name, path in Paths.get_all_paths().items():
        print(f"   {name}: {path}")
    print("═" * 60)


# ═══════════════════════════════════════════════════════════════
# VERIFICACIÓN DE INTEGRIDAD
# ═══════════════════════════════════════════════════════════════
# ✅ El módulo ahora maneja correctamente el path de importación
# ✅ Se puede ejecutar directamente sin PYTHONPATH
# ✅ Mantiene compatibilidad con importaciones normales
# ═══════════════════════════════════════════════════════════════
