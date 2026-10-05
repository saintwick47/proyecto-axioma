#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — rac_static.py
# Ruta: src/agents/rac_static.py
#
# ✅ v0.6.9w (FIX RAC): Validación estática previa + segmentación
#   sintácticamente íntegra + filtro anti-alucinación para la RAC.
#
#   CAUSA RAÍZ de los falsos positivos de la RAC Parcial del 2026-09-13:
#     1. `f.read(1500)` cortaba el archivo a 1500 caracteres, así que el LLM
#        recibía clases abiertas e imports partidos y reportaba "clase sin
#        cerrar", "import truncado", "función incompleta" — todo falso.
#     2. No había validación estática: el LLM opinaba sobre sintaxis.
#     3. `max_tokens=300` cortaba la propia respuesta del LLM a media frase.
#
#   Este módulo NO usa el LLM: es determinista y sin dependencias externas
#   (solo stdlib: `ast`, `json`, `re`).
#     - `validar_sintaxis()`      → ast.parse / json.loads → veredicto real.
#     - `construir_unidades()`    → chunks cortados en fronteras de nodos AST
#                                   (nunca a mitad de una clase/import).
#     - `esqueleto_de_nodo()`     → firmas + docstrings para archivos enormes.
#     - `filtrar_alucinaciones()` → descarta afirmaciones refutadas por la
#                                   evidencia estática (sintaxis OK / símbolo
#                                   que sí existe).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging

import ast
import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

__all__ = [
    "LIMITE_LECTURA_COMPLETA",
    "MAX_CHUNK_CHARS",
    "MAX_CHUNKS_POR_ARCHIVO",
    "DiagnosticoEstatico",
    "UnidadAnalisis",
    "leer_fuente",
    "validar_sintaxis",
    "extraer_simbolos",
    "esqueleto_de_nodo",
    "dividir_en_chunks",
    "construir_unidades",
    "analizar_archivo",
    "bloque_estatico_prompt",
    "construir_prompt_rac",
    "filtrar_alucinaciones",
    "PATRONES_SINTAXIS",
    "PATRONES_AUSENCIA",
]

# ── Límites (caracteres) ─────────────────────────────────────────────────────
#: Máximo de caracteres leídos del archivo. Es un seguro anti-patológico
#: (400 KB ≈ 10.000 líneas), NO el límite de la RAC anterior (1500 chars).
LIMITE_LECTURA_COMPLETA = 400_000
#: Tamaño objetivo de cada unidad enviada al LLM.
MAX_CHUNK_CHARS = 7_000
#: Máximo de unidades por archivo (acota el coste temporal de la RAC).
MAX_CHUNKS_POR_ARCHIVO = 4
#: Por encima de esto, un nodo se envía como esqueleto (firmas) en vez de código.
UMBRAL_ESQUELETO_CHARS = 9_000

_PY = ".py"


# ═══════════════════════════════════════════════════════════════
# TIPOS
# ═══════════════════════════════════════════════════════════════
@dataclass
class UnidadAnalisis:
    """Fragmento de archivo enviado al LLM en una llamada.

    Attributes:
        indice: 1-based, para el encabezado del reporte.
        total: total de unidades del archivo.
        linea_inicio / linea_fin: rango de líneas cubierto (1-based).
        texto: contenido a enviar (código completo, chunk o esqueleto).
        modo: "completo" (archivo entero), "intacto" (chunk en frontera AST),
              "esqueleto" (solo firmas) o "fragmento" (corte textual explícito).
    """

    indice: int
    total: int
    linea_inicio: int
    linea_fin: int
    texto: str
    modo: str = "completo"

    @property
    def es_fragmento(self) -> bool:
        """True si el texto NO representa el archivo completo."""
        return self.modo != "completo" or self.total > 1


