#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# test_axioma_18_auditor_regresiones.py
# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9v: verifica que los DETECTORES nuevos del auditor detecten de verdad
# las clases de bug halladas en la sesión (docs/ISSUES_HISTORY.md):
#   - ISSUE-018/019 → silent_contract_swallow
#   - ISSUE-020     → fragile_source_grep_tests
#   - ISSUE-022     → nondeterministic_hw_tests
# Tests con `assert` real: si un detector deja de detectar, esto falla.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

from pathlib import Path

from tools.auditor.aud_core import CodeIndex
from tools.auditor.aud_detectors_regresiones import (
    FragileSourceGrepTestDetector,
    NondeterministicHardwareTestDetector,
    SilentContractSwallowDetector,
)

import pytest

try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTION = "18. Auditor — regresiones de detectores y política R3"


def _mk_project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _index(root: Path) -> CodeIndex:
    idx = CodeIndex(root)
    idx.build()
    return idx


# ─────────────────────────────────────────────────────────────────────────
# ISSUE-019 — contrato tragado a DEBUG en ruta de alto valor
# ─────────────────────────────────────────────────────────────────────────


class TestSilentContractSwallow:
    BAD = '''
def guardar(x):
    try:
        return algo(x)
    except Exception as e:
        logger.debug(f"skipped: {e}")
    return None
'''
    GOOD_WARNING = '''
def guardar(x):
    try:
        return algo(x)
    except Exception as e:
        logger.warning(f"falló: {e}")
    return None
'''
    GOOD_DEGRADED = '''
def guardar(x):
    try:
        return algo(x)
    except Exception as e:
        log_degraded(logger, e, "Guardar.guardar")
    return None
'''
    GOOD_RERAISE = '''
def guardar(x):
    try:
        return algo(x)
    except Exception:
        raise
'''
    SPECIFIC = '''
def guardar(x):
    try:
        return algo(x)
    except TimeoutError as e:
        logger.debug(f"timeout: {e}")
'''

    def _findings(self, root, body, fname="guard.py"):
        p = root / "src" / "memory" / fname
        p.write_text(body, encoding="utf-8")
        return SilentContractSwallowDetector(_index(root)).run().findings

    def test_detecta_debug_en_ruta_de_alto_valor(self, tmp_path):
        root = _mk_project(tmp_path)
        f = self._findings(root, self.BAD)
        assert f, "el detector NO detectó el except→DEBUG en src/memory/"
        assert f[0].file == "src/memory/guard.py"
        assert f[0].severity.value == "MEDIUM"

    def test_no_detecta_si_loguea_warning(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._findings(root, self.GOOD_WARNING, "g2.py") == []

    def test_no_detecta_si_usa_log_degraded(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._findings(root, self.GOOD_DEGRADED, "g3.py") == []

    def test_no_detecta_si_relanza(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._findings(root, self.GOOD_RERAISE, "g4.py") == []

    def test_no_detecta_excepcion_especifica(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._findings(root, self.SPECIFIC, "g5.py") == []

    def test_no_detecta_fuera_de_ruta_de_alto_valor(self, tmp_path):
        """En rutas no críticas la degradación a DEBUG es diseño deliberado."""
        root = _mk_project(tmp_path)
        (root / "src" / "interfaces").mkdir(parents=True, exist_ok=True)
        (root / "src" / "interfaces" / "ui.py").write_text(self.BAD, encoding="utf-8")
        assert SilentContractSwallowDetector(_index(root)).run().findings == []


# ─────────────────────────────────────────────────────────────────────────
# ISSUE-020 — tests que grepean el fuente
# ─────────────────────────────────────────────────────────────────────────


class TestFragileSourceGrepTests:
    GREP = '''
PROJECT_ROOT = Path(__file__).parent.parent
def _src(rel):
    return (PROJECT_ROOT / rel).read_text()

def test_algo():
    src = _src("src/core/x.py")
    check("metodo presente", "def metodo(self" in src, "ok")
'''
    BEHAVIOR = '''
def test_algo():
    from src.core.x import X
    assert X().metodo() == 1
'''

    def _detect(self, root, body):
        (root / "tests" / "test_x.py").write_text(body, encoding="utf-8")
        return FragileSourceGrepTestDetector(_index(root)).run().findings

    def test_detecta_grep_sobre_fuente(self, tmp_path):
        root = _mk_project(tmp_path)
        f = self._detect(root, self.GREP)
        assert f, "el detector NO detectó el test que grepea el fuente"
        assert f[0].file == "tests/test_x.py"
        assert f[0].line_start > 0

    def test_no_detecta_test_de_comportamiento(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._detect(root, self.BEHAVIOR) == []


# ─────────────────────────────────────────────────────────────────────────
# ISSUE-022 — tests que inyectan clases dependientes del entorno
# ─────────────────────────────────────────────────────────────────────────


class TestNondeterministicHardwareTests:
    REAL_GUARD = '''
def test_coord():
    coord = AgentCoordinator(swap=FakeSwap(), guard=MemoryGuard(), board=None)
    assert coord is not None
'''
    FAKE_GUARD = '''
def test_coord():
    coord = AgentCoordinator(swap=FakeSwap(), guard=FakeGuard(), board=None)
    assert coord is not None
'''
    POLICY_TEST = '''
def test_politica_ram():
    g = MemoryGuard(warn_ratio=0.85, hard_ratio=0.95)
    ok, reason, _ = g.can_load("qwen3:8b")
    assert reason
'''

    def _detect(self, root, body):
        (root / "tests" / "test_y.py").write_text(body, encoding="utf-8")
        return NondeterministicHardwareTestDetector(_index(root)).run().findings

    def test_detecta_guard_real_inyectado(self, tmp_path):
        root = _mk_project(tmp_path)
        f = self._detect(root, self.REAL_GUARD)
        assert f, "el detector NO detectó `guard=MemoryGuard()` inyectado"
        assert f[0].severity.value == "LOW"

    def test_no_detecta_guard_fake(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._detect(root, self.FAKE_GUARD) == []

    def test_no_detecta_test_de_politica_de_ram(self, tmp_path):
        """El test de política usa el guard real a propósito (no se inyecta)."""
        root = _mk_project(tmp_path)
        assert self._detect(root, self.POLICY_TEST) == []


# ─────────────────────────────────────────────────────────────────────────
# Integración: los 3 detectores están registrados en el auditor
# ─────────────────────────────────────────────────────────────────────────


class TestDetectoresRegistrados:
    def test_registrados_en_el_auditor(self):
        from tools.auditor.aud_main import DETECTORS
        for name in ("silent_contract_swallow",
                     "fragile_source_grep_tests",
                     "nondeterministic_hw_tests"):
            assert name in DETECTORS, f"{name} no está registrado en DETECTORS"


# ─────────────────────────────────────────────────────────────────────────
# ✅ v0.6.9v: los detectores corregidos NO deben quedar ciegos.
# Se arreglaron 2 FALSOS POSITIVOS:
#   • AUD-PV2-001 (HIGH) — el refactor 50/50 movió `_screen_input` al mixin
#     guard y el check lo buscaba solo en dispatcher_process.py.
#   • AUD-SP-001 (MEDIUM) — DistributedQueue es un wrapper que DELEGA en
#     TaskQueue, que sí tiene aging (_pop_aged_task).
# Estos tests verifican que el arreglo no volvió ciegos a los detectores.
# ─────────────────────────────────────────────────────────────────────────


class TestStaticPriorityNoCiego:
    REAL_SIN_AGING = '''
import asyncio
class MiQueue:
    """Cola con priority propia y SIN aging."""
    def __init__(self):
        self._q = asyncio.PriorityQueue()
    async def dequeue(self):
        prio, task = await self._q.get()
        return task
'''
    DELEGADOR = '''
class WrapperQueue:
    """Wrapper que delega: no es dueño de la lógica de prioridad."""
    def __init__(self, queue):
        self._queue = queue
    async def dequeue(self, timeout=None):
        return await self._queue.dequeue(timeout=timeout)
'''
    CON_AGING = '''
class OtraQueue:
    def __init__(self):
        self._q = asyncio.PriorityQueue()
    async def dequeue(self):
        return self._pop_aged_task()
'''

    def _detect(self, root, body, fname="q.py"):
        (root / "src" / "core").mkdir(parents=True, exist_ok=True)
        (root / "src" / "core" / fname).write_text(body, encoding="utf-8")
        from tools.auditor.aud_detectors_infra import StaticPriorityDetector
        return StaticPriorityDetector(_index(root)).run().findings

    def test_sigue_detectando_cola_real_sin_aging(self, tmp_path):
        root = _mk_project(tmp_path)
        f = self._detect(root, self.REAL_SIN_AGING)
        assert f, "el detector quedó CIEGO: no detecta una cola real sin aging"
        assert "priority" in f[0].explanation.lower() or "aging" in f[0].title.lower()

    def test_no_marca_wrapper_que_delega(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._detect(root, self.DELEGADOR, "w.py") == []

    def test_no_marca_cola_con_aging(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._detect(root, self.CON_AGING, "aged.py") == []

    def test_helper_is_pure_delegator(self):
        from tools.auditor.aud_detectors_infra import StaticPriorityDetector as D
        assert D._is_pure_delegator("return await self._queue.dequeue(timeout=t)", "dequeue") is True
        assert D._is_pure_delegator("prio, task = await self._q.get()", "dequeue") is False
        # Con estructura propia NO es delegador puro aunque mencione .pop(
        assert D._is_pure_delegator(
            "heappush(h, x); return h.pop()", "pop") is False


class TestPlanV2ScreenInputFamilia:
    def test_repo_real_sin_falso_positivo(self):
        """El repo real NO debe reportar AUD-PV2-001 (la protección existe)."""
        from tools.auditor.aud_core import CodeIndex
        from tools.auditor.aud_detectors_arquitectura import PlanV2ProtectionsDetector
        idx = CodeIndex(PROJECT_ROOT)
        idx.build()
        findings = PlanV2ProtectionsDetector(idx).run().findings
        pv2 = [f for f in findings if f.finding_id == "AUD-PV2-001"]
        assert not pv2, f"AUD-PV2-001 reapareció: {pv2[0].code_snippet if pv2 else ''}"

    def test_check_de_screen_input_es_family_aware(self):
        """El check debe buscar en la familia de módulos (no en un solo archivo)."""
        from tools.auditor.aud_detectors_arquitectura import _PLANV2_CHECKS
        entry = [e for e in _PLANV2_CHECKS if e[1] == "def _screen_input"]
        assert entry, "desapareció el check de _screen_input"
        files = entry[0][0]
        assert isinstance(files, tuple), "el check volvió a fijar UN solo archivo"
        assert "src/core/dispatcher_process.py" in files
        assert "src/core/dispatcher_process_guard.py" in files

    def test_check_de_cableado_existe(self):
        """Debe existir un check de que _screen_input sigue LLAMADO desde process()."""
        from tools.auditor.aud_detectors_arquitectura import _PLANV2_CHECKS
        assert any("self._screen_input(message.content)" in e[1] for e in _PLANV2_CHECKS), \
            "no hay check de cableado de _screen_input desde process()"


# ─────────────────────────────────────────────────────────────────────────
# ✅ v0.6.9v (ISSUE-026) — WIRING de los detectores nuevos al SCORE AXIOMA.
# Se penaliza por CATEGORÍA y SUBLINEALMENTE (con tope), igual que
# `isolated`/`residual`, para no colapsar la métrica por volumen.
# ─────────────────────────────────────────────────────────────────────────


class TestScoreWiring:
    def _auditor(self, findings):
        """Auditor mínimo con hallazgos inyectados (sin correr detectores)."""
        from pathlib import Path
        from tools.auditor.aud_core import AudFinding, AudSeverity, AudVerdict
        from tools.auditor.aud_main import AxiomaAuditor
        a = AxiomaAuditor(Path("."))
        a.findings = list(findings)
        return a

    def _finding(self, detector, severity=None, tags=None):
        from tools.auditor.aud_core import AudFinding, AudSeverity, AudVerdict
        return AudFinding(
            finding_id="X", title="t", detector=detector,
            severity=severity or AudSeverity.MEDIUM,
            verdict=AudVerdict.NEW, file="f.py",
            tags=set(tags or ()),
        )

    def test_score_baja_con_hallazgos_de_contrato(self):
        a = self._auditor([self._finding("silent_contract_swallow")] * 5)
        assert a.compute_score() == 99, a.compute_score()  # 5//5 = 1

    def test_score_baja_con_tests_fragiles(self):
        a = self._auditor([self._finding("fragile_source_grep_tests",
                                        severity=None)] * 4)
        assert a.compute_score() == 99, a.compute_score()  # 4//4 = 1

    def test_tope_evita_colapso(self):
        """Muchísimos hallazgos no deben llevar el SCORE a 0 (tope por regla)."""
        a = self._auditor([self._finding("silent_contract_swallow")] * 5000)
        # tope 6 → 100 - 6 = 94
        assert a.compute_score() == 94, a.compute_score()

    def test_sin_hallazgos_score_100(self):
        assert self._auditor([]).compute_score() == 100

    def test_sin_doble_conteo_con_other_high(self):
        """Los detectores nuevos emiten MEDIUM/LOW → no entran en other_high."""
        a = self._auditor([self._finding("silent_contract_swallow")] * 10)
        rows = {label: ded for label, _, ded in a.score_breakdown()}
        assert rows.get("otros CRITICAL/HIGH ×10", 0) == 0
        assert rows.get("silent_contract_swallow //5 (tope 6)") == 2  # 10//5

    def test_breakdown_es_fuente_unica(self):
        """El total del desglose debe explicar exactamente el SCORE."""
        a = self._auditor([
            *[self._finding("silent_contract_swallow")] * 13,
            *[self._finding("fragile_source_grep_tests")] * 9,
            *[self._finding("nondeterministic_hw_tests")] * 6,
        ])
        total = sum(d for _, _, d in a.score_breakdown())
        assert 100 - total == a.compute_score()
        # 13//5=2, 9//4=2, 6//3=2 → 6
        assert total == 6, total

    def test_wiring_en_repo_real(self):
        """En el repo real los 3 detectores deben estar en el desglose."""
        from pathlib import Path
        from tools.auditor.aud_core import CodeIndex
        from tools.auditor.aud_main import AxiomaAuditor, DETECTORS
        a = AxiomaAuditor(PROJECT_ROOT)
        a.build_index()
        a.run_detectors([n for n in DETECTORS if n in (
            "silent_contract_swallow", "fragile_source_grep_tests",
            "nondeterministic_hw_tests")])
        labels = [l for l, _, _ in a.score_breakdown()]
        for frag in ("silent_contract_swallow", "fragile_source_grep_tests",
                     "nondeterministic_hw_tests"):
            assert any(frag in l for l in labels), f"{frag} no está en el SCORE"
        assert 0 <= a.compute_score() <= 100


# ─────────────────────────────────────────────────────────────────────────
# ✅ v0.6.9v: PRECISIÓN del detector de contrato tragado. Al migrar 94 bloques
# aparecieron 2 imprecisiones más (ambas corregidas + cubiertas acá):
#   1. atribuía al handler externo el log de handlers ANIDADOS;
#   2. contaba mensajes de debug INFORMATIVOS (éxito del fallback) como si
#      fueran un error tragado.
# ─────────────────────────────────────────────────────────────────────────


class TestPrecisionContratoTragado:
    NESTED_ONLY = '''
import logging
def f(x):
    try:
        return algo(x)
    except Exception:
        try:
            return otra(x)
        except Exception as inner:
            logging.getLogger(__name__).debug(f"interno falló: {inner}")
'''
    INFO_DEBUG = '''
import logging
def f(x):
    try:
        return algo(x)
    except Exception:
        # La excepción es flujo esperado (no existe → crear) y el debug es de ÉXITO
        tabla = crear(x)
        logging.getLogger(__name__).debug(f"tabla creada: {tabla}")
'''
    REAL_ERROR = '''
import logging
def f(x):
    try:
        return algo(x)
    except Exception as exc:
        logging.getLogger(__name__).debug(f"[X] falló el contexto: {exc}")
'''

    def _findings(self, root, body, fname="h.py"):
        p = root / "src" / "memory" / fname
        p.write_text(body, encoding="utf-8")
        return SilentContractSwallowDetector(_index(root)).run().findings

    def test_no_atribuye_log_de_handler_anidado(self, tmp_path):
        """El handler EXTERNO no debe heredar el debug del handler ANIDADO.

        El interno SÍ es un caso real (traga y reporta por debug) y debe
        marcarse; lo que no debe ocurrir es que el externo —cuyo cuerpo no
        loguea nada— se cuente como si él mismo tragara el error.
        """
        root = _mk_project(tmp_path)
        f = self._findings(root, self.NESTED_ONLY, "n.py")
        assert len(f) == 1, f"esperaba 1 hallazgo (el interno), hubo {len(f)}"
        assert f[0].line_start == 9, f"se marcó el handler equivocado: línea {f[0].line_start}"

    def test_no_marca_debug_informativo_de_fallback(self, tmp_path):
        root = _mk_project(tmp_path)
        assert self._findings(root, self.INFO_DEBUG, "i.py") == [], \
            "marcó un debug INFORMATIVO (fallback OK) como error tragado"

    def test_si_marca_debug_que_reporta_error(self, tmp_path):
        root = _mk_project(tmp_path)
        f = self._findings(root, self.REAL_ERROR, "r.py")
        assert f, "no detectó un handler que REPORTA un error por DEBUG"

    def test_no_migrado_reaparece(self):
        """Regresión: ningún bloque de alto valor debe quedar a DEBUG."""
        from tools.auditor.aud_core import CodeIndex
        idx = CodeIndex(PROJECT_ROOT)
        idx.build()
        findings = SilentContractSwallowDetector(idx).run().findings
        assert not findings, (
            "reaparecieron bloques 'except → DEBUG' en rutas de alto valor: "
            + ", ".join(f"{f.file}:{f.line_start}" for f in findings)
        )

    def test_log_degraded_clasifica_por_contrato(self):
        """El helper que reemplazó los bloques sube a WARNING solo contratos."""
        import logging
        from src.utils.degradation import log_degraded
        log = logging.getLogger("axioma.test.migrado")
        assert log_degraded(log, TypeError("mal args"), "ctx") is True
        assert log_degraded(log, TimeoutError("red"), "ctx") is False


# ─────────────────────────────────────────────────────────────────────────
# ✅ v0.6.9v (ISSUE-028) — REGRESIÓN: el YAML de modelos debe APLICARSE.
# El loader leía `primary`/`fallbacks` pero el YAML escribe
# `preferred_model`/`fallback_model` → TODOS los mapeos caían al CODE_MODEL
# (`complex_reasoning` corría en el modelo de código).
# ─────────────────────────────────────────────────────────────────────────


class TestConfigModelMapping:
    def test_from_dict_acepta_ambas_convenciones(self):
        from src.llm.config import TaskModelMapping
        m1 = TaskModelMapping.from_dict("t", {"primary": "m1", "fallbacks": ["m2"]})
        assert m1.primary_model == "m1" and m1.fallback_models == ["m2"]
        # convención del YAML
        m2 = TaskModelMapping.from_dict("t", {"preferred_model": "mx",
                                             "fallback_model": "my"})
        assert m2.primary_model == "mx", "no leyó preferred_model"
        assert m2.fallback_models == ["my"], "no leyó fallback_model"

    def test_fallback_vacio_no_genera_lista_con_string_vacio(self):
        from src.llm.config import TaskModelMapping
        m = TaskModelMapping.from_dict("t", {"preferred_model": "m",
                                            "fallback_model": ""})
        assert m.fallback_models == [], m.fallback_models

    def test_mapping_real_se_aplica(self):
        """El modelo por tarea debe ser el que declara models.yaml."""
        from src.llm.config import LLMConfig
        c = LLMConfig()
        esperado = {
            "code_generation": "qwen2.5-coder:7b",
            "code_review": "qwen2.5-coder:7b",
            "code_correction": "qwen2.5-coder:7b",
            "code_explanation": "qwen2.5-coder:7b",
            "general_chat": "qwen3:8b",
            "complex_reasoning": "qwen3:8b",     # ← antes devolvía el coder
        }
        for task, modelo in esperado.items():
            got = c.get_model_for_task(task).name
            assert got == modelo, f"{task}: esperaba {modelo}, obtuvo {got}"

    def test_vision_no_rutea_por_task_mapping(self):
        """La visión usa settings.llm_vision_model (no task_mappings).

        Por eso se quitó la entrada `vision_analysis` de models.yaml: era
        inalcanzable (ISSUE-032).
        """
        import yaml
        from config.settings import settings
        assert settings.llm_vision_model, "no hay modelo de visión configurado"
        data = yaml.safe_load(
            (PROJECT_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
        assert "vision_analysis" not in data.get("task_mappings", {}), \
            "reapareció la entrada inalcanzable vision_analysis"

    def test_complex_reasoning_no_usa_modelo_de_codigo(self):
        from src.llm.config import LLMConfig
        from config.settings import settings
        c = LLMConfig()
        got = c.get_model_for_task("complex_reasoning").name
        assert got == settings.llm_default_model, (
            f"razonamiento complejo debería usar el modelo general "
            f"({settings.llm_default_model}), usa {got}")

    def test_claves_de_task_mappings_son_alcanzables(self):
        """Toda clave de task_mappings debe ser canónica o etiqueta usada en src/."""
        import yaml
        from src.domain.types import TaskType
        canonicos = {t.value for t in TaskType}
        data = yaml.safe_load(
            (PROJECT_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
        src_txt = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in (PROJECT_ROOT / "src").rglob("*.py") if "__pycache__" not in str(p))
        for task in data.get("task_mappings", {}):
            assert task in canonicos or f'"{task}"' in src_txt, (
                f"task_mappings.{task} no es canónico ni se usa en src/ "
                f"(entrada muerta — ver ISSUE-028)")

    def test_detector_config_task_keys_registrado(self):
        from tools.auditor.aud_main import DETECTORS
        assert "config_task_keys" in DETECTORS

    def test_detector_detecta_clave_inerte(self, tmp_path):
        """El detector debe marcar una clave que el loader NO lee."""
        from tools.auditor.aud_detectors_regresiones import ConfigTaskKeysDetector
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "llm").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "llm" / "config.py").write_text(
            'def from_dict(cls, t, data):\n'
            '    return data.get("primary"), data.get("temperature")\n',
            encoding="utf-8")
        (tmp_path / "config" / "models.yaml").write_text(
            "task_mappings:\n"
            "  general_chat:\n"
            "    primary: m\n"
            "    clave_inventada: 1\n",
            encoding="utf-8")
        findings = ConfigTaskKeysDetector(_index(tmp_path)).run().findings
        assert any("clave_inventada" in f.title for f in findings), \
            [f.title for f in findings]


# ─────────────────────────────────────────────────────────────────────────
# ✅ v0.6.9v (ISSUE-036) — GUARDA: `parallel_executor` está RESERVADO PARA
# USO FUTURO (modelos en paralelo cuando haya GPU). Decisión explícita del
# usuario: NO eliminar. Estos tests fallan si alguien lo borra o le rompe la API.
# ─────────────────────────────────────────────────────────────────────────


class TestParallelExecutorReservado:
    """El ejecutor paralelo queda listo para el día que haya VRAM para varios modelos."""

    RESERVADOS = (
        "src/agents/parallel_executor.py",
        "src/agents/parallel_executor_models.py",
    )

    def test_archivos_reservados_existen(self):
        for rel in self.RESERVADOS:
            assert (PROJECT_ROOT / rel).is_file(), (
                f"{rel} fue ELIMINADO — está reservado para uso futuro "
                f"(modelos en paralelo con GPU, ISSUE-036). Restaurarlo.")

    def test_api_paralela_intacta(self):
        """La API debe seguir importable y usable para cuando se cablee."""
        import asyncio
        from src.agents.parallel_executor import (
            ParallelExecutor, RetryConfig, ExecutionResult,
        )

        rc = RetryConfig(base=1.0, multiplier=2.0, jitter=False)
        assert rc.get_delay(2) == 4.0, "RetryConfig.get_delay cambió de contrato"
        res = ExecutionResult.ok("coder", "t1", "out")
        assert res.success is True and res.task_id == "t1"

        async def go():
            ex = ParallelExecutor(max_concurrent=2)
            n = ex.active_count
            await ex.shutdown(wait_for_pending=False)
            return n

        assert asyncio.run(go()) == 0, "ParallelExecutor no arranca/cierra limpio"

    def test_no_marcado_como_aislado_en_el_auditor(self):
        """Está excluido del check de 'módulo sin referencias' por diseño."""
        from tools.auditor.aud_detectors_arquitectura import _ISOLATED_EXCLUSIONS
        for rel in self.RESERVADOS:
            assert any(rel.startswith(exc) for exc in _ISOLATED_EXCLUSIONS), (
                f"{rel} volvería a aparecer como 'aislado' en el auditor")




# ═══════════════════════════════════════════════════════════════
# R3 — `except … : pass` PROHIBIDO (ISSUE-077)
# ═══════════════════════════════════════════════════════════════
# Un handler con cuerpo exactamente `pass` no deja rastro: si el error era de
# CONTRATO (TypeError/AttributeError/NameError), la feature muere en silencio
# (es lo que el detector AUD-SIL del auditor marca como MEDIUM). R3 obliga a
# pasar por `log_degraded`, que sube esos errores a WARNING y los cuenta en
# `degradation_stats()`. Excepciones sancionadas:
#   · `except ImportError: pass` → detección de feature opcional (INFO).
#   · `src/utils/degradation.py` → guard interno: `log_degraded` no puede
#     loguear su propio fallo sin recursión.
_SANCIONADOS = {"ImportError"}
_PERMITIDOS = {"src/utils/degradation.py"}
# Archivos con un `except: pass` migrado a `log_degraded` en ISSUE-077.
_CORREGIDOS = (
    "src/agents/analyst.py",
    "src/core/dispatcher_handlers_extra.py",
    "src/core/dispatcher_operations.py",
    "src/core/dispatcher_plan.py",
    "src/core/dispatcher_process_guard.py",
    "src/core/feedback_loop_detectors_handlers.py",
    "src/core/interpreter_loop.py",
    "src/interfaces/cli/command_parser.py",
    "src/interfaces/components/chat.py",
    "src/interfaces/web/request_stats.py",
    "src/interfaces/web/vision_service.py",
    "src/memory/gateway.py",
    "src/multimodal/tts_engine.py",
    "src/prompts/base_prompts.py",
    "src/router/cache.py",
    "src/router/classifier_embedding.py",
    "src/utils/ambiguity_detector.py",
    "src/utils/confidence.py",
)


def _pass_silencioso(path: Path):
    """[(línea, tipo)] de los `except` cuyo cuerpo es solo `pass`."""
    import ast
    out = []
    for h in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(h, ast.ExceptHandler) and len(h.body) == 1
                and isinstance(h.body[0], ast.Pass)):
            out.append((h.lineno, ast.unparse(h.type) if h.type else "bare"))
    return out


def _sin_sancionar(path: Path):
    return [o for o in _pass_silencioso(path) if o[1] not in _SANCIONADOS]


class TestPoliticaR3ExceptPass:
    def test_ningun_archivo_de_src_tiene_except_pass_silencioso(self):
        """Guardia global: el invariante no puede reaparecer en ningún archivo."""
        ofensas = {}
        for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if rel in _PERMITIDOS:
                continue
            malos = _sin_sancionar(path)
            if malos:
                ofensas[rel] = malos
        check("ningún `except: pass` silencioso en src/", not ofensas,
              str(ofensas) if ofensas else "", SECTION)
        assert not ofensas, (
            f"handlers `pass` silenciosos: {ofensas}. Usar "
            "`log_degraded(logger, exc, \"Clase.metodo (qué se pierde)\")` — R3.")

    @pytest.mark.parametrize("rel", _CORREGIDOS)
    def test_archivo_corregido_usa_log_degraded(self, rel):
        """Un caso por archivo corregido: degrada vía `log_degraded` (R3)."""
        path = PROJECT_ROOT / rel
        malos = _sin_sancionar(path)
        check(f"{rel}: sin `pass` silencioso", not malos, f"{rel}:{malos}", SECTION)
        assert not malos, f"{rel}: handlers `pass` silenciosos en {malos}"
        assert "log_degraded" in path.read_text(encoding="utf-8"), (
            f"{rel} ya no usa log_degraded: la degradación dejaría de ser visible.")

    def test_short_term_sin_try_except_muerto(self):
        """`__del__` no tenía nada que limpiar: se eliminó el `try/except`."""
        path = PROJECT_ROOT / "src/memory/short_term.py"
        fuente = path.read_text(encoding="utf-8")
        check("short_term.py sin `except` alguno", "except " not in fuente.split("def __del__")[1],
              "", SECTION)
        assert "except " not in fuente.split("def __del__")[1]


class TestR3DegradacionVisible:
    """Comportamiento: los sitios corregidos loguean y NO propagan el error."""

    @staticmethod
    def _espia(monkeypatch, modulo):
        vistos = []

        def falso(logger, exc, context, **kw):
            vistos.append((type(exc).__name__, context))
            return False

        monkeypatch.setattr(modulo, "log_degraded", falso)
        return vistos

    def test_callback_de_progreso_roto_no_rompe_el_dispatcher(self, monkeypatch):
        import types
        from src.core import dispatcher_handlers_extra as m
        vistos = self._espia(monkeypatch, m)
        msg = types.SimpleNamespace(metadata={"_on_progress": lambda p: 1 / 0})
        m._notify_progress(msg, "fase")          # no debe propagar
        check("callback roto degradado", len(vistos) == 1 and "notify_progress" in vistos[0][1],
              str(vistos), SECTION)
        assert len(vistos) == 1 and vistos[0][0] == "ZeroDivisionError"

    def test_etiqueta_de_confianza_rota_no_rompe_la_calibracion(self, monkeypatch):
        import types
        from src.core import dispatcher_process_guard as pg
        from src.utils import confidence as cf
        vistos = self._espia(monkeypatch, pg)

        def boom(_v):
            raise TypeError("label roto")

        monkeypatch.setattr(cf, "label_for", boom)
        resp = types.SimpleNamespace(confidence=0.99, confidence_label="Alto",
                                     sources=[], metadata={})
        after = pg.calibrate_response_confidence(resp, task_type="general_chat",
                                                 expects_grounding=True)
        check("el tope por falta de fuentes se aplica igual",
              after == pg.NO_SOURCES_FACTUAL_CAP and resp.confidence == after, str(after), SECTION)
        check("etiqueta rota degradada", len(vistos) == 1, str(vistos), SECTION)
        assert after == pg.NO_SOURCES_FACTUAL_CAP and len(vistos) == 1

    def test_telemetria_de_request_nunca_rompe_el_request(self, monkeypatch):
        from src.interfaces.web import request_stats as rs
        vistos = self._espia(monkeypatch, rs)
        rs.record_request("/x", 200, object())   # `object() > 0` → TypeError de contrato
        check("request con duración inválida degradado", len(vistos) == 1, str(vistos), SECTION)
        assert len(vistos) == 1 and vistos[0][0] == "TypeError"

    def test_etiqueta_opcional_no_invalida_la_confianza_calibrada(self, monkeypatch):
        import types
        from src.utils import confidence as cf
        vistos = self._espia(monkeypatch, cf)

        def boom(_v):
            raise TypeError("label roto")

        monkeypatch.setattr(cf, "label_for", boom)
        resp = types.SimpleNamespace(confidence=0.9, sources=["A - u"], metadata={})
        valor = cf.apply_calibration(resp)
        check("la confianza calibrada sobrevive", resp.confidence == valor and valor is not None,
              str(valor), SECTION)
        check("etiqueta degradada", len(vistos) == 1, str(vistos), SECTION)
        assert resp.confidence == valor and len(vistos) == 1

    def test_version_ilegible_no_rompe_el_prompt(self, monkeypatch):
        from src.prompts import base_prompts as bp
        vistos = self._espia(monkeypatch, bp)

        def boom(*_a, **_k):
            raise OSError("sin __init__.py")

        monkeypatch.setattr(Path, "read_text", boom)
        check("versión de reserva", bp._version_del_proyecto() == "desconocida", "", SECTION)
        check("lectura fallida degradada", len(vistos) == 1, str(vistos), SECTION)
        assert bp._version_del_proyecto() == "desconocida" and len(vistos) == 2

    def test_yaml_corrupto_usa_defaults_y_avisa(self, monkeypatch, tmp_path):
        from src.utils import ambiguity_detector as ad
        vistos = self._espia(monkeypatch, ad)
        cfg = tmp_path / "ambiguity.yaml"
        cfg.write_text("{{{{ no es yaml", encoding="utf-8")
        det = ad.AmbiguityDetector(str(cfg))
        check("template por defecto", "Genera código directo" in det.get_rewrite_template("low", "q"),
              "", SECTION)
        preguntas = det.get_clarification_questions("hazlo mejor")
        check("preguntas por defecto", isinstance(preguntas, list) and len(vistos) == 2,
              str(vistos), SECTION)
        assert len(vistos) == 2, "los dos sitios (templates y preguntas) deben degradar visible"

    def test_parametro_persistido_invalido_no_rompe_la_carga(self, monkeypatch, tmp_path):
        import json
        from src.core import feedback_loop_detectors_handlers as fl
        vistos = self._espia(monkeypatch, fl)
        p = tmp_path / "opt.json"
        p.write_text(json.dumps({"current_params": {"temperature": {"base": "no-numero"}}}),
                     encoding="utf-8")
        fl.ParameterOptimizer(persist_path=str(p))
        check("valor no numérico degradado en ruta crítica de feedback",
              len(vistos) == 1 and "feedback" in vistos[0][1].lower(), str(vistos), SECTION)
        assert len(vistos) == 1

    def test_persistencia_del_cache_no_propaga_al_destruir(self, monkeypatch, tmp_path):
        from src.router import cache as rc
        vistos = self._espia(monkeypatch, rc)
        c = rc.RouterCache(persist_path=tmp_path / "cache.json")

        def boom():
            raise OSError("disco lleno")

        monkeypatch.setattr(c, "_save_persist", boom)
        c.__del__()
        check("destructor sin propagar", len(vistos) == 1 and "__del__" in vistos[0][1],
              str(vistos), SECTION)
        monkeypatch.setattr(rc, "_router_cache_instance", c)
        rc.reset_router_cache()
        check("reset_router_cache degradado", len(vistos) == 2, str(vistos), SECTION)
        assert len(vistos) == 2

    def test_fallo_del_logger_detallado_no_rompe_la_clasificacion(self, monkeypatch):
        from src.router import classifier_core as cc
        from src.router import classifier_embedding as ce
        from src.memory import embedding_model_singleton as ems
        vistos = self._espia(monkeypatch, ce)

        def boom(*_a, **_k):
            raise TypeError("singleton caído")

        monkeypatch.setattr(ems, "get_embedding_model", boom)
        monkeypatch.setattr(cc, "_log_error", boom)

        class Doble(ce.ClassifierEmbeddingMixin):
            _training_vectors = {}
            threshold = 0.5
            _stats = {}

            def _record_feature_std(self, scores):
                return None

        emb = Doble()._get_embedding("hola")
        check("fallback a hash embeddings", isinstance(emb, list) and len(emb) > 0, str(len(emb)),
              SECTION)
        check("logger detallado caído degradado",
              len(vistos) == 1 and "_log_error" in vistos[0][1], str(vistos), SECTION)
        assert len(vistos) == 1 and isinstance(emb, list)

    def test_futuros_de_memoria_no_rompen_el_resumen(self, monkeypatch):
        import concurrent.futures as cf
        import types as _t
        from src.memory import gateway as gw
        vistos = self._espia(monkeypatch, gw)

        class FuturoRoto:
            def result(self, timeout=None):
                raise TypeError("firma rota del componente")

        g = gw.MemoryGateway.__new__(gw.MemoryGateway)
        g.session_id = "s"
        g._strategy_cache = {}
        g._short_term = _t.SimpleNamespace(get_stats=lambda: {})
        g._long_term = _t.SimpleNamespace(get_session_stats=lambda *_a: {})
        g._optimizer = _t.SimpleNamespace(get_stats=lambda: {})
        g._kg = _t.SimpleNamespace(get_stats=lambda: {})
        monkeypatch.setattr(g, "_submit", lambda *_a, **_k: FuturoRoto())
        executor = cf.ThreadPoolExecutor(max_workers=1)
        g._executor = executor
        try:
            resumen = g.get_session_summary()
        finally:
            executor.shutdown(wait=False)
        check("resumen de sesión devuelto igual", isinstance(resumen, dict)
              and resumen.get("session_id") == "s", str(type(resumen)), SECTION)
        check("los 4 futuros caídos se degradan visiblemente", len(vistos) == 4, str(vistos), SECTION)
        assert isinstance(resumen, dict) and len(vistos) == 4

    def test_limpieza_de_temporales_del_ocr_no_propaga(self, monkeypatch, tmp_path):
        import shutil as _sh
        import subprocess
        import tempfile as _tf
        from src.interfaces import __name__ as _  # noqa: F401 — asegura el paquete
        from src.interfaces.components import chat as ch
        vistos = self._espia(monkeypatch, ch)
        monkeypatch.setattr(_sh, "which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: None)
        monkeypatch.setattr(_tf, "mkdtemp", lambda **_k: str(tmp_path / "ocr"))
        (tmp_path / "ocr").mkdir()

        def boom(*_a, **_k):
            raise OSError("rmtree roto")

        monkeypatch.setattr(_sh, "rmtree", boom)
        check("sin páginas → None", ch._ocr_scanned_pdf("/tmp/no_existe.pdf") is None, "", SECTION)
        check("limpieza fallida degradada",
              len(vistos) == 1 and "ocr_scanned_pdf" in vistos[0][1], str(vistos), SECTION)
        assert len(vistos) == 1

    def test_log_de_detalle_de_vision_no_rompe_el_resultado(self, monkeypatch):
        import tools.detailed_logger as dl
        from src.interfaces.web import vision_service as vs
        vistos = self._espia(monkeypatch, vs)

        class LoggerRoto:
            is_initialized = True

            def log_event(self, *_a, **_k):
                raise TypeError("firma rota")

        monkeypatch.setattr(dl, "get_logger", lambda: LoggerRoto())
        vs._log_outcome("tesseract", True, None, 1.5, 10, "qwen3-vl:4b")
        check("log de detalle degradado",
              len(vistos) == 1 and "_log_outcome" in vistos[0][1], str(vistos), SECTION)
        assert len(vistos) == 1


def test_importar_el_modulo_cli_desde_el_paquete_no_recursa():
    """`ISSUE-145`: `from src.interfaces.cli import main` recursaba bajo pytest.

    El `__getattr__` perezoso del paquete hacía `from . import main` adentro, y esa forma
    vuelve a preguntar por el atributo ⇒ se llamaba a sí misma sin fin.
    """
    import importlib
    paquete = importlib.import_module("src.interfaces.cli")
    assert paquete.main is importlib.import_module("src.interfaces.cli.main")
    from src.interfaces.cli import main as modulo
    assert modulo is paquete.main
