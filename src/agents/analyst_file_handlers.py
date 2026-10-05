#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — analyst_file_handlers.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/agents/analyst_file_handlers.py
#
# ✅ v0.6.9v (REFACTOR): handlers de análisis de archivos por tipo extraídos de
#   src/agents/analyst.py (el original queda intacto).
#
#   Qué se movió desde analyst.py (sin cambios de comportamiento):
#     - `analyze_file`         → dispatcher por extensión
#     - `_analyze_jsonl` / `_analyze_json` / `_analyze_pdf` / `_analyze_log`
#     - `_analyze_toon` / `_analyze_txt` / `_analyze_csv`
#     - `_generic_analysis`    → fallback para tipos no reconocidos
#
#   `AnalystAgent` hereda ahora `AnalystFileHandlersMixin`, así la API pública
#   (self.analyze_file, self._analyze_*) no cambia.
#
#   Los handlers llaman a `self.execute(...)` (definido en AnalystAgent) con un
#   `Message` y `analysis_type`, sin importar analyst.py — evita el ciclo.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import json

from src.domain.entities import Message
from src.domain.exceptions import AgentError
from .base import AgentResult


class AnalystFileHandlersMixin:
    """Análisis de archivos por tipo (JSONL/JSON/PDF/LOG/TOON/TXT/CSV).

    Extraído de AnalystAgent (50/50): este mixin se ocupa de leer el archivo,
    armar el prompt y delegar a `self.execute()` para el análisis LLM. La lógica
    es independiente del estado del agente salvo `self.execute`.
    """

    def analyze_file(self, file_path: str, file_type: str) -> AgentResult:
        """
        Dispatcher de análisis según tipo de archivo.

        Args:
            file_path: Ruta absoluta o relativa al archivo a analizar
            file_type: Extensión del archivo (.jsonl, .json, .pdf, .log, .toon, .txt, .csv)

        Returns:
            AgentResult con análisis del archivo o error descriptivo

        ✅ v0.6.9v (ISSUE-007): convertido de async a SÍNCRONO. Los handlers
        internos (`_analyze_*`) ya eran síncronos y ningún caller hace `await
        analyze_file` (verificado: el endpoint /analyze_file de routes.py usa
        `analyst.execute()`, no `analyze_file`). Mantener la firma async era
        deuda de diseño.
        """
        handlers = {
            '.jsonl': self._analyze_jsonl,
            '.json': self._analyze_json,
            '.pdf': self._analyze_pdf,
            '.log': self._analyze_log,
            '.toon': self._analyze_toon,
            '.txt': self._analyze_txt,
            '.csv': self._analyze_csv,
        }

        handler = handlers.get(file_type.lower(), self._generic_analysis)

        try:
            return handler(file_path)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"File analysis failed for {file_path}: {e}")
            return AgentResult(
                success=False,
                content="",
                agent_type=self.AGENT_TYPE,
                model_used=self.config.model,
                task_type=f"analysis_file_{file_type}",
                latency_ms=0.0,
                error=f"File analysis error: {str(e)}",
            )

    def _analyze_jsonl(self, file_path: str) -> AgentResult:
        """
        Analiza archivo JSONL (JSON Lines).

        Args:
            file_path: Ruta al archivo .jsonl

        Returns:
            AgentResult con análisis de los primeros 10 registros

        Raises:
            AgentError: Si falla la lectura o parsing del archivo
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = [json.loads(line) for line in f.readlines()[:100]]

            content = json.dumps(lines[:10], indent=2, ensure_ascii=False)
            prompt = f"Analiza este archivo JSONL ({len(lines)} registros):\n{content[:3000]}"

            return self.execute(
                Message(content=prompt, role="user"),
                analysis_type="data"
            )
        except Exception as e:
            raise AgentError(f"JSONL analysis failed: {e}")

    def _analyze_json(self, file_path: str) -> AgentResult:
        """
        Analiza archivo JSON estructurado.

        Args:
            file_path: Ruta al archivo .json

        Returns:
            AgentResult con análisis de la estructura y datos

        Raises:
            AgentError: Si falla la lectura o parsing del JSON
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            content = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
            prompt = f"Analiza este archivo JSON:\n{content}"

            return self.execute(
                Message(content=prompt, role="user"),
                analysis_type="data"
            )
        except Exception as e:
            raise AgentError(f"JSON analysis failed: {e}")

    def _analyze_pdf(self, file_path: str) -> AgentResult:
        """
        Analiza archivo PDF (requiere extracción previa de texto).

        Nota: Este método asume que el PDF ya fue convertido a texto plano.
        Para PDFs binarios reales, integrar PyPDF2 o pdfplumber.

        Args:
            file_path: Ruta al archivo .txt con contenido extraído de PDF

        Returns:
            AgentResult con análisis del documento

        Raises:
            AgentError: Si falla la lectura del archivo
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()[:5000]

            prompt = f"Analiza este documento PDF:\n{content}"

            return self.execute(
                Message(content=prompt, role="user"),
                analysis_type="document"
            )
        except Exception as e:
            raise AgentError(f"PDF analysis failed: {e}")

    def _analyze_log(self, file_path: str) -> AgentResult:
        """
        Analiza archivo de logs identificando errores y patrones.

        Args:
            file_path: Ruta al archivo .log

        Returns:
            AgentResult con análisis de errores, warnings y patrones temporales

        Raises:
            AgentError: Si falla la lectura del archivo
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            # Analizar últimas 50 líneas para contexto reciente
            sample = lines[-50:] if len(lines) > 50 else lines
            content = ''.join(sample)[:3000]

            prompt = f"Analiza este log ({len(lines)} líneas). Identifica errores, warnings, patrones:\n{content}"

            return self.execute(
                Message(content=prompt, role="user"),
                analysis_type="data"
            )
        except Exception as e:
            raise AgentError(f"Log analysis failed: {e}")

    def _analyze_toon(self, file_path: str) -> AgentResult:
        """
        Analiza archivo .toon (formato custom AXIOMA).

        Args:
            file_path: Ruta al archivo .toon

        Returns:
            AgentResult con análisis de la estructura TOON

        Raises:
            AgentError: Si falla la lectura del archivo
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()[:5000]

            prompt = f"Analiza este archivo TOON (formato AXIOMA):\n{content}"

            return self.execute(
                Message(content=prompt, role="user"),
                analysis_type="structure"
            )
        except Exception as e:
            raise AgentError(f"TOON analysis failed: {e}")

    def _analyze_txt(self, file_path: str) -> AgentResult:
        """
        Analiza archivo de texto plano.

        Args:
            file_path: Ruta al archivo .txt

        Returns:
            AgentResult con análisis de contenido y estructura

        Raises:
            AgentError: Si falla la lectura del archivo
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()[:5000]

            prompt = f"Analiza este documento:\n{content}"

            return self.execute(
                Message(content=prompt, role="user"),
                analysis_type="document"
            )
        except Exception as e:
            raise AgentError(f"TXT analysis failed: {e}")

    def _analyze_csv(self, file_path: str) -> AgentResult:
        """
        Analiza archivo CSV identificando columnas y patrones.

        Args:
            file_path: Ruta al archivo .csv

        Returns:
            AgentResult con análisis de estructura y datos relevantes

        Raises:
            AgentError: Si falla la lectura del archivo
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()[:50]

            content = ''.join(lines)
            prompt = f"Analiza este CSV ({len(lines)} filas). Identifica columnas, patrones, datos relevantes:\n{content}"

            return self.execute(
                Message(content=prompt, role="user"),
                analysis_type="data"
            )
        except Exception as e:
            raise AgentError(f"CSV analysis failed: {e}")

    def _generic_analysis(self, file_path: str) -> AgentResult:
        """
        Análisis genérico para tipos de archivo no reconocidos.

        Args:
            file_path: Ruta al archivo de tipo desconocido

        Returns:
            AgentResult con análisis básico del contenido

        Raises:
            AgentError: Si falla la lectura del archivo
        """
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()[:3000]

            prompt = f"Analiza este archivo:\n{content}"

            return self.execute(
                Message(content=prompt, role="user"),
                analysis_type="document"
            )
        except Exception as e:
            raise AgentError(f"Generic analysis failed: {e}")
