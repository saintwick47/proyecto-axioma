#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 — Contrato de Entrega de Código Verificable
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/code_contract.py
# Autor: SaintWick
# Fecha: 2026-08-20
# Propósito: Modela y parsea la respuesta de CoderAgent como un
#            contrato estructurado (código, dependencias, tests,
#            comando de validación, salida esperada, advertencias)
#            en vez de aceptar la respuesta cruda por heurística.
#            Fase 1 del plan de contrato de código verificable.
#            Importado por coder.py (Fase 2) y code_validator.py
#            (Fase 3) — no requiere cambios en ningún otro módulo.
#
# Decisión de diseño (ver plan_corregido_v2.md, sección "Fase 1"):
# parser por secciones markdown vía regex, no salida estructurada
# de Ollama (format=JSON Schema). Motivos: incompatible con
# chunked_generation_enabled existente, costo de grammar-decoding
# en hardware CPU-only, y qwen2.5-coder:7b está entrenado para el
# patrón de bloques ```lang fenced, no para escapar código
# multilínea dentro de un string JSON. El riesgo de parsing frágil
# lo cubre el repair loop de Fase 4 (sección faltante → retry con
# crítica específica, no fallo terminal).
# ═══════════════════════════════════════════════════════════════
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .coder_core import extract_code, extract_filename, suggest_filename

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# ENCABEZADOS DE SECCIÓN — mismo estilo que FILE_PATTERN existente
# en coder_core.py (^ARCHIVO:\s*...): etiqueta en mayúsculas + ':'
# al inicio de línea, con y sin acento para tolerar salida del LLM
# sin tildes.
# ═══════════════════════════════════════════════════════════════
_SECTION_HEADERS: Dict[str, List[str]] = {
    "codigo":            [r"C[OÓ]DIGO"],
    "dependencias":      [r"DEPENDENCIAS"],
    "tests":             [r"TESTS?"],
    "comando":           [r"COMANDO(?:\s+DE\s+VALIDACI[OÓ]N)?"],
    "salida_esperada":   [r"SALIDA\s+ESPERADA"],
    "advertencias":      [r"ADVERTENCIAS", r"SUPUESTOS", r"LIMITACIONES"],
}

_ALL_HEADER_ALTERNATIVES: List[str] = [
    alt for alts in _SECTION_HEADERS.values() for alt in alts
]

_SECTION_SPLIT_RE = re.compile(
    r"^\s*(?:" + "|".join(_ALL_HEADER_ALTERNATIVES) + r")\s*:\s*$",
    re.MULTILINE | re.IGNORECASE,
)

_BULLET_LINE_RE = re.compile(r"^\s*[-*•]\s*(.+?)\s*$")
_INLINE_CODE_RE = re.compile(r"^`([^`]+)`$")


