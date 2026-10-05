#!/usr/bin/env python3
import logging
# ═══════════════════════════════════════════════════════════════
# axioma_doc_md_generator.py - Generador de documentación Markdown
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_md_generator.py
# Autor: SaintWick
# Versión: 3.9.0
#
# Genera documentación legible en formato Markdown a partir del análisis
# del proyecto, en paralelo al generador TOON. Incluye secciones de
# resumen, arquitectura, archivos principales, componentes, flujos,
# clases, configuración, variables de entorno y estadísticas, con
# integración de FlowMapper y badges de estado. Aplica filtros de
# seguridad para redactar archivos sensibles.
# ═══════════════════════════════════════════════════════════════
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

FLOW_MAPPER_AVAILABLE = False
try:
    from axioma_doc_flow_mapper import get_integration_badge_for_symbol
    FLOW_MAPPER_AVAILABLE = True
except ImportError as _d2e:
    logging.getLogger(__name__).debug(f"[D2 axioma_doc_md_generator.py:24] excepción degradada (intencional): {_d2e}")

from axioma_doc_base import (
    AXIOMA_PROJECT_VERSION,
    CRITICAL_FLOWS_DEFINITION,
    DOCUMENTADOR_VERSION,
)

# ✅ SECURITY PATCH v1.0: Filtro de archivos sensibles para el generador Markdown
try:
    from sensitive_filter import make_default_filter
    from axioma_doc_core import (
        SENSITIVE_FILES_PLACEHOLDER,
        SENSITIVE_CLASS_PLACEHOLDER,
        SENSITIVE_CONSTANT_PLACEHOLDER,
        SENSITIVE_BADGE,
    )
    _SECURITY_FILTER = make_default_filter()
    _SECURITY_AVAILABLE = True
except ImportError:
    _SECURITY_FILTER = None
    _SECURITY_AVAILABLE = False
    SENSITIVE_FILES_PLACEHOLDER = "[🔐 ARCHIVO SENSIBLE — CONTENIDO RESTRINGIDO]"
    SENSITIVE_CLASS_PLACEHOLDER = "[🔐 Clase en archivo sensible]"
    SENSITIVE_CONSTANT_PLACEHOLDER = "[REDACTED]"
    SENSITIVE_BADGE = "[🔐 SENSIBLE]"

# Umbral de complejidad para mostrar métodos internos (mismo que generator_sections)
_INTERNAL_METHODS_COMPLEXITY_THRESHOLD = 3
_INTERNAL_METHODS_MIN_WITH_DOCSTRING = 2