@dataclass
class DiagnosticoEstatico:
    """Resultado determinista del pre-análisis de un archivo."""

    ruta: str
    extension: str
    total_lineas: int = 0
    total_chars: int = 0
    verificado: bool = False          # True si se pudo validar sintaxis/estructura
    sintaxis_ok: bool = False
    error_sintaxis: Optional[str] = None
    linea_error: Optional[int] = None
    truncado_lectura: bool = False
    simbolos: Set[str] = field(default_factory=set)
    unidades: List[UnidadAnalisis] = field(default_factory=list)

    @property
    def es_python(self) -> bool:
        return self.extension == _PY

    @property
    def analizable_por_llm(self) -> bool:
        """El LLM solo analiza si la sintaxis es válida (o no aplica)."""
        return (not self.es_python) or (self.verificado and self.sintaxis_ok)

    def resumen(self) -> str:
        """Línea corta de estado para el reporte."""
        if not self.es_python:
            return f"estructura OK ({self.total_lineas} líneas, sin parser de sintaxis)"
        if not self.verificado:
            return "sintaxis NO verificable"
        if self.sintaxis_ok:
            return "sintaxis VERIFICADA con ast (0 errores)"
        return f"ERROR DE SINTAXIS REAL en línea {self.linea_error}"


# ═══════════════════════════════════════════════════════════════
# LECTURA Y VALIDACIÓN
# ═══════════════════════════════════════════════════════════════
def leer_fuente(file_path: str | Path) -> Tuple[str, bool]:
    """Lee el archivo COMPLETO (a diferencia del antiguo `read(1500)`).

    Args:
        file_path: Ruta del archivo.

    Returns:
        (contenido, truncado_por_limite_seguridad)
    """
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read(LIMITE_LECTURA_COMPLETA + 1)
    truncado = len(data) > LIMITE_LECTURA_COMPLETA
    return (data[:LIMITE_LECTURA_COMPLETA] if truncado else data), truncado


def _validar_json(source: str) -> Tuple[bool, Optional[str], Optional[int]]:
    try:
        json.loads(source)
        return True, None, None
    except json.JSONDecodeError as e:
        return False, f"JSONDecodeError: {e.msg}", e.lineno


def validar_sintaxis(
    source: str, extension: str = _PY
) -> Tuple[bool, bool, Optional[str], Optional[int]]:
    """Valida la sintaxis ANTES de llamar al LLM.

    Returns:
        (verificado, sintaxis_ok, error, linea_error)
    """
    if extension == ".json":
        ok, err, linea = _validar_json(source)
        return True, ok, err, linea
    if extension != _PY:
        return False, True, None, None
    try:
        ast.parse(source)
        return True, True, None, None
    except SyntaxError as e:
        detalle = f"{type(e).__name__}: {e.msg}"
        return True, False, detalle, e.lineno
    except ValueError as e:  # null bytes, etc.
        return True, False, f"ValueError: {e}", None


def extraer_simbolos(source: str) -> Set[str]:
    """Extrae todos los nombres definidos/importados (para refutar ausencias)."""
    simbolos: Set[str] = set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        log_degraded(logger, e, "src/agents/rac_static.py:extraer_simbolos")
        return simbolos

    for nodo in ast.walk(tree):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            simbolos.add(nodo.name)
        elif isinstance(nodo, ast.Name) and isinstance(nodo.ctx, ast.Store):
            simbolos.add(nodo.id)
        elif isinstance(nodo, ast.arg):
            simbolos.add(nodo.arg)
        elif isinstance(nodo, ast.Attribute):
            simbolos.add(nodo.attr)
        elif isinstance(nodo, ast.alias):
            simbolos.add((nodo.asname or nodo.name).split(".")[0])
    return simbolos


