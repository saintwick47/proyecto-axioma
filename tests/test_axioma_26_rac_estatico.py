#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — test_axioma_26_rac_estatico.py
# Ruta: tests/test_axioma_26_rac_estatico.py
#
# ✅ v0.6.9w: Suite del FIX RAC anti-falsos-positivos.
#   Cubre: lectura completa, validación `ast`, chunking en fronteras de nodos,
#   filtro de afirmaciones refutadas y ejecución end-to-end con LLM falso
#   (sin Ollama, sin VRAM, sin sleeps reales).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import ast
import time
import types
from pathlib import Path

import pytest

from src.agents import analyst as analyst_mod
from src.agents import rac_static as rs

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ARCHIVOS_GRANDES = [
    "src/core/dispatcher_process.py",
    "src/core/task_queue.py",
    "src/core/memory_guard.py",
    "src/agents/analyst.py",
]

VALIDA = '''"""Módulo de prueba."""


class Memoria:
    """Guard de memoria de prueba."""

    def reserve(self, mb: int) -> int:
        """Reserva memoria."""
        return mb

    def release(self) -> bool:
        """Libera memoria."""
        return True
'''

ROTA = """class Roto:
    def f(self):
        pass
  def g(self):
    pass
"""

RESPUESTA_LLM = (
    "### Errores Críticos\n"
    "- La clase `Memoria` no se cierra correctamente (línea 1).\n"
    "  - Detalle: falta el cierre de la clase.\n"
    "- Falta el método `release()` (línea 1).\n"
    "- El import de `os` está truncado.\n"
    "- Posible carrera en `reserve` (línea 8): no usa lock.\n"
)


# ═══════════════════════════════════════════════════════════════
# 1. LECTURA COMPLETA (regresión del bug `f.read(1500)`)
# ═══════════════════════════════════════════════════════════════
class TestLecturaCompleta:
    def test_lee_mas_de_1500_caracteres(self):
        ruta = PROJECT_ROOT / "src/core/dispatcher_process.py"
        contenido, truncado = rs.leer_fuente(ruta)
        assert truncado is False
        assert len(contenido) > 1500, "la RAC antigua cortaba el archivo a 1500 chars"
        ast.parse(contenido)  # el archivo completo debe parsear, no un fragmento

    def test_respeta_limite_de_seguridad(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rs, "LIMITE_LECTURA_COMPLETA", 10)
        f = tmp_path / "x.py"
        f.write_text("a = 1\n" * 50, encoding="utf-8")
        contenido, truncado = rs.leer_fuente(f)
        assert truncado is True
        assert len(contenido) == 10


# ═══════════════════════════════════════════════════════════════
# 2. VALIDACIÓN ESTÁTICA
# ═══════════════════════════════════════════════════════════════
class TestValidacionSintaxis:
    @pytest.mark.parametrize("rel", ARCHIVOS_GRANDES)
    def test_archivos_reales_sin_errores_de_sintaxis(self, rel):
        diag = rs.analizar_archivo(PROJECT_ROOT / rel)
        assert diag.sintaxis_ok is True, f"{rel}: {diag.error_sintaxis}"
        assert diag.analizable_por_llm is True
        assert "VERIFICADA" in diag.resumen()

    def test_detecta_error_real_con_linea(self):
        diag = rs.analizar_archivo("roto.py", source=ROTA)
        assert diag.sintaxis_ok is False
        assert diag.linea_error == 4
        assert "Error" in (diag.error_sintaxis or ""), diag.error_sintaxis
        assert diag.analizable_por_llm is False

    def test_json_invalido_se_detecta(self):
        diag = rs.analizar_archivo("x.json", source='{"a": 1,,}')
        assert diag.verificado is True and diag.sintaxis_ok is False
        assert diag.linea_error == 1

    def test_simbolos_extraidos(self):
        diag = rs.analizar_archivo("v.py", source=VALIDA)
        assert {"Memoria", "reserve", "release"} <= diag.simbolos


# ═══════════════════════════════════════════════════════════════
# 3. CHUNKING EN FRONTERAS AST
# ═══════════════════════════════════════════════════════════════
class TestChunking:
    @pytest.mark.parametrize("rel", ARCHIVOS_GRANDES)
    def test_cada_chunk_parsea_y_no_desborda(self, rel):
        diag = rs.analizar_archivo(PROJECT_ROOT / rel)
        assert diag.unidades, "todo archivo debe producir al menos un bloque"
        for unidad in diag.unidades:
            if unidad.modo in ("completo", "intacto", "mixto"):
                ast.parse(unidad.texto)  # SyntaxError ⇒ test fail
            assert len(unidad.texto) <= rs.MAX_CHUNK_CHARS * 1.6

    def test_archivo_pequeno_va_completo(self):
        diag = rs.analizar_archivo("v.py", source=VALIDA)
        assert len(diag.unidades) == 1
        assert diag.unidades[0].modo == "completo"
        assert diag.unidades[0].es_fragmento is False

    def test_cobertura_de_lineas_sin_huecos(self):
        diag = rs.analizar_archivo(PROJECT_ROOT / "src/core/task_queue.py")
        inicio_esperado = 1
        for unidad in sorted(diag.unidades, key=lambda u: u.linea_inicio):
            assert unidad.linea_inicio <= inicio_esperado + 1
            inicio_esperado = max(inicio_esperado, unidad.linea_fin)
        assert inicio_esperado >= diag.total_lineas - 2

    def test_clase_gigante_se_expande_y_marca_omisiones(self):
        diag = rs.analizar_archivo(PROJECT_ROOT / "src/core/dispatcher_process.py")
        assert any(u.modo in ("mixto", "esqueleto") for u in diag.unidades)
        for unidad in diag.unidades:
            if unidad.modo == "mixto":
                assert "[RAC] cuerpo omitido" in unidad.texto

    def test_texto_sin_parser_se_marca_fragmento(self):
        grande = "linea de texto\n" * 3000
        diag = rs.analizar_archivo("notas.md", source=grande)
        assert diag.unidades and all(u.modo == "fragmento" for u in diag.unidades)
        assert all(u.es_fragmento for u in diag.unidades)