class MarkdownDocGenerator:
    """
    Genera documentación Markdown desde el mismo ImprovedProjectAnalyzer
    que usa el generador TOON. Sin duplicar lógica de análisis.
    """
    def __init__(self, analyzer):
        self.a = analyzer

    # ─── utilidades ───────────────────────────────────────────────

    @staticmethod
    def _code(content: str, lang: str = "python") -> str:
        return f"```{lang}\n{content}\n```"

    @staticmethod
    def _table(headers: List[str], rows: List[List[str]]) -> str:
        if not rows:
            return ""
        sep = " | ".join(["---"] * len(headers))
        hdr = " | ".join(headers)
        lines = [f"| {hdr} |", f"| {sep} |"]
        for row in rows:
            safe_row = [str(c).replace('|', '\\|').replace('\n', ' ') for c in row]
            lines.append("| " + " | ".join(safe_row) + " |")
        return "\n".join(lines)

    def _get_integration_badge(self, symbol: str) -> str:
        if FLOW_MAPPER_AVAILABLE and hasattr(self.a, 'integration_report') and self.a.integration_report:
            return get_integration_badge_for_symbol(symbol, self.a.integration_report)
        return "[❓ SIN RASTREO]"

    def _get_file_badge(self, rel_path: str) -> str:
        """
        ✅ BUG E FIX v3.9.0: Lee badge desde info.integration_badge (campo separado).
        Fallback: parsea purpose si tiene prefijo legacy.
        """
        info = self.a.files_info.get(rel_path)
        if info is None:
            return ""
        if hasattr(info, 'integration_badge'):
            return info.integration_badge
        for prefix in ("[✅ CONECTADO]", "[⚠️ AISLADO]"):
            if info.purpose.startswith(prefix):
                return prefix
        return ""

    def _clean_purpose(self, purpose: str) -> str:
        """Limpia prefijos de badge legacy que pudieran estar en purpose."""
        for prefix in ("[✅ CONECTADO] ", "[⚠️ AISLADO] "):
            if purpose.startswith(prefix):
                return purpose[len(prefix):]
        return purpose

    def _has_relevant_internal_methods(self, ec) -> bool:
        """Mismo criterio que DocumentSectionGenerator (BUG F)."""
        file_info = self.a.files_info.get(ec.file)
        complexity = getattr(file_info, 'complexity', 0) if file_info else 0
        if complexity >= _INTERNAL_METHODS_COMPLEXITY_THRESHOLD:
            return True
        private_with_doc = [
            m for m in ec.methods
            if m.name.startswith('_')
            and m.name != '__init__'
            and not m.name.startswith('__')
            and m.docstring
        ]
        return len(private_with_doc) >= _INTERNAL_METHODS_MIN_WITH_DOCSTRING

    def _format_method_sig(self, m) -> str:
        async_pfx = "async " if m.is_async else ""
        params = ", ".join(
            (f"{p.name}: {p.type_hint}" if p.type_hint else p.name)
            + (f" = {p.default}" if p.default else "")
            for p in m.params
        )
        ret = f" -> {m.return_type}" if m.return_type else ""
        doc = f"  # {m.docstring.split(chr(10))[0][:70]}" if m.docstring else ""
        return f"{async_pfx}def {m.name}({params}){ret}{doc}"

    # ─── secciones ────────────────────────────────────────────────

    def _header(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        ai = f"✅ Ollama `{self.a.ollama.model}`" if self.a._use_ollama else "ℹ️ Sin análisis IA"
        flow_status = " | 🔀 FlowMapper v3.9 Activo" if (
            hasattr(self.a, 'integration_report') and self.a.integration_report
        ) else ""

        toc = [
            "1. [Resumen ejecutivo](#1-resumen-ejecutivo)",
            "2. [Arquitectura](#2-arquitectura)",
            "3. [Archivos principales](#3-archivos-principales)",
            "   - [Inventario completo de archivos](#inventario-completo-de-archivos)",
            "   - [Funciones de módulo](#funciones-de-módulo)",
            "4. [Componentes clave](#4-componentes-clave)",
            "5. [Flujos críticos](#5-flujos-críticos)",
            "6. [Contexto y Flujo de Datos](#6-contexto-y-flujo-de-datos)",
            "7. [Integración Asíncrona](#7-integración-asíncrona)",
            "8. [Análisis de clases](#8-análisis-de-clases)",
            "9. [TaskTypes disponibles](#9-tasktypes-disponibles)",
            "10. [Sistema de memoria](#10-sistema-de-memoria)",
            "11. [Modelos IA detectados](#11-modelos-ia-detectados)",
            "12. [Mapa de dependencias](#12-mapa-de-dependencias)",
            "13. [Configuración](#13-configuración)",
            "14. [Contratos de datos](#14-contratos-de-datos)",
            "15. [Variables de entorno](#15-variables-de-entorno)",
            "16. [Problemas detectados](#16-problemas-detectados)",
            "17. [Estadísticas](#17-estadísticas)",
            "18. [Estado de Conexión de Módulos](#18-estado-de-conexión-de-módulos)",
        ]

        lines = [
            # ✅ v0.6.9w: versión REAL del proyecto (leída de src/__init__.py),
            # antes hardcodeada como v0.3.6.
            f"# AXIOMA v{AXIOMA_PROJECT_VERSION} — Documentación Completa",
            f"> Generado: {now} | {ai}{flow_status}",
            f"> Documentador v{DOCUMENTADOR_VERSION} — BUG A/B/C/D/E/F corregidos",
            "## Índice",
            "\n".join(toc),
            "---",
            "",
        ]
        return "\n".join(lines)

    def _resumen(self) -> str:
        s = self.a.stats
        rows = [
            ["Directorios", str(s.total_dirs)],
            ["Archivos Python", str(s.python_files)],
            ["Líneas de código", f"{s.total_lines:,}"],
            ["Clases (incl. anidadas)", str(s.total_classes)],
            ["Funciones (incl. métodos)", str(s.total_functions)],
            ["Modelos IA detectados", str(len(self.a.ai_models))],
            ["Sistemas de memoria", str(len(self.a.memory_systems))],
            ["TaskTypes definidos", str(len(self.a.task_types))],
            ["Variables de entorno", str(len(self.a.env_vars))],
        ]

        desc = [
            "AXIOMA es un sistema de agente IA multi-modelo que:",
            "- Orquesta múltiples modelos locales (Ollama) según la tarea",
            "- Mantiene memoria persistente usando LanceDB + SQLite + Embeddings",
            "- Aprende de interacciones con clasificadores ML entrenables",
            "- Ejecuta código Python de forma segura con `CoderAgent`",
            "- Integra APIs externas (búsqueda web, datos financieros, clima)",
            "- Valida respuestas con Chain of Thought forzado + auditoría CWE Top-25 (`ValidatorAgent`)",
            "- Rastrea flujos cruzados y badges de integración reales (v3.9.0)",
        ]

        lines = ["## 1. Resumen ejecutivo"]
        lines.extend(desc)
        lines.append("")
        lines.append(self._table(["Métrica", "Valor"], rows))
        lines.append("")
        lines.append("> **Alcance del escaneo:** excluye `data/`, `venv/`, `deepseek-harness/`, "
                     "`inforchat/`, internos de `.git`, caches, el propio `tools/documentador/` y, "
                     "por seguridad, `push.py`/`commit.py`/`.env*`. Los modelos listados son los "
                     "REALMENTE instalados en Ollama (`ollama list`).")
        lines.append("---")
        return "\n".join(lines)

    def _arquitectura(self) -> str:
        src_path = self.a.project_root / "src"
        folders = [
            ("core", "Dispatcher — punto de entrada del sistema"),
            ("domain", "Tipos, entidades, excepciones, contratos"),
            ("llm", "OllamaClient con circuit breaker y retry"),
            ("memory", "Gateway unificado + 3 capas de memoria"),
            ("router", "Clasificador + SmartRouter + Caché L1/L2"),
            ("agents", "Coder, Searcher, Researcher, Analyst, Validator"),
            ("handlers", "DataHandler — dominios deterministas sin LLM"),
            ("interfaces", "Web FastAPI + CLI"),
            ("multimodal", "Pipeline de Voz, STT, TTS, Sandbox"),
        ]
        existing = [(f, d) for f, d in folders if (src_path / f).exists()]
        tree = ["```", "axioma/", "├── src/"]
        for i, (f, d) in enumerate(existing):
            prefix = "└──" if i == len(existing) - 1 else "├──"
            tree.append(f"│   {prefix} {f}/   # {d}")
        tree += [
            "├── config/         # Configuraciones globales",
            "├── data/           # Datos persistentes (BD, vectores)",
            "├── tools/          # Utilidades y documentador",
            "├── tests/          # Tests unitarios",
            "└── logs/           # Logs del sistema",
            "```",
        ]

        lines = ["## 2. Arquitectura"]
        lines.append("```")
        lines.append("Usuario (GUI / CLI)")
        lines.append("        │")
        lines.append("        ▼")
        lines.append("┌───────────────────┐")
        lines.append("│    Dispatcher     │  ← Punto de entrada")
        lines.append("└────────┬──────────┘")
        lines.append("         │")
        lines.append("         ├─→ SmartRouter ────→ Clasificación + selección de modelo")
        lines.append("         ├─→ DataHandler ────→ Clima / Finanzas / Hora / Noticias")
        lines.append("         ├─→ MemoryGateway ──→ LanceDB + SQLite + ShortTerm")
        lines.append("         ├─→ OllamaClient ───→ Circuit breaker + retry + timeouts")
        lines.append("         └─→ Agents ─────────→ Coder · Searcher · Researcher · Analyst · Validator")
        lines.append("```")
        lines.append("### Estructura de directorios")
        lines.append("\n".join(tree))
        lines.append("---")
        return "\n".join(lines)

    def _archivos_principales(self) -> str:
        """
        ✅ BUG E FIX v3.9.0: Badge desde integration_badge, purpose limpio.
        ✅ SECURITY PATCH v1.0: Archivos sensibles se muestran con placeholder.
        """
        top = sorted(self.a.files_info.items(), key=lambda x: x[1].lines, reverse=True)[:30]
        rows = []
        for i, (p, info) in enumerate(top, 1):
            # ✅ SECURITY: detectar archivo sensible
            is_sensitive = False
            if _SECURITY_AVAILABLE and _SECURITY_FILTER:
                is_sensitive = _SECURITY_FILTER.is_sensitive_file(Path(p)).is_sensitive

            if is_sensitive:
                badge = (self._get_file_badge(p) + " " + SENSITIVE_BADGE).strip()
                purpose = SENSITIVE_FILES_PLACEHOLDER
                file_name_display = f"{p} {SENSITIVE_BADGE}"
            else:
                badge = self._get_file_badge(p)
                purpose = self._clean_purpose(info.purpose)[:60]
                file_name_display = p

            rows.append([str(i), file_name_display, str(info.lines), info.category, badge, purpose])

        lines = [
            "## 3. Archivos principales",
            self._table(["#", "Archivo", "Líneas", "Categoría", "Badge", "Propósito"], rows),
            "_Top 30 archivos por líneas de código. Archivos 🔐 SENSIBLE tienen contenido restringido._",
            "",
            self._inventario_archivos(),
        ]
        return "\n".join(lines)

    def _inventario_archivos(self) -> str:
        """
        ✅ v4.2.2 (2026-09-28): INVENTARIO COMPLETO de los archivos analizados.

        Motivo (medido): la sección 3 solo mostraba el Top 30 por líneas y la 8
        solo clases con métodos públicos, así que **82 de 294 archivos
        analizados no aparecían NINGUNA vez** en la documentación: los 26
        `__init__.py`, 47 archivos que solo definen funciones de módulo (que no
        se renderizaban en ninguna sección) y 9 módulos de producción cuyas
        clases eran mixins con métodos privados.

        Este inventario garantiza que TODO archivo analizado quede registrado —
        también los nuevos, que entran solos porque el escaneo es en vivo
        (§17 lo confirma: "En caché (omitidos): 0").
        """
        items = sorted(self.a.files_info.items(), key=lambda x: x[0])

        def _seguro(rel: str) -> bool:
            if not (_SECURITY_AVAILABLE and _SECURITY_FILTER):
                return True
            return not _SECURITY_FILTER.is_sensitive_file(Path(rel)).is_sensitive

        filas = []
        publicas = []
        for i, (p, info) in enumerate(items, 1):
            if not _seguro(p):
                filas.append([str(i), f"{p} {SENSITIVE_BADGE}", "—", "SENSIBLE", "—", "—"])
                continue
            n_clases = len(info.classes)
            funcs_pub = [f for f in info.functions if not f.startswith("_")]
            filas.append([
                str(i), p, str(info.lines), info.category,
                str(n_clases) if n_clases else "—",
                str(len(funcs_pub)) if funcs_pub else "—",
            ])
            if funcs_pub:
                publicas.append((p, funcs_pub))

        total_funcs = sum(len(f) for _, f in publicas)
        out = [
            "### Inventario completo de archivos",
            "",
            f"_Los **{len(items)}** archivos analizados, ordenados por ruta. "
            f"`Clases` y `Func. módulo` son lo que define cada uno (— = ninguno): "
            f"permite verificar que ningún archivo quede sin registrar._",
            "",
            self._table(["#", "Archivo", "Líneas", "Categoría", "Clases", "Func. módulo"], filas),
            "",
        ]
        if publicas:
            out += [
                f"### Funciones de módulo ({total_funcs} públicas en {len(publicas)} archivos)",
                "",
                "_Funciones definidas a nivel de módulo (no viven en ninguna clase); "
                "antes no aparecían en ninguna sección. Se listan los nombres; la "
                "firma completa está en el código._",
                "",
            ]
            for p, funcs in publicas:
                nombres = ", ".join(f"`{f}`" for f in funcs[:12])
                if len(funcs) > 12:
                    nombres += f" _(+{len(funcs) - 12} más)_"
                out.append(f"- `{p}`: {nombres}")
            out.append("")
        out.append("---")
        return "\n".join(out)

    def _componentes_clave(self) -> str:
        """
        ✅ BUG F FIX v3.9.0: Muestra métodos internos relevantes para clases
        con complejidad >= 3 o con métodos privados que tienen docstring.
        ✅ SECURITY PATCH v1.0: Salta archivos sensibles con placeholder.
        """
        PRIORITY = [
            "src/core/dispatcher.py",
            "src/router/smart_router.py",
            "src/router/classifier.py",
            "src/router/classifier_core.py",
            "src/memory/gateway.py",
            "src/llm/client.py",
            "src/agents/base.py",
            "src/agents/coder.py",
            "src/agents/searcher.py",
            "src/agents/researcher.py",
            "src/agents/analyst.py",
            "src/agents/validator.py",
            "src/handlers/data_handler.py",
            "config/settings.py",
            "src/domain/types.py",
        ]
        lines = ["## 4. Componentes clave"]

        for path in PRIORITY:
            if path not in self.a.files_info:
                continue

            # ✅ SECURITY: saltar archivos sensibles (los muestra como placeholder)
            if _SECURITY_AVAILABLE and _SECURITY_FILTER:
                if _SECURITY_FILTER.is_sensitive_file(Path(path)).is_sensitive:
                    lines.append(f"### `{path}` {SENSITIVE_BADGE}")
                    lines.append(
                        f"**Categoría:** SENSIBLE | **Líneas:** {self.a.files_info[path].lines}"
                    )
                    lines.append(f"**Propósito:** {SENSITIVE_FILES_PLACEHOLDER}")
                    lines.append(
                        "_Archivo excluido de la documentación por contener credenciales._"
                    )
                    lines.append("")
                    lines.append("---")
                    continue
            info = self.a.files_info[path]
            badge = self._get_file_badge(path)
            lines.append(f"### `{path}` {badge}")
            lines.append(
                f"**Categoría:** {info.category} | **Líneas:** {info.lines} "
                f"| **Complejidad:** {info.complexity}"
            )
            lines.append(f"**Propósito:** {self._clean_purpose(info.purpose)}")
            if info.docstring:
                safe_doc = info.docstring[:200].replace('\n', ' ')
                lines.append(f"> {safe_doc}")

            for cls_name in info.classes:
                ec_list = [ec for ec in self.a.enriched_classes.get(cls_name, [])
                           if ec.file == path]
                for ec in ec_list:
                    bases = f"({', '.join(ec.bases)})" if ec.bases else ""
                    cls_badge = self._get_integration_badge(cls_name)
                    lines.append(f"#### `class {cls_name}{bases}` {cls_badge}")
                    summary = ec.ai_summary or (
                        ec.docstring.split('\n')[0][:150] if ec.docstring else ""
                    )
                    if summary:
                        lines.append(f"{summary}\n")

                    pub = [m for m in ec.methods
                           if not m.name.startswith('_') or m.name == '__init__']
                    if pub:
                        lines.append("**Métodos públicos:**")
                        lines.append("```python")
                        for m in pub[:8]:
                            lines.append(self._format_method_sig(m))
                        if len(pub) > 8:
                            lines.append(f"# ... {len(pub) - 8} métodos más")
                        lines.append("```")

                    # ✅ BUG F FIX: métodos internos relevantes
                    if self._has_relevant_internal_methods(ec):
                        internal = [
                            m for m in ec.methods
                            if m.name.startswith('_')
                            and m.name != '__init__'
                            and not m.name.startswith('__')
                            and m.docstring
                        ]
                        if internal:
                            lines.append("**Métodos internos relevantes:**")
                            lines.append("```python")
                            for m in internal[:8]:
                                lines.append(self._format_method_sig(m))
                            if len(internal) > 8:
                                lines.append(f"# ... {len(internal) - 8} más")
                            lines.append("```")

                    lines.append("")

            if info.functions:
                funcs_display = ", ".join(f"`{f}`" for f in info.functions[:6])
                if len(info.functions) > 6:
                    funcs_display += f" + {len(info.functions) - 6} más"
                lines.append(f"**Funciones módulo:** {funcs_display}")

            lines.append("---")
        return "\n".join(lines)

    def _flujos(self) -> str:
        flows = []
        if self.a.critical_flows:
            for cf in self.a.critical_flows:
                steps = []
                for s in cf.steps:
                    if isinstance(s, dict):
                        steps.append(s)
                    else:
                        steps.append({
                            "step": getattr(s, "step_number", 0),
                            "component": getattr(s, "component", ""),
                            "fallback": getattr(s, "fallback", None),
                            "timeout": getattr(s, "timeout", None),
                        })
                flows.append({
                    "name": cf.name,
                    "description": cf.description,
                    "steps": steps,
                    "failure_points": cf.failure_points,
                    "integration_status": getattr(cf, 'integration_status', '[❓ PENDIENTE]'),
                })
        else:
            flows = [
                {
                    "name": fd["name"],
                    "description": fd["description"],
                    "steps": fd["steps"],
                    "failure_points": fd["failure_points"],
                    "integration_status": "[❓ SIN FLOWMAPPER]",
                }
                for fd in CRITICAL_FLOWS_DEFINITION
            ]

        lines = ["## 5. Flujos críticos"]
        for flow in flows:
            lines.append(
                f"### {flow['name'].replace('_', ' ').title()} {flow['integration_status']}"
            )
            lines.append(f"_{flow['description']}_")
            rows = []
            for step in flow["steps"]:
                component = step.get("component", "")
                fallback = step.get("fallback") or "—"
                timeout = step.get("timeout") or "—"
                comp_symbol = component.split('(')[0].split('.')[0]
                badge = self._get_integration_badge(comp_symbol)
                rows.append([
                    str(step.get("step", step.get("step_number", ""))),
                    f"{component} {badge}",
                    fallback,
                    timeout,
                ])
            lines.append(self._table(["Paso", "Componente", "Fallback", "Timeout"], rows))
            if flow.get("failure_points"):
                lines.append("\n**Puntos de fallo:**")
                for fp in flow["failure_points"]:
                    lines.append(f"- ⚠️ {fp}")
            lines.append("---")
        return "\n".join(lines)

    def _context_flows_md(self) -> str:
        if not FLOW_MAPPER_AVAILABLE or not hasattr(self.a, 'integration_report') or not self.a.integration_report:
            return "## 6. Contexto y Flujo de Datos\n_Activa `--trace-flows` para habilitar rastreo de contexto._\n---\n"

        report = self.a.integration_report
        lines = ["## 6. Contexto y Flujo de Datos"]
        lines.append("### Rastreo de Contratos de Datos\n")
        rows = []
        for name, trace in report.symbol_traces.items():
            if trace.instantiation_count > 0 or 'Context' in name or 'Queue' in name:
                used_in = (
                    ", ".join(trace.used_in[:3]) + ("..." if len(trace.used_in) > 3 else "")
                    if trace.used_in else "Aislado"
                )
                rows.append([
                    name,
                    trace.defined_in or 'N/A',
                    used_in,
                    str(trace.instantiation_count),
                    str(trace.call_sites),
                ])
        if rows:
            lines.append(self._table(
                ["Símbolo", "Definido_En", "Usado_En", "Instanciaciones", "Llamadas"], rows
            ))
        lines.append("\n### Contratos Activos Evaluados\n")
        for fc in report.flow_contracts:
            lines.append(f"- **{fc.name}**: {fc.badge}")
        lines.append("---")
        return "\n".join(lines)

    def _async_integration_md(self) -> str:
        lines = ["## 7. Integración Asíncrona"]
        lines.append("### Métodos `async def` y Gestión de Event Loops\n")
        async_methods = []
        for filepath, sigs in self.a.module_functions.items():
            for sig in sigs:
                if sig.is_async:
                    async_methods.append((sig.name, filepath, sig.return_type, sig.docstring))
        for ecs in self.a.enriched_classes.values():
            for ec in ecs:
                for m in ec.methods:
                    if m.is_async:
                        async_methods.append((
                            f"{ec.name}.{m.name}", ec.file, m.return_type, m.docstring
                        ))

        if not async_methods:
            lines.append("_No se detectaron firmas asíncronas en el código analizado._")
        else:
            rows = []
            for name, file, ret, doc in async_methods[:40]:
                doc_summary = doc.split('\n')[0][:80].replace('|', ' ') if doc else "Sin docstring"
                rows.append([name, file, ret or '-', doc_summary])
            lines.append(self._table(
                ["Método", "Archivo", "Tipo_Retorno", "Docstring_Resumen"], rows
            ))

        lines.append("### Recomendaciones de Gestión")
        lines.append("- **Event Loop**: `run_full_duplex` y `process_stream` corren bajo `asyncio.run()` o bucles personalizados.")
        lines.append("- **Error Handling**: `try/except` con `asyncio.CancelledError` y `TimeoutError` en puntos de entrada críticos.")
        lines.append("- **Bloqueo Síncrono**: Evitar `time.sleep`, `requests.get` en métodos `async`.")
        lines.append("---")
        return "\n".join(lines)

    def _clases(self) -> str:
        """
        ✅ BUG F FIX v3.9.0: Incluye métodos internos relevantes para clases
        con complejidad >= 3 o >= 2 métodos privados con docstring.
        ✅ SECURITY PATCH v1.0: Omite clases en archivos sensibles.
        ✅ v4.2.2 (2026-09-28): el filtro ya NO exige un método público.
        Motivo medido: 9 archivos de producción con clases (mixins con solo
        métodos privados) no aparecían NINGUNA vez en la doc —
        `src/core/dispatcher_process_guard.py`, `src/router/classifier_embedding.py`,
        `src/interfaces/components/transparency.py`, `src/utils/ui.py`,
        `src/tools_mcp/tool_fs_implementations.py`, `tools/auditor/aud_detectors_*.py`,
        `tools/actron/actron_proxy.py`. Ahora solo se omiten las clases
        realmente vacías (sin métodos ni atributos ni resumen IA).
        """
        # ✅ SECURITY: filtrar clases que están en archivos sensibles
        def _is_class_safe(ec):
            if not (_SECURITY_AVAILABLE and _SECURITY_FILTER):
                return True
            return not _SECURITY_FILTER.is_sensitive_file(Path(ec.file)).is_sensitive

        filtered = [
            ec
            for ecs in self.a.enriched_classes.values()
            for ec in ecs
            # ✅ v4.2.2: cualquier miembro cuenta (público o privado).
            if _is_class_safe(ec) and (ec.methods or ec.attributes or ec.ai_summary)
        ]
        filtered.sort(key=lambda ec: (ec.file, ec.name))
        total_enriched = sum(len(v) for v in self.a.enriched_classes.values())
        # ✅ SECURITY: contar las omitidas
        sensitive_count = 0
        if _SECURITY_AVAILABLE and _SECURITY_FILTER:
            for ecs in self.a.enriched_classes.values():
                for ec in ecs:
                    if not _is_class_safe(ec):
                        sensitive_count += 1

        lines = ["## 8. Análisis de clases"]
        sensitive_note = (
            f" ({sensitive_count} omitidas por seguridad)" if sensitive_count > 0 else ""
        )
        lines.append(
            f"_{len(filtered)} clases documentadas de {total_enriched} totales "
            f"(omitidos stubs vacíos){sensitive_note}_"
        )
        lines.append("")

        current_file = None
        for ec in filtered:
            if ec.file != current_file:
                current_file = ec.file
                file_badge = self._get_file_badge(ec.file)
                lines.append(f"\n### `{ec.file}` {file_badge}")

            bases = f"({', '.join(ec.bases)})" if ec.bases else ""
            tags = []
            if ec.is_dataclass: tags.append("dataclass")
            if ec.is_enum: tags.append("enum")
            if ec.is_pydantic: tags.append("Pydantic")
            tag_str = f" `{'` `'.join(tags)}`" if tags else ""
            badge = self._get_integration_badge(ec.name)
            lines.append(f"#### `class {ec.name}{bases}`{tag_str} {badge}")

            if ec.ai_summary:
                lines.append(f"\n{ec.ai_summary}")
            elif ec.docstring:
                lines.append(f"\n> {ec.docstring.split(chr(10))[0][:180]}")

            if ec.attributes:
                attr_rows = [[a[0], a[1] or "—"] for a in ec.attributes[:12]]
                lines.append(self._table(["Atributo", "Tipo"], attr_rows))
                lines.append("")

            # Métodos públicos
            pub = [m for m in ec.methods if not m.name.startswith("_") or m.name == "__init__"]
            if pub:
                lines.append("```python")
                for m in pub[:6]:
                    lines.append(self._format_method_sig(m))
                if len(pub) > 6:
                    lines.append(f"# ... {len(pub) - 6} métodos más")
                lines.append("```")

            # ✅ BUG F FIX: métodos internos relevantes
            if self._has_relevant_internal_methods(ec):
                internal = [
                    m for m in ec.methods
                    if m.name.startswith('_')
                    and m.name != '__init__'
                    and not m.name.startswith('__')
                    and m.docstring
                ]
                if internal:
                    lines.append("\n**Métodos internos relevantes:**")
                    lines.append("```python")
                    for m in internal[:8]:
                        lines.append(self._format_method_sig(m))
                    if len(internal) > 8:
                        lines.append(f"# ... {len(internal) - 8} más")
                    lines.append("```")

            lines.append("---")
        return "\n".join(lines)

    def _tasktypes(self) -> str:
        if not self.a.task_types:
            return "## 9. TaskTypes disponibles\n_No detectados._\n---\n"
        rows = [[t.name, t.description, t.example] for t in self.a.task_types]
        lines = [
            "## 9. TaskTypes disponibles",
            self._table(["TaskType", "Descripción", "Ejemplo"], rows),
            "---",
        ]
        return "\n".join(lines)

    def _memoria(self) -> str:
        if not self.a.memory_systems:
            return "## 10. Sistema de memoria\n_No detectado._\n---\n"
        lines = ["## 10. Sistema de memoria"]
        for m in self.a.memory_systems:
            lines.append(f"### {m.type}")
            lines.append(f"- **Ubicación:** `{m.location}`")
            lines.append(f"- **Propósito:** {m.purpose}")
            lines.append(f"- **Implementación:** `{m.implementation_file}`")
            lines.append(f"- **Tamaño estimado:** {m.size_estimate}")
            for k, v in m.additional_info.items():
                val = ", ".join(v) if isinstance(v, list) else str(v)
                lines.append(f"- **{k}:** {val}")
            lines.append("")
        lines.append("---")
        return "\n".join(lines)

    def _modelos(self) -> str:
        if not self.a.ai_models:
            return "## 11. Modelos IA detectados\n_Ninguno detectado._\n---\n"
        rows = [[n, i.size, i.purpose, i.provider] for n, i in self.a.ai_models.items()]
        lines = [
            "## 11. Modelos IA detectados",
            self._table(["Modelo", "Tamaño", "Propósito", "Proveedor"], rows),
            "---",
        ]
        return "\n".join(lines)

    def _dependencias(self) -> str:
        if not self.a.module_dependencies:
            return "## 12. Mapa de dependencias\n_No analizado._\n---\n"
        module_groups: Dict[str, List[str]] = {}
        MODULE_TYPE = {
            "core": "Core", "router": "Enrutamiento", "memory": "Memoria",
            "llm": "LLM Client", "agents": "Agentes", "domain": "Dominio",
            "handlers": "Handlers", "interfaces": "Interfaces",
            "integrations": "Integraciones", "config": "Configuración",
            "multimodal": "Multimodal",
        }
        for module_dot, node in self.a.module_dependencies.items():
            parts = module_dot.split(".")
            group = parts[1] if len(parts) >= 2 and parts[0] == "src" else parts[0]
            if group not in module_groups:
                module_groups[group] = []
            for dep in node.depends_on:
                if dep not in module_groups[group]:
                    module_groups[group].append(dep)

        rows = []
        for group, deps in sorted(module_groups.items()):
            internal = [d for d in deps if not d.startswith("ext:")]
            external = [d.replace("ext:", "") for d in deps if d.startswith("ext:")]
            dep_str = ", ".join(internal[:6])
            if external:
                dep_str += (" | " if dep_str else "") + f"_ext: {', '.join(external[:4])}_"
            rows.append([group, dep_str or "—", MODULE_TYPE.get(group, "General")])

        dep_count: Dict[str, int] = {}
        for node in self.a.module_dependencies.values():
            for dep in node.depends_on:
                dep_count[dep] = dep_count.get(dep, 0) + 1
        critical = sorted(dep_count.items(), key=lambda x: x[1], reverse=True)[:8]
        crit_rows = [[d.replace("ext:", ""), str(c)] for d, c in critical]

        lines = [
            "## 12. Mapa de dependencias",
            self._table(["Módulo", "Depende de", "Tipo"], rows),
            "### Dependencias críticas (más usadas)",
            self._table(["Módulo", "Usado por N módulos"], crit_rows),
            "---",
        ]
        return "\n".join(lines)

    def _infer_impact_md(self, name: str) -> str:
        n = name.lower()
        if 'timeout' in n:
            return "⏱ Afecta tiempos de respuesta"
        if 'model' in n:
            return "🤖 Cambia modelo IA (calidad/velocidad)"
        if 'cache' in n:
            return "💾 Afecta rendimiento y RAM"
        if 'path' in n or 'dir' in n:
            return "📁 Validar ruta existente"
        if 'threshold' in n or 'score' in n:
            return "⚖️ Afecta criterios de validación"
        if 'pattern' in n or 'regex' in n:
            return "🔍 Modifica detección de patrones"
        if 'level' in n:
            return "🎚 Afecta sensibilidad de detección"
        return "🔄 Requiere reinicio"

    def _configuracion(self) -> str:
        if not self.a.config_params:
            return "## 13. Configuración\n_No detectada._\n---\n"
        from collections import defaultdict
        py_params = [p for p in self.a.config_params
                     if not str(getattr(p, 'file', '') or '').endswith(('.yaml', '.yml'))]
        yaml_params = [p for p in self.a.config_params
                       if str(getattr(p, 'file', '') or '').endswith(('.yaml', '.yml'))]

        out = ["## 13. Configuración"]
        if py_params:
            rows = []
            for p in py_params[:40]:
                val = str(p.value)[:45] + ("…" if len(str(p.value)) > 45 else "")
                rows.append([p.name, val, p.type_hint or "—", self._infer_impact_md(p.name)])
            out.append("### settings.py")
            out.append(self._table(["Parámetro", "Valor", "Tipo", "Impacto"], rows))
            out.append("")

        if yaml_params:
            by_file: dict = defaultdict(list)
            for p in yaml_params:
                by_file[getattr(p, 'file', 'config/unknown.yaml')].append(p)
            for source_file, params in sorted(by_file.items()):
                rows = []
                for p in params[:80]:
                    val = str(p.value)[:45] + ("…" if len(str(p.value)) > 45 else "")
                    rows.append([p.name, val, p.type_hint or "—", self._infer_impact_md(p.name)])
                out.append(f"### {source_file}")
                out.append(self._table(["Clave", "Valor", "Tipo", "Impacto"], rows))
                out.append("")

        out.append("---")
        return "\n".join(out)

    def _contratos(self) -> str:
        lines = ["## 14. Contratos de datos"]
        if self.a.dataclass_contracts:
            lines.append("### Dataclasses")
            rows = [[n, info.get("file", "—")] for n, info in sorted(self.a.dataclass_contracts.items())]
            lines.append(self._table(["Nombre", "Archivo"], rows))
            lines.append("")
        if self.a.enum_definitions:
            lines.append("### Enums")
            rows = [[n, files[0]] for n, files in sorted(self.a.enum_definitions.items())]
            lines.append(self._table(["Nombre", "Archivo"], rows))
            lines.append("")
        lines.append("---")
        return "\n".join(lines)

    def _env_vars(self) -> str:
        """
        ✅ BUG D + BUG G FIX v3.9.0: Agrupa variables por categoría en lugar de
        lista plana. Refleja el output real del scanner Pydantic (35+ vars detectadas).
        """
        if not self.a.env_vars:
            return "## 15. Variables de entorno\n_No detectadas._\n---\n"

        all_vars = sorted(self.a.env_vars)
        api_vars   = [v for v in all_vars if any(k in v for k in ('API', 'KEY', 'TOKEN'))]
        llm_vars   = [v for v in all_vars if any(k in v for k in ('LLM', 'OLLAMA', 'MODEL'))]
        db_vars    = [v for v in all_vars if any(k in v for k in ('DB', 'PATH', 'DIR', 'LANCE', 'SQLITE'))]
        sec_vars   = [v for v in all_vars if any(k in v for k in ('SECRET', 'ENCRYPT', 'SESSION'))]
        categorized = set(api_vars + llm_vars + db_vars + sec_vars)
        other_vars = [v for v in all_vars if v not in categorized]

        lines = [f"## 15. Variables de entorno ({len(all_vars)} detectadas)"]
        lines.append("> Detectadas mediante scanner Pydantic BaseSettings + os.environ (v3.9.0)")
        lines.append("")

        for group_name, group_vars in [
            ("🔑 APIs Externas", api_vars),
            ("🤖 Modelos LLM / Ollama", llm_vars),
            ("💾 Base de Datos y Rutas", db_vars),
            ("🔒 Seguridad", sec_vars),
            ("⚙️ General", other_vars),
        ]:
            if group_vars:
                lines.append(f"### {group_name}")
                lines.append("```")
                for v in group_vars:
                    lines.append(v)
                lines.append("```")
                lines.append("")

        lines.append("---")
        return "\n".join(lines)

    def _problemas(self) -> str:
        real  = [d for d in self.a.duplications if not d.is_legitimate]
        legit = [d for d in self.a.duplications if d.is_legitimate]
        lines = ["## 16. Problemas detectados"]
        if real:
            lines.append(f"### ⚠️ Duplicaciones problemáticas ({len(real)})\n")
            for d in real:
                lines.append(f"**`{d.class_name}`** — {d.severity}")
                lines.append(f"_{d.reason}_")
                lines.append("Ubicaciones:")
                for loc in d.locations:
                    lines.append(f"- `{loc}`")
                lines.append("> **Acción:** Consolidar en un solo archivo y actualizar imports.\n")
        else:
            lines.append("✅ No se detectaron duplicaciones problemáticas.\n")
        if legit:
            lines.append(f"\n### ✅ Patrones de fallback legítimos ({len(legit)})\n")
            for d in legit:
                lines.append(f"- **`{d.class_name}`**: {d.reason}")
            lines.append("")
        lines.append("---")
        return "\n".join(lines)

    def _estadisticas(self) -> str:
        s = self.a.stats
        stats = self.a.get_analysis_stats() if hasattr(self.a, 'get_analysis_stats') else {}
        real_dups   = len([d for d in self.a.duplications if not d.is_legitimate])
        legit_dups  = len([d for d in self.a.duplications if d.is_legitimate])
        enriched    = sum(len(v) for v in self.a.enriched_classes.values())
        ai_analyzed = sum(1 for ecs in self.a.enriched_classes.values() for ec in ecs if ec.ai_summary)

        rows = [
            ["Directorios",                str(s.total_dirs)],
            ["Archivos Python",            str(s.python_files)],
            ["Líneas de código",           f"{s.total_lines:,}"],
            ["Clases totales (incl. anidadas)", str(s.total_classes)],
            ["Funciones totales (incl. métodos)", str(s.total_functions)],
            ["Modelos IA",                 str(len(self.a.ai_models))],
            ["Sistemas de memoria",        str(len(self.a.memory_systems))],
            ["TaskTypes",                  str(len(self.a.task_types))],
            ["Dataclasses",                str(len(self.a.dataclass_contracts))],
            ["Enums",                      str(len(self.a.enum_definitions))],
            ["Variables de entorno",       str(len(self.a.env_vars))],
            ["Duplicaciones problemáticas",str(real_dups)],
            ["Duplicaciones legítimas",    str(legit_dups)],
            ["Clases con signatures",      str(enriched)],
            ["Clases con análisis IA",     str(ai_analyzed)],
        ]

        cache_rows = [
            ["Archivos totales",   str(stats.get("total_files", s.python_files))],
            ["En caché (omitidos)",str(stats.get("cached_files", 0))],
            ["Analizados",         str(stats.get("analyzed_files", s.python_files))],
            ["Inconsistencias",    str(stats.get("inconsistencies_found", 0))],
        ]
        hit = stats.get("cached_files", 0) / max(stats.get("total_files", 1), 1) * 100
        cache_rows.append(["Cache hit rate", f"{hit:.1f}%"])

        flow_stats = []
        if hasattr(self.a, 'integration_report') and self.a.integration_report:
            flow_stats = [
                ["Contratos Evaluados", str(len(self.a.integration_report.flow_contracts))],
                ["Módulos Conectados",  str(len(self.a.integration_report.connected_modules))],
                ["Módulos Aislados",    str(len(self.a.integration_report.isolated_modules))],
            ]

        lines = [
            "## 17. Estadísticas",
            self._table(["Métrica", "Valor"], rows),
            "### Rendimiento del análisis",
            self._table(["Métrica", "Valor"], cache_rows),
        ]
        if flow_stats:
            lines.append("### Estado FlowMapper v3.9")
            lines.append(self._table(["Métrica", "Valor"], flow_stats))

        lines.append(
            f"\n---\n*Generado por `axioma_doc_main.py v{DOCUMENTADOR_VERSION}` — "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*"
        )
        return "\n".join(lines)

    def _integration_status_md(self) -> str:
        if not FLOW_MAPPER_AVAILABLE or not hasattr(self.a, 'integration_report') or not self.a.integration_report:
            return "## 18. Estado de Conexión de Módulos\n_Activa `--trace-flows` para habilitar rastreo._\n---\n"

        report = self.a.integration_report
        lines = ["## 18. Estado de Conexión de Módulos"]
        lines.append(f"**Total Módulos Rastreados:** {len(report.module_graph)}")
        lines.append(f"> Badges corregidos v3.9.0 — sin herencia por vecindad de archivo")

        lines.append("### ✅ Módulos Conectados\n")
        for mod in report.connected_modules:
            lines.append(f"- `{mod}`")

        lines.append("\n### ⚠️ Módulos Aislados o Parciales\n")
        if report.isolated_modules:
            for mod in report.isolated_modules:
                lines.append(f"- `{mod}`")
        else:
            lines.append("_Ninguno detectado._")

        lines.append("\n### Recomendaciones Automáticas\n")
        for fc in report.flow_contracts:
            if fc.status in ('ISOLATED', 'PARTIAL'):
                missing = [
                    c for c in fc.components
                    if c not in [r.get('symbol_name') for r in fc.references]
                ]
                lines.append(
                    f"- ⚡ Revisar: **{fc.name}** "
                    f"({', '.join(missing) if missing else 'Referencias cruzadas débiles'})"
                )

        lines.append("---")
        return "\n".join(lines)

    # ─── punto de entrada ─────────────────────────────────────────

    def generate(self, output_file: Path) -> None:
        """Genera el archivo .md completo."""
        sections = [
            self._header(),
            self._resumen(),
            self._arquitectura(),
            self._archivos_principales(),
            self._componentes_clave(),
            self._flujos(),
            self._context_flows_md(),
            self._async_integration_md(),
            self._clases(),
            self._tasktypes(),
            self._memoria(),
            self._modelos(),
            self._dependencias(),
            self._configuracion(),
            self._contratos(),
            self._env_vars(),
            self._problemas(),
            self._estadisticas(),
            self._integration_status_md(),
        ]

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(sections))
        print(f"   📝 Markdown generado: {output_file} ({output_file.stat().st_size // 1024} KB)")

if __name__ == "__main__":
    print("Módulo de generación Markdown. Ejecute axioma_doc_main.py")
    print(f"Versión: v{DOCUMENTADOR_VERSION} — BUG E/F/G corregidos")
    print("  BUG E: badge desde integration_badge, purpose limpio")
    print("  BUG F: métodos internos relevantes documentados")
    print("  BUG G: env_vars agrupadas por categoría (35+ vars Pydantic)")
    sec_status = "ACTIVADO" if _SECURITY_AVAILABLE else "NO DISPONIBLE"
    print(f"🔒 SECURITY PATCH v1.0: Filtro {sec_status}")
    print("   - Archivos sensibles reemplazados por placeholder en secciones 3, 4, 8")
    print("   - Constantes sensibles redactadas vía extract_module_constants wrapper")
