#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tests/axioma_report.py
# Autor: SaintWick
# AXIOMA — tests/axioma_report.py
# Helper compartido de la suite integral de verificación.
# Genera la documentación de resultados en logs/ en formato Markdown.
# No es un archivo de test (pytest no lo recolecta).
#
# ✅ v0.6.9v (ISSUE-005): soporte de RUN-ID COMPARTIDO. Antes, cada proceso
# pytest creaba su propio axioma_test_report_<ts>.md y _prune_old_reports()
# borraba los demás → al correr los tests archivo por archivo (un proceso
# por archivo, para poder poner timeout), el reporte final solo reflejaba el
# ÚLTIMO archivo. Ahora, si el entorno define AXIOMA_TEST_RUN_ID, todos los
# procesos de la tanda escriben en el MISMO reporte (acumulando secciones) y
# la poda solo corre al crear el reporte, no en cada append.
# Ver tools/run_all_tests.py (runner que arma la tanda con un run-id).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
REPORT_PREFIX = "axioma_test_report"

_lock = threading.RLock()
_report_path: Path | None = None
_section_open: str | None = None
_totals = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}

# ✅ ISSUE-005: run-id compartido entre procesos de una misma tanda.
RUN_ID = (os.environ.get("AXIOMA_TEST_RUN_ID") or "").strip()
RUN_LABEL = (os.environ.get("AXIOMA_TEST_RUN_LABEL") or "").strip()
_IS_SHARED_RUN = bool(RUN_ID)