# ═══════════════════════════════════════════════════════════════
# 4. FILTRO DE AFIRMACIONES REFUTADAS
# ═══════════════════════════════════════════════════════════════
class TestFiltroAlucinaciones:
    def test_descarta_falsos_positivos_de_sintaxis_y_ausencia(self):
        diag = rs.analizar_archivo("v.py", source=VALIDA)
        limpio, descartes = rs.filtrar_alucinaciones(RESPUESTA_LLM, diag)
        assert "no se cierra" not in limpio
        assert "truncado" not in limpio
        assert "`release()`" not in limpio
        assert "carrera en `reserve`" in limpio, "los hallazgos legítimos NO se filtran"
        assert len(descartes) == 3

    def test_no_filtra_cuando_la_sintaxis_esta_rota(self):
        diag = rs.analizar_archivo("roto.py", source=ROTA)
        limpio, descartes = rs.filtrar_alucinaciones(RESPUESTA_LLM, diag)
        assert "no se cierra" in limpio
        assert descartes == []

    def test_salida_vacia_reporta_sin_hallazgos(self):
        diag = rs.analizar_archivo("v.py", source=VALIDA)
        limpio, descartes = rs.filtrar_alucinaciones("- La clase `Memoria` no se cierra\n", diag)
        assert limpio == "SIN HALLAZGOS RELEVANTES"
        assert len(descartes) == 1


# ═══════════════════════════════════════════════════════════════
# 5. END-TO-END con LLM falso (sin Ollama)
# ═══════════════════════════════════════════════════════════════
class _Respuesta:
    def __init__(self, content: str):
        self.content = content


class _ClienteFalso:
    """Cliente Ollama simulado: registra prompts y devuelve una respuesta fija."""

    prompts: list = []
    respuesta: str = RESPUESTA_LLM

    def __init__(self, model=None, timeout=None, disable_fallback=False, scenario=None, **kw):
        self.model = model

    def preload_model(self, timeout=None):
        return True

    def generate(self, **kwargs):
        type(self).prompts.append(kwargs.get("prompt", ""))
        return _Respuesta(type(self).respuesta)


@pytest.fixture()
def proyecto_falso(tmp_path):
    core = tmp_path / "src" / "core"
    core.mkdir(parents=True)
    (core / "valida.py").write_text(VALIDA, encoding="utf-8")
    (core / "rota.py").write_text(ROTA, encoding="utf-8")
    return tmp_path


class TestRACEndToEnd:
    def _ejecutar(self, monkeypatch, proyecto):
        _ClienteFalso.prompts = []
        _ClienteFalso.respuesta = RESPUESTA_LLM
        monkeypatch.setattr(analyst_mod, "find_project_root", lambda *a, **k: proyecto)
        monkeypatch.setattr(analyst_mod, "_CLIENT_BASE_AVAILABLE", True)
        monkeypatch.setattr(analyst_mod, "OllamaClientBase", _ClienteFalso)
        # Sin sleeps reales (45s de pausa de lote harían el test eterno).
        monkeypatch.setattr(
            analyst_mod, "time", types.SimpleNamespace(sleep=lambda *_: None, time=time.time)
        )
        agente = analyst_mod.AnalystAgent.__new__(analyst_mod.AnalystAgent)
        return agente.review_system(scope="parcial")

    def test_reporte_limpio_y_bloque_estatico(self, monkeypatch, proyecto_falso):
        resultado = self._ejecutar(monkeypatch, proyecto_falso)
        assert resultado["success"] is True
        assert resultado["files_analyzed"] == 1, "solo el archivo con sintaxis válida va al LLM"

        reporte = Path(resultado["report_path"]).read_text(encoding="utf-8")
        assert "Verificación estática previa" in reporte
        assert "Módulos Python con `ast.parse` OK: **1**" in reporte
        assert "Errores de sintaxis REALES: **1**" in reporte
        # El archivo roto se reporta como error real y NO se envía al LLM.
        assert "Error de sintaxis REAL" in reporte
        assert "descartadas por evidencia estática: **3**" in reporte
        # Los falsos positivos del LLM no aparecen como hallazgos: solo pueden
        # figurar en la traza colapsable de afirmaciones descartadas.
        hallazgos = reporte.split("<details>")[0]
        assert "no se cierra" not in hallazgos
        assert "está truncado" not in hallazgos
        assert "carrera en `reserve`" in hallazgos, "los hallazgos legítimos se conservan"
        assert "falso positivo de sintaxis" in reporte

        assert len(_ClienteFalso.prompts) == 1
        prompt = _ClienteFalso.prompts[0]
        assert "REGLAS OBLIGATORIAS" in prompt
        assert "SINTAXIS VÁLIDA" in prompt
        assert "MODO DE ENVÍO" in prompt
        assert "release" in prompt  # el símbolo real se envía como evidencia

    def test_archivo_con_sintaxis_rota_no_consume_llm(self, monkeypatch, proyecto_falso):
        (proyecto_falso / "src" / "core" / "valida.py").write_text(ROTA, encoding="utf-8")
        resultado = self._ejecutar(monkeypatch, proyecto_falso)
        assert resultado["success"] is True
        assert resultado["files_analyzed"] == 0
        assert _ClienteFalso.prompts == []
        reporte = Path(resultado["report_path"]).read_text(encoding="utf-8")
        assert "Archivos con error de sintaxis (sin LLM):** 2" in reporte