@dataclass
class CodeFile:
    """Un archivo individual dentro del contrato (código o test)."""
    name: str
    content: str
    language: str = "python"

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a dict plano para metadata de Response."""
        return {"name": self.name, "content": self.content, "language": self.language}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodeFile":
        """Reconstruye desde el dict de to_dict() — ver CodeContract.from_dict()."""
        return cls(
            name=data.get("name", ""),
            content=data.get("content", ""),
            language=data.get("language", "python"),
        )


@dataclass
class CodeContract:
    """
    Contrato de entrega de código parseado desde la respuesta del LLM.

    `complete` y `missing` reflejan el resultado de `parse_code_contract()`
    contra los flags `code_require_*` de settings — no una verificación
    de contenido (eso es Fase 3, validación estática/dinámica).
    """
    files: List[CodeFile] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    test_files: List[CodeFile] = field(default_factory=list)
    test_command: Optional[str] = None
    expected_output: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    complete: bool = False
    missing: List[str] = field(default_factory=list)
    raw_response: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a dict plano — va en Response.metadata['code_artifact']."""
        return {
            "files":            [f.to_dict() for f in self.files],
            "dependencies":     list(self.dependencies),
            "test_files":       [f.to_dict() for f in self.test_files],
            "test_command":     self.test_command,
            "expected_output":  self.expected_output,
            "warnings":         list(self.warnings),
            "complete":         self.complete,
            "missing":          list(self.missing),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodeContract":
        """
        Reconstruye desde el dict de to_dict() — usado en
        dispatcher_handlers.py (Fase 3) para tomar el code_artifact que
        CoderAgent ya dejó en AgentResult.metadata sin tener que volver
        a parsear la respuesta cruda del LLM (parse_code_contract() ya
        corrió una vez dentro de CoderAgent.execute(), Fase 2).
        `raw_response` no viaja en to_dict() — queda vacío acá, no se
        necesita para validación estática/dinámica (Fase 3).
        """
        return cls(
            files=[CodeFile.from_dict(f) for f in data.get("files", [])],
            dependencies=list(data.get("dependencies", [])),
            test_files=[CodeFile.from_dict(f) for f in data.get("test_files", [])],
            test_command=data.get("test_command"),
            expected_output=data.get("expected_output"),
            warnings=list(data.get("warnings", [])),
            complete=bool(data.get("complete", False)),
            missing=list(data.get("missing", [])),
        )


def _split_sections(response: str) -> Dict[str, str]:
    """
    Divide la respuesta cruda en segmentos de texto por encabezado.

    Returns:
        Dict con clave = nombre canónico de sección (ver
        `_SECTION_HEADERS`) y valor = texto entre ese encabezado y
        el siguiente (o el final de la respuesta). Las claves cuyo
        encabezado no aparece en la respuesta quedan ausentes del dict.
    """
    matches = list(_SECTION_SPLIT_RE.finditer(response))
    if not matches:
        return {}

    sections: Dict[str, str] = {}
    for i, m in enumerate(matches):
        header_text = m.group(0).strip().rstrip(":").strip().lower()
        canonical = None
        for name, alternatives in _SECTION_HEADERS.items():
            if any(re.fullmatch(alt, header_text, re.IGNORECASE) for alt in alternatives):
                canonical = name
                break
        if canonical is None:
            continue

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(response)
        sections[canonical] = response[start:end].strip()

    return sections


def _parse_dependencies(text: str) -> List[str]:
    """Extrae una lista de dependencias de líneas con bullet o coma-separadas."""
    if not text:
        return []
    deps: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        bullet_match = _BULLET_LINE_RE.match(line)
        candidate = bullet_match.group(1) if bullet_match else line
        for part in candidate.split(","):
            part = part.strip().strip("`")
            if part:
                deps.append(part)
    return deps


def _parse_notes(text: str) -> List[str]:
    """Extrae advertencias/supuestos/limitaciones como lista de líneas."""
    if not text:
        return []
    notes: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        bullet_match = _BULLET_LINE_RE.match(line)
        notes.append(bullet_match.group(1) if bullet_match else line)
    return notes


def _parse_command(text: str) -> Optional[str]:
    """Extrae el comando de validación — primera línea no vacía, sin backticks."""
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        inline_match = _INLINE_CODE_RE.match(line)
        return inline_match.group(1) if inline_match else line
    return None


def _strip_outer_fence(text: Optional[str]) -> Optional[str]:
    """✅ v0.6.9a (C): quita fences ``` alrededor de secciones de texto del
    contrato (ej. el modelo envuelve SALIDA ESPERADA en ```plaintext)."""
    if not text:
        return text
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if len(lines) <= 1:
            return ""
        body = "\n".join(lines[1:])
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        return body.strip()
    return text


def _code_blocks_to_files(text: str, query: str = "", is_test: bool = False) -> List[CodeFile]:
    """
    Convierte bloques ```lang fenced de un segmento a CodeFile.

    Reusa `extract_code()` (coder_core.py, ya probado) en vez de
    duplicar la extracción de bloques markdown. El nombre de archivo
    sale de un `ARCHIVO:` inmediatamente antes del bloque si existe
    (vía `extract_filename()`, mismo patrón que ya usa CoderAgent),
    o de `suggest_filename()` como fallback — con prefijo `test_`
    cuando `is_test=True` y no hubo `ARCHIVO:` explícito, para no
    colisionar con el nombre del archivo principal (mismo `query`
    en ambas llamadas produce el mismo `suggest_filename()`).
    """
    if not text:
        return []
    blocks = extract_code(text)
    files: List[CodeFile] = []
    search_from = 0
    for block in blocks:
        block_start = text.find(block["code"], search_from)
        preceding = text[:block_start] if block_start != -1 else text
        explicit_name = extract_filename(preceding)
        if explicit_name:
            name = explicit_name
        else:
            name = suggest_filename(query, [block])
            if is_test and not name.lower().startswith("test_"):
                name = f"test_{name}"
        files.append(CodeFile(name=name, content=block["code"], language=block["language"]))
        if block_start != -1:
            search_from = block_start + len(block["code"])
    return files


def parse_code_contract(response: str, settings: Any, query: str = "") -> CodeContract:
    """
    Parsea la respuesta cruda del LLM como CodeContract.

    Determina `missing`/`complete` cruzando las secciones encontradas
    contra los flags `code_require_tests`/`code_require_dependencies`/
    `code_require_commands`/`code_require_expected_output` de settings
    — una sección ausente solo cuenta como faltante si el flag
    correspondiente la exige. `files` (la sección CÓDIGO) siempre es
    obligatoria: sin código no hay contrato posible.

    Args:
        response: Contenido crudo devuelto por el LLM.
        settings: Instancia de Settings (para los flags code_require_*).
        query: Consulta original del usuario, para suggest_filename().

    Returns:
        CodeContract con `complete`/`missing` resueltos.
    """
    sections = _split_sections(response)

    code_text = sections.get("codigo", response if not sections else "")
    files = _code_blocks_to_files(code_text, query)

    dependencies = _parse_dependencies(sections.get("dependencias", ""))
    test_files = _code_blocks_to_files(sections.get("tests", ""), query, is_test=True)
    test_command = _parse_command(sections.get("comando", ""))
    expected_output = _strip_outer_fence(sections.get("salida_esperada") or None)
    warnings = _parse_notes(sections.get("advertencias", ""))

    missing: List[str] = []
    if not files:
        missing.append("codigo")
    if getattr(settings, "code_require_dependencies", False) and not dependencies:
        missing.append("dependencias")
    if getattr(settings, "code_require_tests", False) and not test_files:
        missing.append("tests")
    if getattr(settings, "code_require_commands", False) and not test_command:
        missing.append("comando")
    if getattr(settings, "code_require_expected_output", False) and not expected_output:
        missing.append("salida_esperada")

    contract = CodeContract(
        files=files,
        dependencies=dependencies,
        test_files=test_files,
        test_command=test_command,
        expected_output=expected_output,
        warnings=warnings,
        complete=not missing,
        missing=missing,
        raw_response=response,
    )

    if missing:
        logger.debug(f"CodeContract incompleto — faltan secciones: {missing}")

    return contract


def build_contract_critique(contract: CodeContract) -> str:
    """
    Construye una crítica en español para el retry (Fase 4), a partir
    de las secciones faltantes de un CodeContract incompleto.
    """
    if not contract.missing:
        return ""

    _labels = {
        "codigo":           "el bloque de código (```python ... ```)",
        "dependencias":     "la sección DEPENDENCIAS: con las librerías usadas",
        "tests":            "la sección TESTS: con al menos un test",
        "comando":          "la sección COMANDO: con el comando para correr los tests",
        "salida_esperada":  "la sección SALIDA ESPERADA: con el resultado esperado",
    }
    faltantes = ", ".join(_labels.get(m, m) for m in contract.missing)
    return f"Faltó incluir: {faltantes}. Repetí la respuesta completando esas secciones."