# ═══════════════════════════════════════════════════════════════
# ESQUELETO (archivos enormes)
# ═══════════════════════════════════════════════════════════════
def esqueleto_de_nodo(nodo: ast.AST) -> str:
    """Devuelve firmas + docstrings de un nodo (cuerpos → `...`).

    Sirve para archivos gigantes (p. ej. `dispatcher_process.py`, 1268 líneas):
    el LLM recibe la estructura COMPLETA en lugar de un trozo truncado.
    """
    copia = copy.deepcopy(nodo)
    for sub in ast.walk(copia):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(sub, clean=False)
            cuerpo: List[ast.stmt] = []
            if doc:
                cuerpo.append(ast.Expr(value=ast.Constant(value=doc)))
            cuerpo.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
            sub.body = cuerpo
    try:
        return ast.unparse(copia)
    except Exception as e:  # pragma: no cover — ast.unparse es estable, seguro defensivo
        log_degraded(logger, e, "src/agents/rac_static.py:esqueleto_de_nodo")
        return ast.dump(copia, annotate_fields=False)


# ═══════════════════════════════════════════════════════════════
# SEGMENTACIÓN EN FRONTERAS AST
# ═══════════════════════════════════════════════════════════════
def _rangos_top_level(
    tree: ast.Module, total_lineas: int
) -> List[Tuple[int, int, Optional[int]]]:
    """Rangos (inicio, fin, índice_de_nodo) por sentencia de nivel superior.

    El `inicio` es la línea siguiente al nodo anterior, de modo que los
    comentarios y decoradores quedan dentro del rango del nodo que los sigue.
    El índice es `None` para el rango de cola (código tras el último nodo).
    """
    rangos: List[Tuple[int, int, Optional[int]]] = []
    cursor = 1
    for idx, nodo in enumerate(tree.body):
        inicio = max(cursor, getattr(nodo, "lineno", cursor))
        fin = max(inicio, getattr(nodo, "end_lineno", inicio))
        rangos.append((cursor, fin, idx))
        cursor = fin + 1
    if cursor <= total_lineas:
        rangos.append((cursor, total_lineas, None))
    return rangos


def _texto_lineas(lineas: List[str], inicio: int, fin: int) -> str:
    return "".join(lineas[max(0, inicio - 1):fin])


def _es_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _sub_bloques_de_clase(
    lineas: List[str],
    clase: ast.ClassDef,
    max_chars: int,
    max_grupos: int,
    inicio_grupo: Optional[int] = None,
) -> List[Tuple[int, int, str, str]]:
    """Expande una clase gigante en sub-bloques de métodos (todos válidos).

    Cada sub-bloque repite la cabecera de la clase (`class X(...):` + docstring)
    y agrupa métodos completos, de modo que el texto enviado siempre parsea y el
    LLM ve código real en lugar de un esqueleto vacío.

    Args:
        lineas: Líneas del archivo (con salto incluido).
        clase: Nodo `ClassDef` a expandir.
        max_chars: Presupuesto de caracteres por sub-bloque.
        max_grupos: Número máximo de sub-bloques.
        inicio_grupo: Primera línea del grupo original (incluye decoradores y
            comentarios previos a la clase, que si no se perderían).

    Returns:
        Lista de (linea_inicio, linea_fin, texto, modo). Vacía si no se expande.
    """
    cuerpo = list(clase.body)
    if not cuerpo:
        return []

    inicio_cabecera = clase.lineno if inicio_grupo is None else min(inicio_grupo, clase.lineno)
    cabecera_fin = cuerpo[0].end_lineno if _es_docstring(cuerpo[0]) else clase.lineno - 1
    cabecera = _texto_lineas(lineas, inicio_cabecera, cabecera_fin)
    resto = cuerpo[1:] if _es_docstring(cuerpo[0]) else cuerpo
    if not resto:
        return []

    # Agrupar métodos por presupuesto; un método que no cabe solo se resume.
    # El rango de cada método arranca en la línea siguiente al anterior, de modo
    # que los comentarios y decoradores intermedios no se pierden.
    grupos: List[Dict[str, Any]] = []
    cursor = cabecera_fin + 1
    for stmt in resto:
        inicio = cursor  # incluye comentarios/decoradores previos al método
        fin = getattr(stmt, "end_lineno", inicio)
        texto = _texto_lineas(lineas, inicio, fin)
        cursor = fin + 1
        omitido = False
        if len(texto) > max_chars:
            texto = (
                f"# [RAC] cuerpo omitido ({fin - inicio + 1} líneas) — revisar solo la firma:\n"
                + esqueleto_de_nodo(stmt)
                + "\n"
            )
            omitido = True
        if grupos:
            ultimo = grupos[-1]
            if len(cabecera) + sum(len(t) for t in ultimo["textos"]) + len(texto) <= max_chars:
                ultimo["fin"] = fin
                ultimo["textos"].append(texto)
                ultimo["stmts"].append(stmt)
                ultimo["omitido"] = ultimo["omitido"] or omitido
                continue
        grupos.append(
            {"inicio": inicio, "fin": fin, "textos": [texto], "stmts": [stmt], "omitido": omitido}
        )

    # Respetar el presupuesto de bloques: el sobrante se envía como resumen.
    if len(grupos) > max_grupos:
        indice_ultimo = sum(len(g["stmts"]) for g in grupos[: max_grupos - 1])
        sobrantes = resto[indice_ultimo + len(grupos[max_grupos - 1]["stmts"]):]
        cola = "".join(
            f"# [RAC] resto de la clase (resumen):\n{esqueleto_de_nodo(s)}\n"
            for s in sobrantes
            if isinstance(s, ast.stmt)
        )
        ultimo = grupos[max_grupos - 1]
        if cola:
            ultimo["textos"].append(cola)
            ultimo["omitido"] = True
        fin_total = grupos[-1]["fin"]
        grupos = grupos[:max_grupos]
        grupos[-1]["fin"] = fin_total

    sub: List[Tuple[int, int, str, str]] = []
    for pos, grupo in enumerate(grupos):
        texto = cabecera + "".join(grupo["textos"])
        try:
            ast.parse(texto)
        except (SyntaxError, ValueError) as e:
            log_degraded(logger, e, "src/agents/rac_static.py:_sub_bloques_de_clase")
            return []
        sub.append(
            (
                # El primer sub-bloque arranca en la cabecera enviada (incluye
                # decoradores/comentarios previos a la clase).
                int(inicio_cabecera) if pos == 0 else int(grupo["inicio"]),
                int(grupo["fin"]),
                texto,
                "mixto" if grupo["omitido"] else "intacto",
            )
        )
    return sub


