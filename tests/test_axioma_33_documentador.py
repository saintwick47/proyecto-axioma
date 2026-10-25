#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral
# DOCUMENTADOR — regresión de las secciones 5/7/10/13/18, flags y ANSI
# ═══════════════════════════════════════════════════════════════
# Medido en HEAD antes de esta tanda (2026-10-25):
#   · §5 aprobaba TODO por nombre de CLASE: `SmartRouter.select_model()`,
#     `MemoryGateway.search_context()`, `CoderAgent.generate()` y compañía
#     aparecían [✅ CONECTADO] sin existir.
#   · `--validate` imprimía "VALIDACIÓN APROBADA" sin llamar a validate_docs.py.
#   · `--strict-secrets` fallaba abierto si no encontraba `secret_scanner.py`.
#   · 25 líneas del .md tenían secuencias ANSI y §7 truncaba a 40 async de 336.
# Este archivo genera el documento REAL en tmp_path (sin IA, con FlowMapper) y
# verifica el contenido: no usa mocks del generador.
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_DIR = PROJECT_ROOT / "tools" / "documentador"

pytestmark = pytest.mark.skipif(
    not DOC_DIR.is_dir(),
    reason="el documentador no viaja en la copia pública (preparar_publicacion.sh)",
)

if str(DOC_DIR) not in sys.path:
    sys.path.insert(0, str(DOC_DIR))


def _seccion(texto: str, numero: int) -> str:
    """Devuelve el bloque `## <numero>. …` hasta el próximo `##`."""
    marca = f"## {numero}. "
    inicio = texto.index(marca)
    fin = texto.find("\n## ", inicio + len(marca))
    return texto[inicio:fin if fin != -1 else len(texto)]


def _filas_tabla(bloque: str) -> list[str]:
    """Filas de datos de las tablas Markdown del bloque (sin cabecera/separador)."""
    filas = []
    for linea in bloque.splitlines():
        if not linea.startswith("| "):
            continue
        if linea.startswith("| ---") or "--- |" in linea:
            continue
        if "| Paso |" in linea or "| Método |" in linea or "| Atributo |" in linea:
            continue
        filas.append(linea)
    return filas


@pytest.fixture(scope="module")
def documento(tmp_path_factory):
    """Genera el documento una sola vez (sin IA) en un tmp_path real."""
    from axioma_doc_analyzer import ImprovedProjectAnalyzer
    from axioma_doc_md_generator import MarkdownDocGenerator

    analyzer = ImprovedProjectAnalyzer(
        project_root=PROJECT_ROOT,
        use_ollama=False,
        trace_flows=True,
    )
    analyzer.analyze()
    salida = tmp_path_factory.mktemp("documentador") / "AXIOMA_COMPLETE_CONTEXT.md"
    MarkdownDocGenerator(analyzer).generate(salida)
    return SimpleNamespace(
        analyzer=analyzer,
        path=salida,
        text=salida.read_text(encoding="utf-8"),
    )


# ── Encabezado y navegación (D6) ─────────────────────────────────────────────

def test_documento_generado_con_commit_fecha_y_alcance(documento):
    """El doc debe ser trazable: commit, fecha, alcance y qué quedó fuera."""
    assert documento.path.exists() and documento.path.stat().st_size > 100_000
    cabecera = documento.text.split("## Índice")[0]
    assert re.search(r"\*\*Commit documentado:\*\* `[0-9a-f]{7,}`", cabecera), cabecera
    assert "**Generado:**" in cabecera
    assert "**Alcance:**" in cabecera and "archivos Python analizados" in cabecera
    assert "`tests/`" in cabecera and "`deepseek-harness/`" in cabecera
    assert "## Cómo leer este documento" in documento.text
    assert "### Índice de módulos y clases" in documento.text


# ── §5: verificación por método (D1) ─────────────────────────────────────────

def test_seccion_5_usa_metodos_reales_y_no_aprueba_inexistentes(documento):
    """§5 debe listar los métodos que EXISTEN y no marcar ✅ lo que no existe."""
    s5 = _seccion(documento.text, 5)
    reales = [
        "Dispatcher.process()",
        "SmartRouter.route()",
        "ModelRouter.route()",
        "MemoryGateway.get_context()",
        "CoderAgent.execute()",
        "CodeValidator.validate()",
        "MemoryGateway.add_message()",
        "SearcherAgent.execute()",
        "DataHandler.handle()",
        "ExternalAPIManager.search_web()",
    ]
    for componente in reales:
        assert componente in s5, f"falta el método real {componente} en §5"
    filas = _filas_tabla(s5)
    assert len(filas) == 19, f"se esperaban 19 pasos, hay {len(filas)}"
    assert not any("❌ NO ENCONTRADO" in fila for fila in filas), filas
    # Cada fila trae la definición real archivo:línea.
    assert s5.count("src/") >= 19