class TestLinterDeNombresIndefinidos:
    """`ruff --select F821,F811,W605` como test (ISSUE-098 / C7 / ISSUE-109 / ISSUE-110).

    Por qué existe: el 2026-09-28 un `NameError` real (`ISSUE-096`) dejó al
    ValidatorAgent sin LLM en una corrida del usuario — una constante renombrada
    seguía viva en un aviso de `client_base.generate()`. **Ninguno de los 896
    tests lo vio** (ningún test ejecutaba ese camino); `ruff --select F821` lo caza
    en ~0,2 s. En ese momento había **16** nombres indefinidos preexistentes
    (2 reales: `rafael_daemon.py` sin `settings` y `create_agent()`); hoy: 0.

    ✅ **`W605`** (secuencias de escape inválidas, `ISSUE-109`): se sumó porque un
    `` \\` `` dentro de un docstring **no-raw** de `tools/quality_gate.py` (que
    escribí al arreglar `ISSUE-095`) emitía `SyntaxWarning` al compilar — y Python
    avisa que en el futuro eso es **`SyntaxError`**. Con `W605` seleccionado había
    **2 casos, ambos en esa misma línea**; hoy 0.

    ✅ **`F811`** (redefinición de nombre, `ISSUE-110`): se sumó tras arreglar
    `tools/actron/actron_scan.py`, donde el camino de fallback importaba
    `syn_scan_port` y **acto seguido** lo ponía en `None` (el modo SYN quedaba
    desactivado en silencio, y si el segundo import fallaba el `ImportError`
    salía sin capturar). Medición previa: **1 caso en todo el repo**; hoy 0.
    Ojo: `F811` caza el **síntoma** (nombre redefinido) — el bug real estaba en el
    `None`; por eso el fix se verificó por **comportamiento**, no solo con el linter.

    Se saltea si `ruff` no está instalado (entorno local sin dev-deps); en CI se
    instala con `requirements-ci.txt`. Para correrlo con un binario suelto:
    `RUFF_BIN=/ruta/ruff pytest tests/test_axioma_26_rac_estatico.py -k Linter`.
    """

    @staticmethod
    def _ruff_bin():
        """Binario de ruff: `RUFF_BIN` → PATH → **junto al intérprete** (venv).

        Lo tercero importa: con ruff instalado en el venv pero sin `venv/bin` en el
        PATH, `shutil.which()` no lo encuentra y el test se saltaba en silencio.
        """
        import os
        import shutil
        import sys
        from pathlib import Path
        candidatos = [os.environ.get("RUFF_BIN"), shutil.which("ruff"),
                      str(Path(sys.executable).parent / "ruff"),
                      str(Path(sys.executable).parent / "ruff.exe")]
        return next((c for c in candidatos if c and Path(c).exists()), None)

    def test_sin_nombres_indefinidos(self):
        import subprocess

        ruff = self._ruff_bin()
        if not ruff:
            pytest.skip("ruff no instalado (se instala en CI vía requirements-ci.txt)")

        raiz = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [ruff, "check", "src", "tools", "tests", "--select", "F821,F811,W605",
             "--no-cache", "--output-format", "concise"],
            cwd=str(raiz), capture_output=True, text=True, timeout=120,
        )
        salida = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, (
            "nombres indefinidos o redefinidos (rompen en runtime) o escapes inválidos "
            "(incluye tests/: un `NameError` en un test es un test que nunca corre) "
            "(SyntaxWarning hoy, SyntaxError mañana):\n"
            + "\n".join(l for l in salida.splitlines()
                        if any(r in l for r in ("F821", "F811", "W605")))[:2000]
        )

    def test_el_chequeo_es_rapido(self):
        """Si el chequeo se vuelve lento, deja de correr en cada tanda."""
        import subprocess
        import time as _time

        ruff = self._ruff_bin()
        if not ruff:
            pytest.skip("ruff no instalado")
        raiz = Path(__file__).resolve().parent.parent
        t0 = _time.perf_counter()
        subprocess.run([ruff, "check", "src", "tools", "tests", "--select", "F821,F811,W605",
                        "--no-cache", "--output-format", "concise"],
                       cwd=str(raiz), capture_output=True, text=True, timeout=120)
        assert _time.perf_counter() - t0 < 30.0


