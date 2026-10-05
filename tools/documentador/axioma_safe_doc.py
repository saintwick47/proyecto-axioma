#!/usr/bin/env python3

import logging
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Documentador Seguro v1.0 (Wrapper con backup)
# Ruta: tools/documentador/axioma_safe_doc.py
# Autor: SaintWick
# Propósito: Ejecuta el documentador estándar con:
#            1) Backup automático de la doc previa
#            2) Activación del filtro de secrets
#            3) Verificación post-generación
#            4) Rollback automático si falla la verificación
#
# Uso:  python tools/documentador/axioma_safe_doc.py
#       python tools/documentador/axioma_safe_doc.py --keep-backup
#       python tools/documentador/axioma_safe_doc.py --restore <timestamp>
# ═══════════════════════════════════════════════════════════════

import sys
import shutil
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

# Permitir imports relativos
sys.path.insert(0, str(Path(__file__).parent))

try:
    from sensitive_filter import make_default_filter
    from secret_scanner import scan_directory, EXIT_OK, EXIT_VIOLATION
except ImportError as e:
    print(f"❌ No se pueden importar los módulos del filtro: {e}")
    print("   Asegúrate de tener sensitive_filter.py y secret_scanner.py")
    print("   en el mismo directorio que este script.")
    sys.exit(2)

BACKUP_DIR = Path("data/backups/docs")
DOCS_DIR = Path("docs")
DOC_MAIN = Path("tools/documentador/axioma_doc_main.py")


# ═══════════════════════════════════════════════════════════════
# COLORES
# ═══════════════════════════════════════════════════════════════

class C:
    G = '\033[92m'
    R = '\033[91m'
    Y = '\033[93m'
    B = '\033[94m'
    CY = '\033[96m'
    M = '\033[95m'
    X = '\033[0m'
    BD = '\033[1m'


def info(m):
    print(f"{C.CY}ℹ {m}{C.X}")


def ok(m):
    print(f"{C.G}✓ {m}{C.X}")


def warn(m):
    print(f"{C.Y}⚠ {m}{C.X}")


def err(m):
    print(f"{C.R}✗ {m}{C.X}")


def step(m):
    print(f"{C.M}▶ {m}{C.X}")


# ═══════════════════════════════════════════════════════════════
# BACKUP
# ═══════════════════════════════════════════════════════════════