def test_el_resolver_por_metodo_detecta_los_nombres_viejos(documento):
    """Los 6 métodos que la definición declaraba y NO existen deben dar None."""
    from axioma_doc_analysis_passes import (
        _build_class_method_index,
        _resolve_class_method,
    )

    indice = _build_class_method_index(documento.analyzer)
    inexistentes = [
        ("SmartRouter", "select_model"),
        ("MemoryGateway", "search_context"),
        ("MemoryGateway", "store"),
        ("MemoryGateway", "store_knowledge"),
        ("SearcherAgent", "synthesize"),
        ("CoderAgent", "generate"),
        ("Dispatcher", "dispatch"),
    ]
    for clase, metodo in inexistentes:
        assert _resolve_class_method(indice, clase, metodo) is None, f"{clase}.{metodo}"
    # Y resuelve herencia/mixins, que es el punto del fix.
    assert _resolve_class_method(indice, "Dispatcher", "process")
    assert _resolve_class_method(indice, "IntentClassifierCore", "_preprocess_text")


def test_un_flujo_con_metodo_inexistente_se_renderiza_como_no_encontrado():
    """Aunque la definición mienta, el render debe decir ❌ (nunca ✅)."""
    from axioma_doc_base import CriticalFlow
    from axioma_doc_md_generator import MarkdownDocGenerator

    paso = {
        "step": 1,
        "component": "CoderAgent.generate()",
        "fallback": None,
        "timeout": "1s",
        "method_found": False,
        "method_definition": "",
    }
    flow = CriticalFlow(
        name="FLUJO_INEXISTENTE",
        description="prueba",
        steps=[paso],
        failure_points=[],
        fallback_chain=[],
    )
    fake = SimpleNamespace(
        critical_flows=[flow],
        integration_report=None,
        project_root=PROJECT_ROOT,
    )
    texto = MarkdownDocGenerator(fake)._flujos()
    assert "❌ NO ENCONTRADO" in texto
    assert "✅ CONECTADO" not in texto
    assert "NO ENCONTRADO" in texto.split("### Flujo Inexistente")[1]


# ── §18 y §7 (D2, D10) ───────────────────────────────────────────────────────

def test_seccion_18_presente_con_modulos(documento):
    """Sin --trace-flows §18 quedaba vacía; ahora el default la llena."""
    s18 = _seccion(documento.text, 18)
    assert "Módulos Conectados" in s18
    assert "Módulos Aislados o Parciales" in s18
    assert "`src/" in s18


def test_seccion_7_lista_todos_los_async(documento):
    """§7 ya no corta en 40: la tabla debe tener exactamente todos los async."""
    s7 = _seccion(documento.text, 7)
    assert "se listan todos" in s7
    filas = _filas_tabla(s7)
    a = documento.analyzer
    esperados = sum(
        1 for firmas in a.module_functions.values() for f in firmas if f.is_async
    ) + sum(
        1
        for clases in a.enriched_classes.values()
        for ec in clases
        for m in ec.methods
        if m.is_async
    )
    assert esperados > 100
    assert len(filas) == esperados, f"§7 tiene {len(filas)} filas; async reales {esperados}"


# ── Flags honestos y validación real (D3, D7) ────────────────────────────────

def test_flags_del_cli_son_los_que_existen():
    resultado = subprocess.run(
        [sys.executable, str(DOC_DIR / "axioma_doc_main.py"), "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert resultado.returncode == 0, resultado.stderr
    ayuda = resultado.stdout
    for flag in ("--ollama", "--no-ollama", "--no-trace-flows", "--trace-flows",
                 "--clear-cache", "--validate", "--max-ai-classes",
                 "--strict-secrets", "--output"):
        assert flag in ayuda, f"falta {flag}"
    for flag in ("--force", "--strict-include"):
        assert flag not in ayuda, f"{flag} ya no existe y no debe anunciarse"


def test_validate_usa_el_validador_real_y_falla_bajo_umbral(documento):
    """`validate_docs.py` debe medir cobertura y devolver exit 1 bajo el umbral."""
    validador = DOC_DIR / "validate_docs.py"
    comando = [sys.executable, str(validador), "--doc", str(documento.path)]

    aprobado = subprocess.run(comando + ["--min-coverage", "1"],
                              capture_output=True, text=True, timeout=120)
    assert aprobado.returncode == 0, aprobado.stdout + aprobado.stderr
    assert "Cobertura obtenida" in aprobado.stdout
    assert "Símbolos reales encontrados" in aprobado.stdout

    reprobado = subprocess.run(comando + ["--min-coverage", "99"],
                               capture_output=True, text=True, timeout=120)
    assert reprobado.returncode == 1, reprobado.stdout
    assert "FALLIDO" in reprobado.stdout


def test_clear_cache_borra_solo_la_cache_ia(tmp_path, monkeypatch):
    import axioma_doc_ai

    cache = tmp_path / "cache_ia.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(axioma_doc_ai, "AI_CACHE_FILE", cache)
    assert axioma_doc_ai.clear_ai_cache() is True
    assert not cache.exists()


# ── ANSI y entorno sin data/ (D5, D8) ───────────────────────────────────────

def test_el_documento_no_contiene_secuencias_ansi(documento):
    from axioma_doc_base_utils import strip_ansi

    assert "\x1b" not in documento.text
    assert strip_ansi("resumen\x1b[2D\x1b[Kalgo") == "resumenalgo"


def test_secciones_10_y_13_avisan_si_falta_data():
    from axioma_doc_md_generator import MarkdownDocGenerator

    fake = SimpleNamespace(
        project_root=PROJECT_ROOT / "no_existe_data",
        memory_systems=[],
        config_params=[],
    )
    generador = MarkdownDocGenerator(fake)
    assert "no disponible en este entorno" in generador._memoria()
    assert "no disponible en este entorno" in generador._configuracion()