class TestRacResumible:
    """RAC v0.7.5: estado resumible + prioridad por riesgo + cobertura.

    Motivo MEDIDO: cada bloque cuesta 100-165 s ⇒ 45 min alcanzan para ~10 de 48
    archivos (`parcial`), y como no había estado cada corrida repetía el mismo
    prefijo.
    """

    def test_estado_ida_y_vuelta(self, tmp_path):
        from src.agents.analyst import _rac_cargar_estado, _rac_guardar_estado
        ruta = tmp_path / "rac_state.json"
        _rac_guardar_estado(ruta, {"parcial": {"completados": ["a.py"], "ciclo": 1}})
        estado = _rac_cargar_estado(ruta)
        assert estado["parcial"]["completados"] == ["a.py"], estado
        assert _rac_cargar_estado(tmp_path / "no-existe.json") == {}

    def test_estado_tolera_archivo_corrupto(self, tmp_path):
        from src.agents.analyst import _rac_cargar_estado
        ruta = tmp_path / "rac_state.json"
        ruta.write_text("{esto no es json", encoding="utf-8")
        assert _rac_cargar_estado(ruta) == {}

    def test_no_reinicia_a_medias(self):
        from src.agents.analyst import _rac_scope_estado
        estado = {}
        sc = _rac_scope_estado(estado, "parcial", ["a.py", "b.py", "c.py"])
        sc["completados"] = ["a.py"]
        sc = _rac_scope_estado(estado, "parcial", ["a.py", "b.py", "c.py"])
        assert sc["completados"] == ["a.py"], sc
        assert sc["ciclo"] == 1 and sc["ciclos_completos"] == 0, sc

    def test_abre_ciclo_nuevo_al_completar(self):
        from src.agents.analyst import _rac_scope_estado
        estado = {}
        sc = _rac_scope_estado(estado, "parcial", ["a.py", "b.py"])
        sc["completados"] = ["a.py", "b.py"]
        sc = _rac_scope_estado(estado, "parcial", ["a.py", "b.py"])
        assert sc["completados"] == [], sc
        assert sc["ciclo"] == 2 and sc["ciclos_completos"] == 1, sc

    def test_saltea_los_ya_analizados(self):
        from src.agents.analyst import _rac_pendientes
        candidatos = [(9.0, "grande.py"), (1.0, "chico.py"), (5.0, "medio.py")]
        assert [c[1] for c in _rac_pendientes(candidatos, ["grande.py"])] == \
            ["chico.py", "medio.py"]
        assert _rac_pendientes(candidatos, []) == candidatos

    def test_riesgo_prioriza_tamano_bloques_y_señales(self):
        import types
        from src.agents.analyst import _rac_riesgo
        def _diag(chars, n_bloques, texto):
            return types.SimpleNamespace(
                total_chars=chars,
                unidades=[types.SimpleNamespace(texto=texto) for _ in range(n_bloques)])
        grande = _rac_riesgo(_diag(60000, 8, "subprocess.run(...)\nthreading.Thread(...)"))
        chico = _rac_riesgo(_diag(800, 1, "x = 1"))
        assert grande > chico * 5, (grande, chico)

    def test_cobertura_legible(self):
        from src.agents.analyst import _rac_cobertura
        assert _rac_cobertura(48, 10) == "**Cobertura del ciclo: 10/48 archivos (21%)**"
        assert "100%" in _rac_cobertura(0, 0)

    def test_los_knobs_salen_de_settings_no_de_os_getenv(self, monkeypatch):
        """ISSUE-102/b: `.env` lo lee pydantic, no `os.getenv` (no hay load_dotenv).

        Los 4 knobs de RAC se leían con `os.getenv("AXIOMA_RAC_*")`, así que el valor
        del `.env` **se ignoraba** (y el informe aconsejaba ajustarlo). Ahora salen de
        `settings`, que sí lee el archivo.
        """
        from config.settings import settings
        from src.agents.analyst import _rac_config
        monkeypatch.setattr(settings, "rac_max_minutes", 300, raising=False)
        monkeypatch.setattr(settings, "rac_timeout", 600, raising=False)
        monkeypatch.setattr(settings, "rac_batch_pause", 45, raising=False)
        monkeypatch.setattr(settings, "rac_cooldown", 3, raising=False)
        cfg = _rac_config()
        assert cfg == {"max_minutes": 300, "timeout": 600,
                       "batch_pause": 45, "cooldown": 3}, cfg

    def test_tiempo_necesario_documentado(self):
        """El presupuesto por defecto debe ser coherente con el costo medido.

        Medido: 130 bloques en `parcial` ⇒ 113 min de prefill + generación; un ciclo
        completo en el peor caso son ~277 min. El default de settings (45) es sólo la
        plantilla: el valor real vive en `.env`.
        """
        from config.settings import settings
        from src.agents.analyst import _rac_config
        assert settings.rac_max_minutes >= 1
        assert _rac_config()["timeout"] >= 240, "600 s evita el recorte de profundidad"


# ─────────────────────────────────────────────────────────────────────────
# ✅ v8.2 (ISSUE-106): el detector de handlers silenciosos NO debe ser ciego.
# Antes: un `except (A, B):` se clasificaba como "tuple" y se descartaba (15
# handlers ciegos medidos en el repo real), `except ImportError:` con cuerpo
# silencioso no se contaba, y `log_degraded(...)` no se reconocía como log.
# ─────────────────────────────────────────────────────────────────────────