def dividir_en_chunks(
    source: str,
    max_chars: int = MAX_CHUNK_CHARS,
    max_chunks: int = MAX_CHUNKS_POR_ARCHIVO,
) -> List[UnidadAnalisis]:
    """Divide código Python en chunks que SIEMPRE parsean (fronteras de nodos).

    Los comentarios y decoradores quedan dentro del chunk de la sentencia que
    sigue, de modo que ningún chunk empieza a mitad de una construcción.
    Las clases gigantes se expanden en sub-bloques de métodos válidos; las
    funciones gigantes se envían como esqueleto (firmas + docstring).

    Args:
        source: Código fuente completo.
        max_chars: Presupuesto de caracteres por chunk.
        max_chunks: Número máximo de chunks (se agrupan si hacen falta menos).

    Returns:
        Lista de UnidadAnalisis (modo "intacto", "mixto" o "esqueleto").
    """
    lineas = source.splitlines(keepends=True)
    total_lineas = len(lineas)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        log_degraded(logger, e, "src/agents/rac_static.py:dividir_en_chunks")
        return []

    rangos = _rangos_top_level(tree, total_lineas)
    if not rangos:
        return []

    # Agrupar rangos consecutivos respetando el presupuesto de caracteres.
    grupos: List[Dict[str, Any]] = []
    actual: Optional[Dict[str, Any]] = None
    for inicio, fin, idx_nodo in rangos:
        if actual is None:
            actual = {"inicio": inicio, "fin": fin, "idx": [] if idx_nodo is None else [idx_nodo]}
            continue
        if len(_texto_lineas(lineas, actual["inicio"], fin)) <= max_chars:
            actual["fin"] = fin
            if idx_nodo is not None:
                actual["idx"].append(idx_nodo)
        else:
            grupos.append(actual)
            actual = {"inicio": inicio, "fin": fin, "idx": [] if idx_nodo is None else [idx_nodo]}
    if actual is not None:
        grupos.append(actual)

    entradas: List[Dict[str, Any]] = []
    for pos, grupo in enumerate(grupos):
        inicio, fin = grupo["inicio"], grupo["fin"]
        nodos = [tree.body[i] for i in grupo["idx"]]
        texto = _texto_lineas(lineas, inicio, fin)
        modo = "intacto"
        if len(texto) > UMBRAL_ESQUELETO_CHARS:
            pendientes = len(grupos) - pos - 1
            restante = max(1, max_chunks - len(entradas) - pendientes)
            if len(nodos) == 1 and isinstance(nodos[0], ast.ClassDef):
                sub = _sub_bloques_de_clase(
                    lineas, nodos[0], max_chars, restante, inicio_grupo=inicio
                )
                if sub:
                    for s_inicio, s_fin, s_texto, s_modo in sub:
                        entradas.append(
                            {
                                "inicio": s_inicio,
                                "fin": s_fin,
                                "texto": s_texto,
                                "modo": s_modo,
                                "nodos": [nodos[0]],
                            }
                        )
                    continue
            if nodos:
                texto = "\n\n".join(esqueleto_de_nodo(n) for n in nodos)
                modo = "esqueleto"
        entradas.append(
            {"inicio": inicio, "fin": fin, "texto": texto, "modo": modo, "nodos": nodos}
        )

    # Si aun sobran unidades, fusionar la cola en un único bloque de estructura
    # (resumen por firmas) en lugar de concatenar código sin presupuesto.
    while len(entradas) > max_chunks:
        ultima = entradas.pop()
        previa = entradas.pop()
        nodos = previa["nodos"] + [n for n in ultima["nodos"] if n not in previa["nodos"]]
        entradas.append(
            {
                "inicio": previa["inicio"],
                "fin": ultima["fin"],
                "texto": "\n\n".join(esqueleto_de_nodo(n) for n in nodos),
                "modo": "esqueleto",
                "nodos": nodos,
            }
        )

    return [
        UnidadAnalisis(
            indice=idx,
            total=len(entradas),
            linea_inicio=e["inicio"],
            linea_fin=e["fin"],
            texto=e["texto"],
            modo=e["modo"],
        )
        for idx, e in enumerate(entradas, start=1)
    ]