def _safe_run_id(value: str) -> str:
    """Sanitiza el run-id para usarlo como nombre de archivo."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", value)[:64] or "run"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _prune_old_reports(keep: int = 1, keep_batches: int = 2) -> None:
    """✅ 2026-08-29: conserva solo los N reportes más recientes en logs/.

    Cada corrida genera axioma_test_report_<ts>.md; sin poda se acumulan
    decenas de reportes obsoletos (pedido: solo el más actual).

    ⚠️ v0.6.9v (bug encontrado por el quality gate): el glob
    `axioma_test_report_*` también matcheaba los reportes **consolidados de la
    tanda** (`axioma_test_report_batch_<run_id>.md`, que escribe
    `tools/run_all_tests.py`) y los BORRABA: correr un solo archivo de test
    después de la suite completa eliminaba la evidencia consolidada (justo la
    que necesita el gate, con `run_id` y `pytest exitstatus`). Ahora se podan
    por separado: los de un archivo (keep=1) y los consolidados (keep=2).
    """
    try:
        reports = sorted(
            LOGS_DIR.glob(f"{REPORT_PREFIX}_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        batches = [p for p in reports if "_batch_" in p.name]
        singles = [p for p in reports if "_batch_" not in p.name]
        for old in singles[keep:]:
            old.unlink(missing_ok=True)
        for old in batches[keep_batches:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def report_path() -> Path:
    """Devuelve la ruta del reporte (se crea una única vez con cabecera)."""
    global _report_path
    with _lock:
        if _report_path is None:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            if _IS_SHARED_RUN:
                # Una sola ruta para toda la tanda (todos los procesos suman acá).
                _report_path = LOGS_DIR / f"{REPORT_PREFIX}_{_safe_run_id(RUN_ID)}.md"
                already = _report_path.exists() and _report_path.stat().st_size > 0
            else:
                _report_path = LOGS_DIR / f"{REPORT_PREFIX}_{_stamp()}.md"
                already = False

            if not already:
                _report_path.touch()  # el nuevo debe existir ANTES de podar
                _prune_old_reports()  # solo al CREAR, no en cada append de la tanda
                try:
                    import platform
                    py = sys.version.split()[0]
                    _write(
                        f"# Informe de Verificación Integral — AXIOMA\n\n"
                        f"- **Fecha:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                        f"- **Python:** {py} ({platform.machine()})\n"
                        f"- **Proyecto:** {PROJECT_ROOT}\n"
                        f"- **Comando:** `pytest tests/`\n"
                        f"- **Suite:** verificación integral de `config/`, `src/` y estructura del proyecto\n"
                        + (f"- **Tanda (run-id):** `{RUN_ID}` — los resultados de todos los\n"
                           f"  archivos de la tanda se acumulan en ESTE archivo.\n" if _IS_SHARED_RUN else "")
                        + f"\n---\n\n"
                    )
                except Exception:
                    pass
            elif _IS_SHARED_RUN:
                # Proceso siguiente de la misma tanda: marcar dónde empieza su bloque.
                label = RUN_LABEL or f"pid {os.getpid()}"
                _write(f"\n---\n\n# ▶ Corrida: `{label}`\n")
        return _report_path


def _write(text: str) -> None:
    with _lock:
        with open(report_path(), "a", encoding="utf-8") as fh:
            fh.write(text)


def section(title: str) -> None:
    """Abre una sección del reporte (solo escribe si cambia la sección)."""
    global _section_open
    with _lock:
        if _section_open == title:
            return
        _section_open = title
    _write(f"\n## {title}\n\n| Check | Resultado | Detalle |\n|---|---|---|\n")


def check(name: str, ok: bool, detail: str = "", section_title: str | None = None) -> bool:
    """Registra un check individual. Devuelve ok (para assert opcional)."""
    if section_title is not None and section_title != _section_open:
        section(section_title)
    status = "✅ PASS" if ok else "❌ FAIL"
    key = "passed" if ok else ("errors" if detail.startswith("ERROR:") else "failed")
    with _lock:
        _totals[key] += 1
    safe = (detail or "").replace("|", "\\|").replace("\n", " ")[:400]
    safe_name = name.replace("|", "\\|")
    _write(f"| {safe_name} | {status} | {safe} |\n")
    return ok


def skipped(name: str, detail: str = "") -> bool:
    with _lock:
        _totals["skipped"] += 1
    _write(f"| {name} | ⏭️ SKIP | {(detail or '').replace('|', '\\\\|')[:300]} |\n")
    return True


def totals() -> dict:
    with _lock:
        return dict(_totals)


def finalize(extra: str = "", pytest_exitstatus: int | None = None) -> None:
    """Escribe el resumen final del reporte.

    ✅ ISSUE-005: en una tanda compartida (run-id) el resumen es POR CORRIDA
    ("### Resultado de esta corrida"), de modo que los procesos siguientes
    NO pisan el resumen de los anteriores — todos quedan en el mismo archivo.

    ✅ v0.6.9v: el veredicto ahora también contempla el resultado REAL de
    pytest (`pytest_exitstatus`). Antes solo miraba los checks registrados con
    `check()`; un archivo que usa `assert` de pytest y FALLA dejaba el total en
    0 y el veredicto decía "TODO CORRECTO" (reporte engañoso).
    """
    t = totals()
    total = t["passed"] + t["failed"] + t["skipped"] + t["errors"]
    ok_run = t["failed"] == 0 and t["errors"] == 0
    # exitstatus 0 = OK, 5 = sin tests recolectados; cualquier otro = fallo real.
    pytest_ok = pytest_exitstatus is None or pytest_exitstatus in (0, 5)
    if not pytest_ok:
        ok_run = False
    verdict = "✅ TODO CORRECTO" if ok_run else "❌ HAY FALLOS"
    heading = "### Resultado de esta corrida" if _IS_SHARED_RUN else "## Resumen final"
    summary = (
        f"\n---\n\n{heading}\n\n"
        f"- ✅ Pasaron (checks): **{t['passed']}**\n"
        f"- ❌ Fallaron (checks): **{t['failed']}**\n"
        f"- ⚠️  Errores (checks): **{t['errors']}**\n"
        f"- ⏭️  Saltados (checks): **{t['skipped']}**\n"
        f"- Total checks registrados: **{total}**\n"
    )
    if pytest_exitstatus is not None:
        summary += f"- 🧪 pytest exitstatus: **{pytest_exitstatus}**" + (
            " (OK)\n" if pytest_ok else " ← hay tests FALLIDOS\n")
    summary += f"- **Veredicto: {verdict}**\n"
    if _IS_SHARED_RUN and RUN_LABEL:
        summary = f"\n> Corrida: `{RUN_LABEL}`\n" + summary
    if extra:
        summary += f"\n{extra}\n"
    _write(summary)