class TestSilenciososNoCiego:
    SILENCIOSO_TUPLE = '''
def f(x):
    try:
        return int(x)
    except (ValueError, TypeError):
        return None
'''
    LOG_DEGRADED = '''
def f():
    try:
        return algo()
    except Exception:
        log_degraded(logger, None, "modulo.f")
        return None
'''
    IMPORTERROR_SILENCIOSO = '''
try:
    import opcional
except ImportError:
    opcional = None
'''
    PERMISSION = '''
def escribir(p):
    try:
        return open(p, "w")
    except PermissionError:
        return None
'''

    def _detector(self, root):
        from tools.auditor.aud_core import CodeIndex
        from tools.auditor.aud_detectors_salud import SilentExceptionsDetector
        idx = CodeIndex(root); idx.build()
        return SilentExceptionsDetector(idx)

    @staticmethod
    def _mk(tmp_path):
        (tmp_path / "src" / "memory").mkdir(parents=True, exist_ok=True)
        return tmp_path

    def _correr(self, tmp_path, body, fname="m.py"):
        root = self._mk(tmp_path)
        (root / "src" / "memory" / fname).write_text(body, encoding="utf-8")
        return self._detector(root).run().findings

    def test_tuple_con_tipos_vigilados_ya_no_es_ciego(self, tmp_path):
        """Debe dar **LOW** (no basta con el INFO agregado de cobertura)."""
        f = self._correr(tmp_path, self.SILENCIOSO_TUPLE)
        low = [x for x in f if x.detector == "silent_exceptions"
               and x.severity.value == "LOW"]
        assert low, ("un `except (ValueError, TypeError): return None` debe dar LOW, "
                     f"no solo cobertura: {[x.title for x in f]}")

    def test_log_degraded_cuenta_como_registro(self, tmp_path):
        f = self._correr(tmp_path, self.LOG_DEGRADED, "d.py")
        sil = [x for x in f if x.detector == "silent_exceptions"]
        assert not sil, f"log_degraded(...) ya registra: {[x.title for x in sil]}"

    def test_permission_error_es_vigilado(self, tmp_path):
        """Un `PermissionError` tragado puede ser pérdida de datos ⇒ LOW."""
        f = self._correr(tmp_path, self.PERMISSION, "p.py")
        low = [x for x in f if x.detector == "silent_exceptions"
               and x.severity.value == "LOW"]
        assert low, f"PermissionError debe dar LOW: {[x.title for x in f]}"

    def test_importerror_silencioso_cuenta_en_la_cobertura(self, tmp_path):
        """No da LOW (feature detection), pero debe quedar CONTADO (INFO)."""
        f = self._correr(tmp_path, self.IMPORTERROR_SILENCIOSO, "i.py")
        sil = [x for x in f if x.detector == "silent_exceptions"]
        assert not [x for x in sil if x.severity.value == "LOW"], \
            f"ImportError silencioso no debe dar LOW: {[x.title for x in sil]}"
        assert any("NO vigilados" in x.title for x in sil), \
            f"debe contarse en la cobertura: {[x.title for x in sil]}"


class TestRacPromptSinValvulaDeEscape:
    """`ISSUE-113`: el prompt del RAC pedía la respuesta `SIN HALLAZGOS RELEVANTES`.

    Medido en la corrida REAL del usuario (2026-09-29): **79 de 79** llamadas
    devolvieron exactamente esos 12 tokens (53 s cada una) y **30 de 38** bloques del
    informe acumulado eran ese mismo texto ⇒ el RAC trabajó horas para no producir
    nada. A/B con el mismo bloque y modelo: prompt viejo 24 chars / 12 tok vs prompt
    nuevo 608 chars / 171 tok.
    """

    def test_el_prompt_no_tiene_la_valvula_de_escape(self):
        from src.agents.analyst import RAC_SYSTEM_PROMPT
        assert "SIN HALLAZGOS RELEVANTES" not in RAC_SYSTEM_PROMPT, \
            "volvió la válvula de escape que dejaba al RAC sin producir nada"

    def test_el_prompt_exige_estructura(self):
        from src.agents.analyst import RAC_SYSTEM_PROMPT
        assert "## Qué hace" in RAC_SYSTEM_PROMPT, RAC_SYSTEM_PROMPT[:200]
        assert "## Observaciones" in RAC_SYSTEM_PROMPT, RAC_SYSTEM_PROMPT[:200]

    def test_conserva_las_reglas_antialucinacion(self):
        """La válvula se fue; las reglas que evitan inventar, NO."""
        from src.agents.analyst import RAC_SYSTEM_PROMPT
        assert "NUNCA reportes errores de sintaxis" in RAC_SYSTEM_PROMPT
        assert "No inventes símbolos" in RAC_SYSTEM_PROMPT


class TestRacControlYCentinelas:
    """`ISSUE-113`: no se podía saber el estado del RAC ni pausarlo/detenerlo.

    La UI solo mostraba "Revisión Axioma Completa en progreso..." y matarlo perdía
    todo lo analizado, porque el informe se escribía recién al final.
    """

    def _rutas(self, tmp_path, monkeypatch):
        import src.agents.analyst as a
        monkeypatch.setattr(a, "RAC_STOP_PATH", tmp_path / "rac_stop")
        monkeypatch.setattr(a, "RAC_PAUSE_PATH", tmp_path / "rac_pause")
        monkeypatch.setattr(a, "RAC_STATE_PATH", str(tmp_path / "rac_state.json"))
        return a

    def test_stop_crea_y_resume_borra_el_centinela(self, tmp_path, monkeypatch):
        a = self._rutas(tmp_path, monkeypatch)
        msg = a.rac_control("stop")
        assert a.RAC_STOP_PATH.exists(), msg
        assert "informe" in msg.lower(), msg
        a.rac_control("resume")
        assert not a.RAC_STOP_PATH.exists(), "resume no quitó el centinela de parada"

    def test_pause_y_resume(self, tmp_path, monkeypatch):
        a = self._rutas(tmp_path, monkeypatch)
        a.rac_control("pause")
        assert a.RAC_PAUSE_PATH.exists()
        assert "resume" in a.rac_control("pause").lower()
        a.rac_control("resume")
        assert not a.RAC_PAUSE_PATH.exists()

    def test_status_informa_avance_y_ritmo(self, tmp_path, monkeypatch):
        import json
        a = self._rutas(tmp_path, monkeypatch)
        (tmp_path / "rac_state.json").write_text(json.dumps({
            "completa": {"iniciado": time.time() - 600, "ciclo": 1,
                         "pendientes_iniciales": 100,
                         "completados": [f"f{i}.py" for i in range(10)],
                         "ultima_corrida": time.time()},
        }), encoding="utf-8")
        salida = a.rac_control("status")
        assert "10/100" in salida, salida
        assert "f9.py" in salida, salida
        assert "restante" in salida, salida

    def test_status_marca_scope_inactivo_sin_eta_falso(self, tmp_path, monkeypatch):
        """Un scope parado horas daría un ETA absurdo (medido: 25 min/archivo)."""
        import json
        a = self._rutas(tmp_path, monkeypatch)
        (tmp_path / "rac_state.json").write_text(json.dumps({
            "parcial": {"iniciado": time.time() - 9000, "ciclo": 1,
                        "pendientes_iniciales": 48,
                        "completados": [f"g{i}.py" for i in range(11)],
                        "ultima_corrida": time.time() - 9000},
        }), encoding="utf-8")
        salida = a.rac_control("status")
        assert "sin actividad reciente" in salida, salida