def _chunks_textuales(
    source: str, max_chars: int = MAX_CHUNK_CHARS, max_chunks: int = MAX_CHUNKS_POR_ARCHIVO
) -> List[UnidadAnalisis]:
    """Corte por líneas para archivos sin parser (md/txt/yaml/json gigantes).

    El modo es "fragmento" y el prompt avisa explícitamente de ello para que el
    LLM no confunda un corte del pipeline con un defecto del archivo.
    """
    lineas = source.splitlines(keepends=True)
    total_lineas = len(lineas)
    if total_lineas == 0:
        return [UnidadAnalisis(1, 1, 1, 1, "", "fragmento")]

    # Repartir en como mucho `max_chunks` bloques equilibrados.
    n = max(1, min(max_chunks, max(1, -(-len(source) // max_chars))))
    por_bloque = max(1, -(-total_lineas // n))
    unidades: List[UnidadAnalisis] = []
    inicio = 1
    while inicio <= total_lineas:
        fin = min(total_lineas, inicio + por_bloque - 1)
        unidades.append(
            UnidadAnalisis(
                indice=len(unidades) + 1,
                total=0,  # se rellena al final
                linea_inicio=inicio,
                linea_fin=fin,
                texto="".join(lineas[inicio - 1:fin]),
                modo="fragmento",
            )
        )
        inicio = fin + 1
    for u in unidades:
        u.total = len(unidades)
    return unidades


def construir_unidades(source: str, diagnostico: "DiagnosticoEstatico") -> List[UnidadAnalisis]:
    """Decide cómo enviar el archivo al LLM (completo, chunks AST o fragmentos)."""
    if len(source) <= MAX_CHUNK_CHARS:
        return [
            UnidadAnalisis(
                indice=1,
                total=1,
                linea_inicio=1,
                linea_fin=max(1, diagnostico.total_lineas),
                texto=source,
                modo="completo",
            )
        ]
    if diagnostico.es_python and diagnostico.sintaxis_ok:
        unidades = dividir_en_chunks(source)
        if unidades:
            return unidades
    return _chunks_textuales(source)


def analizar_archivo(
    file_path: str | Path, source: Optional[str] = None, truncado: bool = False
) -> DiagnosticoEstatico:
    """Pre-análisis determinista completo de un archivo (sin LLM).

    Args:
        file_path: Ruta del archivo.
        source: Contenido ya leído (si es None se lee del disco).
        truncado: True si `source` ya venía recortado por `LIMITE_LECTURA_COMPLETA`.

    Returns:
        DiagnosticoEstatico con sintaxis, símbolos y unidades listas para el LLM.
    """
    ruta = Path(file_path)
    extension = ruta.suffix.lower()
    if source is None:
        source, truncado = leer_fuente(ruta)
    elif len(source) > LIMITE_LECTURA_COMPLETA:
        truncado = True
        source = source[:LIMITE_LECTURA_COMPLETA]

    diag = DiagnosticoEstatico(
        ruta=str(ruta),
        extension=extension,
        total_lineas=source.count("\n") + (0 if source.endswith("\n") else 1),
        total_chars=len(source),
        truncado_lectura=truncado,
    )
    verificado, ok, error, linea = validar_sintaxis(source, extension)
    diag.verificado = verificado
    diag.sintaxis_ok = ok
    diag.error_sintaxis = error
    diag.linea_error = linea
    if diag.es_python and ok:
        diag.simbolos = extraer_simbolos(source)
    diag.unidades = construir_unidades(source, diag)
    return diag


# ═══════════════════════════════════════════════════════════════
# PROMPT (anti-alucinación)
# ═══════════════════════════════════════════════════════════════
MODO_DESCRIPCION = {
    "completo": "el archivo COMPLETO, de la primera a la última línea",
    "intacto": "un bloque COMPLETO y sintácticamente íntegro del archivo (cortado en fronteras de funciones/clases; "
               "puede repetir la cabecera `class ...:` de la clase a la que pertenece el bloque)",
    "mixto": "un bloque sintácticamente íntegro del archivo; los métodos marcados con "
             "`# [RAC] cuerpo omitido` se envían solo como firma porque superan el presupuesto de la RAC, "
             "y puede repetirse la cabecera `class ...:` de la clase contenedora",
    "esqueleto": "la ESTRUCTURA COMPLETA del archivo (firmas, clases, métodos y docstrings); los cuerpos se omiten con `...` de forma deliberada",
    "fragmento": "una PARTE del archivo (fichero de texto sin estructura de código)",
}


def bloque_estatico_prompt(diag: DiagnosticoEstatico) -> str:
    """Bloque de evidencia determinista que se inyecta en el prompt."""
    lineas = [
        "## VERIFICACIÓN ESTÁTICA PREVIA (determinista, hecha con `ast` antes de tu análisis)",
        f"- Archivo: {Path(diag.ruta).name} ({diag.total_lineas} líneas, {diag.total_chars} caracteres)",
    ]
    if diag.es_python and diag.verificado:
        if diag.sintaxis_ok:
            lineas.append(
                "- `ast.parse()`: SINTAXIS VÁLIDA — 0 errores, clases cerradas, "
                "imports completos y paréntesis balanceados."
            )
            lineas.append(
                f"- Símbolos detectados (definiciones, métodos e imports): {len(diag.simbolos)}"
            )
        else:
            lineas.append(f"- `ast.parse()`: ERROR REAL → {diag.error_sintaxis} (línea {diag.linea_error})")
    elif diag.extension == ".json" and diag.verificado:
        lineas.append(
            "- `json.loads()`: VÁLIDO" if diag.sintaxis_ok else f"- `json.loads()`: ERROR REAL → {diag.error_sintaxis}"
        )
    else:
        lineas.append("- Sin parser aplicable: NO afirmes nada sobre sintaxis o cierre de bloques.")

    if diag.simbolos:
        muestra = sorted(diag.simbolos)[:80]
        lineas.append(f"- Símbolos (primeros {len(muestra)}): {', '.join(muestra)}")

    lineas += [
        "",
        "REGLAS OBLIGATORIAS (incumplirlas invalida tu informe):",
        "1. PROHIBIDO reportar errores de sintaxis, clases/bloques sin cerrar, imports truncados, "
        "código incompleto o líneas cortadas: la verificación estática de arriba es la única autoridad. "
        "Si crees ver una construcción abierta, es porque el bloque enviado es parcial (ver MODO DE ENVÍO). "
        "Tampoco reportes como error las marcas `# [RAC] cuerpo omitido` ni los `...` del esqueleto: "
        "son omisiones del pipeline, no defectos del código. Sí puedes señalar como problema de "
        "MANTENIBILIDAD que un método supere cientos de líneas cuando esa marca lo indique.",
        "2. PROHIBIDO afirmar que un método, función, clase, atributo o import «no existe» o «falta» "
        "si aparece en la lista de símbolos o si lo estás viendo en el código enviado.",
        "3. Cada hallazgo debe citar `archivo:línea` y copiar literalmente el código implicado. "
        "Si no puedes citar la línea exacta, no lo reportes.",
        "4. Solo cuentan defectos de LÓGICA, CONCURRENCIA, SEGURIDAD, RENDIMIENTO o MANTENIBILIDAD.",
        "5. Máximo 6 hallazgos por bloque, ordenados por severidad (CRÍTICO/ALTO/MEDIO/BAJO). Sé conciso: "
        "una frase de evidencia + una de recomendación por hallazgo. Si no hay defectos reales, escribe "
        "exactamente `SIN HALLAZGOS RELEVANTES`.",
        "6. ANTES de responder `SIN HALLAZGOS RELEVANTES`, comprueba explícitamente estos cinco puntos y "
        "reporta lo que encuentres: (a) manejo de excepciones (silenciadas, demasiado amplias, sin log), "
        "(b) concurrencia (locks, `await` bajo lock, estado compartido, `time.sleep` en async), "
        "(c) validación de entradas y rutas de error, (d) métodos o funciones desproporcionadamente largos "
        "o con complejidad alta, (e) duplicación de lógica o de definiciones entre módulos.",
    ]
    return "\n".join(lineas)


def construir_prompt_rac(
    nombre_archivo: str,
    unidad: UnidadAnalisis,
    diag: DiagnosticoEstatico,
) -> str:
    """Construye el prompt de un bloque, con el modo de envío declarado."""
    descripcion = MODO_DESCRIPCION.get(unidad.modo, MODO_DESCRIPCION["completo"])
    lenguaje = nombre_archivo.rsplit(".", 1)[-1] if "." in nombre_archivo else "text"
    cabecera = [
        f"Analiza `{nombre_archivo}` (líneas {unidad.linea_inicio}-{unidad.linea_fin}).",
        f"MODO DE ENVÍO: {descripcion}."
        + (
            f" Este es el bloque {unidad.indice} de {unidad.total}."
            if unidad.total > 1
            else ""
        ),
        "",
        bloque_estatico_prompt(diag) if unidad.indice == 1 else
        "Recuerda: la verificación estática previa confirmó la sintaxis; no reportes cortes de código.",
        "",
        "### CÓDIGO",
        f"```{lenguaje}",
        unidad.texto,
        "```",
    ]
    return "\n".join(cabecera)


# ═══════════════════════════════════════════════════════════════
# FILTRO ANTI-ALUCINACIÓN (post-LLM)
# ═══════════════════════════════════════════════════════════════
PATRONES_SINTAXIS = re.compile(
    r"sin\s+cerrar|no\s+se\s+cierra|no\s+cierra\s+correctamente|"
    r"truncad\w*|cortad\w*\s+(?:a\s+mitad|abruptamente)|"
    r"incomplet\w*|no\s+est[áa]\s+complet\w*|falta\s+(?:el\s+)?cierre|"
    r"syntaxerror|no\s+compila|indentaci[óo]n\s+incorrecta|sangr[íi]a\s+incorrecta",
    re.IGNORECASE,
)
PATRONES_AUSENCIA = re.compile(
    r"no\s+existe|no\s+est[áa]n?\s+definid\w*|no\s+se\s+encuentra|"
    r"falta\s+(?:el\s+|la\s+|los\s+|las\s+)?(?:m[ée]todo|funci[óo]n|clase|atributo|variable|import|m[óo]dulo)|"
    r"ausencia\s+de|no\s+hay\s+(?:un\s+|una\s+)?(?:m[ée]todo|funci[óo]n|clase|atributo)|"
    r"no\s+implementad\w*|m[ée]todo\s+inexistente",
    re.IGNORECASE,
)
_PATRON_IDENT = re.compile(
    r"`{1,3}([A-Za-z_][A-Za-z0-9_]*)`{1,3}"                       # `reserve`
    r"|\b([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)"                      # reserve()
    r"|\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"      # memory_guard.reserve
    r"|\b([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b"                  # snake_case sin comillas
)


def _identificadores(linea: str) -> Set[str]:
    encontrados: Set[str] = set()
    for m in _PATRON_IDENT.finditer(linea):
        for grupo in m.groups():
            if grupo:
                encontrados.add(grupo)
    return encontrados


def _sangria(linea: str) -> int:
    return len(linea) - len(linea.lstrip())


def filtrar_alucinaciones(
    texto: str, diag: DiagnosticoEstatico
) -> Tuple[str, List[str]]:
    """Descarta afirmaciones refutadas por la evidencia estática.

    Args:
        texto: Respuesta cruda del LLM.
        diag: Diagnóstico estático del archivo analizado.

    Returns:
        (texto_filtrado, motivos_descartados) — los motivos van al pie del
        reporte para dejar traza de la limpieza.
    """
    if not texto:
        return texto, []

    sintaxis_verificada = diag.es_python and diag.verificado and diag.sintaxis_ok
    if not sintaxis_verificada and not diag.simbolos:
        return texto, []

    lineas = texto.splitlines()
    conservadas: List[str] = []
    descartes: List[str] = []
    saltar_hasta_sangria: Optional[int] = None

    for linea in lineas:
        sangria = _sangria(linea)
        if saltar_hasta_sangria is not None:
            if linea.strip() == "" or sangria > saltar_hasta_sangria:
                continue
            saltar_hasta_sangria = None

        motivo: Optional[str] = None
        if sintaxis_verificada and PATRONES_SINTAXIS.search(linea):
            motivo = "falso positivo de sintaxis (ast.parse confirmó 0 errores)"
        elif PATRONES_AUSENCIA.search(linea):
            for ident in _identificadores(linea):
                if ident in diag.simbolos:
                    motivo = f"falso positivo de ausencia: `{ident}` SÍ está definido en el archivo"
                    break

        if motivo is None:
            conservadas.append(linea)
        else:
            descartes.append(f"{linea.strip()[:160]} — {motivo}")
            saltar_hasta_sangria = sangria

    limpio = "\n".join(conservadas).strip()
    if not limpio:
        limpio = "SIN HALLAZGOS RELEVANTES"
    return limpio, descartes


def resumen_ejecucion(diagnosticos: Sequence[DiagnosticoEstatico]) -> Dict[str, Any]:
    """Métricas agregadas del pre-análisis, para la cabecera del reporte."""
    python = [d for d in diagnosticos if d.es_python]
    return {
        "archivos": len(diagnosticos),
        "python_verificados": sum(1 for d in python if d.verificado and d.sintaxis_ok),
        "errores_sintaxis": [d for d in python if d.verificado and not d.sintaxis_ok],
        "total_lineas": sum(d.total_lineas for d in diagnosticos),
    }