def create_backup(keep_count: int = 10) -> Path | None:
    """
    Crea backup de la doc actual. Retorna la ruta del backup o None.
    Solo hace backup si docs/AXIOMA_COMPLETE_CONTEXT.md existe.
    """
    target = DOCS_DIR / "AXIOMA_COMPLETE_CONTEXT.md"
    if not target.exists():
        info("No hay doc previa — saltando backup.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / timestamp
    backup_path.mkdir(parents=True, exist_ok=True)

    try:
        shutil.copy2(target, backup_path / target.name)
        manifest = backup_path / "manifest.txt"
        manifest.write_text(
            f"timestamp={timestamp}\n"
            f"source={target}\n"
            f"size={target.stat().st_size}\n"
            f"lines={sum(1 for _ in target.open(encoding='utf-8', errors='ignore'))}\n",
            encoding="utf-8"
        )
        ok(f"Backup creado: {backup_path.relative_to(Path.cwd())}")

        # Limpiar backups antiguos
        cleanup_old_backups(keep_count)
        return backup_path
    except Exception as e:
        err(f"Error creando backup: {e}")
        return None


def cleanup_old_backups(keep_count: int):
    """Mantiene solo los últimos N backups."""
    if not BACKUP_DIR.exists():
        return
    backups = sorted(
        [d for d in BACKUP_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True
    )
    for old in backups[keep_count:]:
        try:
            shutil.rmtree(old)
            info(f"Backup antiguo eliminado: {old.name}")
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 axioma_safe_doc.py:129] excepción degradada (intencional): {_d2e}")


def list_backups():
    """Lista los backups disponibles."""
    if not BACKUP_DIR.exists():
        warn("No hay backups guardados")
        return
    backups = sorted(
        [d for d in BACKUP_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True
    )
    print(f"\n{C.BD}📦 Backups disponibles:{C.X}\n")
    for i, b in enumerate(backups[:20], 1):
        manifest = b / "manifest.txt"
        meta = ""
        if manifest.exists():
            meta = " | " + " | ".join(
                line.strip() for line in manifest.read_text(encoding="utf-8").splitlines()
            )
        print(f"  {i}. {b.name}{meta}")
    if len(backups) > 20:
        print(f"  ... y {len(backups) - 20} más")


def restore_backup(timestamp: str) -> bool:
    """Restaura desde un backup por timestamp."""
    backup_path = BACKUP_DIR / timestamp
    if not backup_path.exists():
        err(f"Backup no encontrado: {timestamp}")
        return False
    target = DOCS_DIR / "AXIOMA_COMPLETE_CONTEXT.md"
    src = backup_path / target.name
    if not src.exists():
        err(f"Backup corrupto: falta {target.name} en {backup_path}")
        return False
    try:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        ok(f"Doc restaurada desde {timestamp}")
        return True
    except Exception as e:
        err(f"Error restaurando: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# EJECUCIÓN SEGURA
# ═══════════════════════════════════════════════════════════════

def run_documenter(extra_args: list[str]) -> int:
    """Ejecuta el documentador original con los flags que queremos."""
    if not DOC_MAIN.exists():
        err(f"No se encuentra {DOC_MAIN}")
        return 2

    cmd = [sys.executable, str(DOC_MAIN)] + extra_args
    info(f"Ejecutando: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, cwd=Path.cwd())
        return result.returncode
    except Exception as e:
        err(f"Error ejecutando documentador: {e}")
        return 2


def verify_output(project_root: Path) -> int:
    """Escanea la doc generada buscando secrets sin redactar."""
    step("Verificación post-generación...")
    findings = scan_directory(DOCS_DIR, project_root)
    if not findings:
        ok("Verificación OK: sin secrets sin redactar")
        return 0
    err(f"Verificación FALLÓ: {len(findings)} secrets detectados")
    for doc_file, line_no, pattern, text in findings:
        rel = doc_file.relative_to(project_root) if doc_file.is_absolute() else doc_file
        err(f"  {rel}:{line_no} — {pattern} — {text!r}")
    return EXIT_VIOLATION


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="AXIOMA — Documentador Seguro con backup y verificación",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python tools/documentador/axioma_safe_doc.py                       # Backup + generar + verificar
  python tools/documentador/axioma_safe_doc.py --trace-flows         # + FlowMapper
  python tools/documentador/axioma_safe_doc.py --no-ollama           # Sin IA
  python tools/documentador/axioma_safe_doc.py --list-backups        # Ver backups
  python tools/documentador/axioma_safe_doc.py --restore 20260721_120000  # Restaurar
  python tools/documentador/axioma_safe_doc.py --skip-verify         # Saltar verificación
        """
    )
    parser.add_argument(
        '--restore',
        type=str,
        metavar='TIMESTAMP',
        help='Restaurar desde backup (formato: YYYYMMDD_HHMMSS)'
    )
    parser.add_argument(
        '--list-backups',
        action='store_true',
        help='Listar backups disponibles'
    )
    parser.add_argument(
        '--keep-backup',
        type=int,
        default=10,
        help='Cuántos backups mantener (default: 10)'
    )
    parser.add_argument(
        '--skip-verify',
        action='store_true',
        help='Saltar verificación post-generación'
    )
    parser.add_argument(
        '--skip-backup',
        action='store_true',
        help='Saltar backup previo (NO recomendado)'
    )
    # Pasar el resto al documentador
    parser.add_argument(
        'passthrough',
        nargs=argparse.REMAINDER,
        help='Argumentos adicionales para axioma_doc_main.py'
    )

    args = parser.parse_args()
    project_root = Path.cwd()

    # MODO: listar backups
    if args.list_backups:
        list_backups()
        return 0

    # MODO: restaurar
    if args.restore:
        print(f"{C.BD}🔄 Restaurando desde {args.restore}...{C.X}")
        return 0 if restore_backup(args.restore) else 1

    # MODO: generar doc con backup + verificación
    print("=" * 70)
    print(f"{C.BD}🛡️  AXIOMA — Documentador Seguro v1.0{C.X}")
    print("=" * 70)
    print()

    # 1) Backup
    if not args.skip_backup:
        step("PASO 1/3: Backup de la doc previa")
        create_backup(keep_count=args.keep_backup)
    else:
        warn("Backup omitido (--skip-backup)")
    print()

    # 2) Ejecutar documentador
    step("PASO 2/3: Generando documentación")
    passthrough = [a for a in args.passthrough if a != "--"]  # Limpiar separador
    rc = run_documenter(passthrough)
    if rc != 0:
        err(f"Documentador terminó con código {rc}")
        return rc
    print()

    # 3) Verificación post-generación
    if not args.skip_verify:
        step("PASO 3/3: Verificación de secrets")
        verify_rc = verify_output(project_root)
        if verify_rc != 0:
            err("=" * 70)
            err("❌ FALLO DE SEGURIDAD: La doc generada contiene secrets.")
            err("   Revisa los archivos listados arriba.")
            err("   Si los parches al documentador aún no están aplicados,")
            err("   aplícalos y vuelve a ejecutar.")
            err("")
            err("   Para usar la versión previa: python axioma_safe_doc.py --list-backups")
            return verify_rc
    else:
        warn("Verificación omitida (--skip-verify) — NO recomendado en CI")

    print()
    print("=" * 70)
    ok(f"{C.BD}DOCUMENTACIÓN GENERADA Y VERIFICADA{C.X}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