class TestNumCtxFijoParaElRac:
    """`ISSUE-114`: el `num_ctx` del RAC bailaba entre 2048/4096/8192/16384.

    Medido en su corrida real (80 llamadas): 49 con 4096, 21 con 8192, 9 con 2048 y 1
    con 16384. `num_ctx` es parámetro de CARGA del modelo: cada valor distinto **descarta
    TODO el KV cacheado** (`ISSUE-086`, medido 830× más lento en frío) ⇒ se perdía la
    reutilización del system prompt común a las 577 llamadas y la RAM del KV se movía en
    cada llamada. Ahora el caller puede fijarlo (`num_ctx=`) y el RAC usa `RAC_NUM_CTX`.
    """

    def test_generate_respeta_num_ctx_explicito(self):
        import sys
        from pathlib import Path
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.llm.client import OllamaClient
        from src.agents.analyst import RAC_NUM_CTX

        capturado: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self): pass

            def json(self):
                return {"response": "ok", "model": "qwen2.5-coder:7b", "done": True,
                        "eval_count": 3, "prompt_eval_count": 2}

        # ✅ CI (2026-10-03): mismo motivo que en `test_axioma_04`: el paso de
        # `ModelSwap.ensure()` necesita Ollama vivo y hacía fallar este test en
        # GitHub Actions (el gate quedaba BLOQUEADO por este archivo). Acá se mide
        # el `num_ctx` del payload, no la gestión de VRAM.
        with patch("src.llm.client.httpx"), \
                patch("src.core.model_swap.ModelSwap.ensure", lambda self, model: None):
            cli = OllamaClient(model="qwen2.5-coder:7b", timeout=180,
                               base_url="http://127.0.0.1:11434")
            cli._get_client = lambda timeout=None: type("S", (), {
                "post": staticmethod(lambda url, json=None, timeout=None:
                                     (capturado.update(json), _Resp())[1])})()
            cli.generate(prompt="x" * 500, timeout=180, max_tokens=400,
                         num_ctx=RAC_NUM_CTX, task_type="RAC_REVIEW")
        assert capturado["options"]["num_ctx"] == RAC_NUM_CTX, capturado["options"]

    def test_los_topes_del_rac_son_sanos(self):
        from src.agents.analyst import RAC_NUM_CTX, RAC_NUM_PREDICT
        assert RAC_NUM_CTX & (RAC_NUM_CTX - 1) == 0, "num_ctx debe ser potencia de dos"
        assert RAC_NUM_CTX >= 8192, "debe cubrir el prompt más grande visto (6.694 tok)"
        # Medido: con 360 se truncó un bloque; con el prompt acotado a 3 puntos la
        # salida típica es ~180 tokens.
        assert 300 <= RAC_NUM_PREDICT <= 512, RAC_NUM_PREDICT


class TestRacIncremental:
    """`ISSUE-115`: RAC incremental — reutiliza el análisis si el archivo NO cambió.

    Medido: el scope `completa` son 213 archivos y **577 llamadas** al LLM por ciclo
    (≈6,8 h de prefill + generación) y un ciclo nuevo re-analizaba TODO aunque nada
    hubiera cambiado. Acá se verifica lo que importa: **la segunda corrida no hace ni
    una llamada** cuando el contenido es idéntico, y el informe **incluye** el análisis
    reutilizado (la calidad no baja: se reusa el análisis del mismo código, el mismo
    prompt y el mismo modelo).
    """

    def _ejecutar(self, monkeypatch, proyecto):
        _ClienteFalso.prompts = []
        _ClienteFalso.respuesta = RESPUESTA_LLM
        monkeypatch.setattr(analyst_mod, "find_project_root", lambda *a, **k: proyecto)
        monkeypatch.setattr(analyst_mod, "_CLIENT_BASE_AVAILABLE", True)
        monkeypatch.setattr(analyst_mod, "OllamaClientBase", _ClienteFalso)
        monkeypatch.setattr(
            analyst_mod, "time", types.SimpleNamespace(sleep=lambda *_: None, time=time.time)
        )
        agente = analyst_mod.AnalystAgent.__new__(analyst_mod.AnalystAgent)
        return agente.review_system(scope="parcial")

    @staticmethod
    def _cerrar_ciclo(proyecto):
        """Marca TODO el alcance como completado ⇒ la próxima corrida abre ciclo NUEVO.

        Es el caso real que importa: mientras el ciclo está abierto, un archivo ya
        analizado ni se toca (`_rac_pendientes` lo excluye); la reutilización se pone
        a prueba cuando el ciclo se cierra y RAC vuelve a recorrer todo el alcance.
        """
        import json as _json
        ruta = proyecto / "data" / "rac_state.json"
        estado = _json.loads(ruta.read_text(encoding="utf-8"))
        sc = estado["parcial"]
        candidatos = [str(p.relative_to(proyecto)) for p in (proyecto / "src").rglob("*.py")]
        sc["completados"] = sorted(candidatos)
        ruta.write_text(_json.dumps(estado), encoding="utf-8")

    def test_segunda_corrida_no_llama_al_llm(self, monkeypatch, proyecto_falso):
        primera = self._ejecutar(monkeypatch, proyecto_falso)
        assert primera["success"] is True
        llamadas_1 = len(_ClienteFalso.prompts)
        assert llamadas_1 > 0, "la primera corrida debe analizar de verdad"
        self._cerrar_ciclo(proyecto_falso)

        segunda = self._ejecutar(monkeypatch, proyecto_falso)
        assert segunda["success"] is True
        assert len(_ClienteFalso.prompts) == 0, (
            "la segunda corrida NO debe llamar al LLM si nada cambió "
            f"(hizo {len(_ClienteFalso.prompts)} llamadas)")
        reporte = Path(segunda["report_path"]).read_text(encoding="utf-8")
        assert "REUTILIZADO" in reporte, "el informe debe marcar el análisis reutilizado"
        # Lo que NO puede pasar: que el informe quede sin el análisis (calidad).
        assert RESPUESTA_LLM.splitlines()[0] in reporte, (
            "el informe debe CONTENER el análisis reutilizado, no solo la marca")
        assert "RAC incremental:** ACTIVO" in reporte

    def test_archivo_modificado_se_reanaliza(self, monkeypatch, proyecto_falso):
        """La calidad NO baja: si el archivo cambia, se vuelve a analizar."""
        self._ejecutar(monkeypatch, proyecto_falso)
        self._cerrar_ciclo(proyecto_falso)
        objetivo = proyecto_falso / "src" / "core" / "valida.py"
        original = objetivo.read_text(encoding="utf-8")
        objetivo.write_text(original + "\n# cambio real\n", encoding="utf-8")

        resultado = self._ejecutar(monkeypatch, proyecto_falso)
        assert resultado["success"] is True
        assert len(_ClienteFalso.prompts) > 0, (
            "un archivo modificado SIEMPRE se re-analiza (no se reutiliza nada)")

    def test_cambiar_el_prompt_invalida_la_cache(self, monkeypatch, proyecto_falso):
        """Si cambia el prompt del sistema, el análisis guardado queda obsoleto."""
        self._ejecutar(monkeypatch, proyecto_falso)
        self._cerrar_ciclo(proyecto_falso)
        monkeypatch.setattr(analyst_mod, "RAC_SYSTEM_PROMPT",
                            analyst_mod.RAC_SYSTEM_PROMPT + "\nNueva regla.")
        resultado = self._ejecutar(monkeypatch, proyecto_falso)
        assert resultado["success"] is True
        assert len(_ClienteFalso.prompts) > 0, (
            "con otro prompt hay que re-analizar: reutilizar sería mezclar criterios")


class TestVozSinBucleDeRepeticion:
    """`ISSUE-126`: el bucle de "¿Podés repetir? No entendí bien." que reportó el usuario.

    Medido en `logs/rafael.log` (corrida real): la wake word funciona **solo por energía**
    (`[WakeWord] Init — porcupine=False openwakeword=False`), así que cualquier ruido
    dispara el turno; el STT recibía audios de 0,5-1,3 s sin voz, `faster_whisper` los
    descartaba enteros y la transcripción salía vacía ⇒ `conf 0.00 < 0.5` ⇒ pedía repetir
    ⇒ volvía a escuchar ⇒ otro ruido ⇒ **bucle infinito**.
    """

    def test_turno_vacio_se_ignora_no_pide_repeticion(self):
        from src.multimodal.voice_pipeline import accion_turno_voz
        accion, vacios = accion_turno_voz("", 0.0, 0.5, 0)
        assert accion == "ignorar", "un turno SIN voz no debe pedir repetición"
        assert vacios == 1
        # y sigue ignorando: sólo tras 3 seguidos avisa una vez
        accion2, vacios2 = accion_turno_voz("   ", 0.0, 0.5, vacios)
        assert accion2 == "ignorar" and vacios2 == 2
        accion3, vacios3 = accion_turno_voz("", 0.0, 0.5, vacios2)
        assert accion3 == "avisar_silencio", "al tercero avisa UNA vez"
        assert vacios3 == 0, "y reinicia el contador (no vuelve a avisar en bucle)"

    def test_habla_mal_entendida_si_pide_repeticion(self):
        from src.multimodal.voice_pipeline import accion_turno_voz
        accion, _ = accion_turno_voz("buscá algo", 0.2, 0.5, 0)
        assert accion == "repetir", "si HAY texto con confianza baja, sí corresponde repetir"

    def test_turno_bueno_sigue(self):
        from src.multimodal.voice_pipeline import accion_turno_voz
        accion, vacios = accion_turno_voz("rafael buscá el archivo", 0.9, 0.5, 2)
        assert accion == "seguir" and vacios == 0, "un turno bueno reinicia los vacíos"


class TestWakeWordEnergiaSostenida:
    """`ISSUE-126`: la wake word por energía disparaba con UN solo frame de 30 ms."""

    def _detector(self):
        from src.multimodal.wake_word import WakeWordDetector
        d = WakeWordDetector.__new__(WakeWordDetector)     # sin abrir el micrófono
        d._noise_floor = 500.0
        d._sensitivity = 1.0
        d._frames_sostenidos = 5
        d._frames_sobre_umbral = 0
        return d

    def test_el_default_exige_energia_sostenida(self):
        """Si alguien vuelve a "un frame alcanza", este test lo caza."""
        from src.multimodal.wake_word import WakeWordDetector
        assert WakeWordDetector.FRAMES_SOSTENIDOS >= 3, (
            "un solo frame de 30 ms no puede declarar wake word (ISSUE-126)")

    def test_un_pico_aislado_no_dispara(self):
        d = self._detector()
        assert d._evaluar_frame(5000.0) is False, "un pico de 30 ms (clic/tos) no alcanza"

    def test_energia_sostenida_dispara(self):
        d = self._detector()
        assert [d._evaluar_frame(5000.0) for _ in range(4)] == [False] * 4
        assert d._evaluar_frame(5000.0) is True, "150 ms sostenidos sí son wake word"

    def test_silencio_reinicia_el_contador(self):
        d = self._detector()
        for _ in range(4):
            d._evaluar_frame(5000.0)
        d._evaluar_frame(10.0)          # silencio
        assert d._evaluar_frame(5000.0) is False, "el silencio debe reiniciar la cuenta"


class TestFraseDeInvocacion:
    """`ISSUE-127`: la frase de invocación ahora es "hey rafael" (configurable).

    Se verifica **por STT** (no hay modelo ML de wake word), así que el matching tiene que
    tolerar cómo transcribe Whisper: se acepta la frase configurada **y** el nombre solo.
    """

    def _pipeline(self, frase: str):
        from src.multimodal.voice_pipeline import VoicePipeline
        p = VoicePipeline.__new__(VoicePipeline)     # sin abrir audio
        p._wake_word = frase
        return p

    def test_frase_completa(self):
        p = self._pipeline("hey rafael")
        assert p._strip_wake_prefix("hey rafael buscá el clima") == "buscá el clima"

    def test_nombre_solo_tambien_vale(self):
        """Whisper puede transcribir "hey" como "ey"/"hei": el nombre es lo discriminante."""
        p = self._pipeline("hey rafael")
        assert p._strip_wake_prefix("rafael buscá el clima") == "buscá el clima"

    def test_verbo_de_llamada_se_descarta(self):
        p = self._pipeline("hey rafael")
        assert p._strip_wake_prefix("hey rafael, dime la hora") == "la hora"

    def test_frase_ajena_se_ignora(self):
        """Clave para el ruido: si no invoca a Rafael, NO se despacha nada."""
        p = self._pipeline("hey rafael")
        assert p._strip_wake_prefix("hola qué tal cómo va") is None
        assert p._strip_wake_prefix("") is None

    def test_frase_configurable(self):
        p = self._pipeline("che rafael")
        assert p._strip_wake_prefix("che rafael abrí el editor") == "abrí el editor"


class TestNormalizacionDeAudio:
    """`ISSUE-129`: la normalización de nivel rescata los DOS extremos medidos.

    Con el micro al 31 % el STT/VAD no oían la voz (turnos vacíos) y al 85 % el audio
    saturaba (RMS 0,73 · pico 2,04) ⇒ el VAD veía voz en el 100 % y el turno nunca
    cerraba. Medido sobre las grabaciones reales del usuario: atenuando a RMS 0,0061
    (como el 31 %) el texto transcrito es **idéntico** al de nivel sano; recortando a
    RMS 0,403 (como el 85 %) también, con confianza incluso mejor.
    """

    def _audio(self, rms_objetivo: float, n: int = 16000):
        import numpy as np
        rng = np.random.default_rng(0)
        a = rng.normal(0, 1, n).astype("float32")
        return (a / float(np.sqrt(np.mean(a ** 2))) * rms_objetivo).astype("float32")

    def test_senales_muy_bajas_se_normalizan(self):
        import numpy as np
        from src.multimodal.stt_engine import normalizar_audio
        a = self._audio(0.006)          # como el micrófono al 31 %
        n = normalizar_audio(a)
        assert float(np.sqrt(np.mean(n ** 2))) > 0.05, "una señal débil debe subir de nivel"

    def test_senales_saturadas_bajan_y_no_recortan(self):
        """Un audio recortado es casi una onda cuadrada (RMS ≈ pico): lo que importa es
        que el PICO quede dentro de rango (<=1.0) y por debajo del original recortado."""
        import numpy as np
        from src.multimodal.stt_engine import normalizar_audio
        a = np.clip(self._audio(0.4) * 5.0, -1.0, 1.0)   # como el 85 %
        n = normalizar_audio(a)
        assert float(np.max(np.abs(n))) <= 1.0
        assert float(np.max(np.abs(n))) < float(np.max(np.abs(a))) or float(np.max(np.abs(a))) < 1.0

    def test_silencio_no_se_amplifica(self):
        import numpy as np
        from src.multimodal.stt_engine import normalizar_audio
        silencio = np.full(8000, 0.0001, dtype="float32")
        assert np.allclose(normalizar_audio(silencio), silencio), (
            "amplificar el silencio convertiría el ruido de fondo en 'voz'")

    def test_pico_ya_en_objetivo_no_se_toca(self):
        """El contrato es por PICO (no por RMS): si el pico ya está cerca del objetivo
        (0.7), el audio se deja igual. Con ruido blanco RMS 0.09 el pico es ~0.35, así
        que SÍ se normaliza: normalizar voz por pico es lo correcto (el RMS de voz con
        pico 0.7 queda en 0.07-0.17, o sea en rango sano)."""
        import numpy as np
        from src.multimodal.stt_engine import normalizar_audio
        a = (self._audio(0.2) * (0.7 / np.max(np.abs(self._audio(0.2))))).astype("float32")
        assert 0.69 <= float(np.max(np.abs(a))) <= 0.71
        assert np.allclose(normalizar_audio(a), a), "pico ya en objetivo: no se toca"
