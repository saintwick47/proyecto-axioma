#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — dispatcher_handlers_files.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/core/dispatcher_handlers_files.py
#
# ✅ REFACTOR: mitad de ARCHIVOS/CARPETAS extraída de
#   src/core/dispatcher_handlers.py (1123 líneas) para dejarlo por debajo de
#   las 1000 (regla del proyecto: cada archivo >1000 líneas se parte en DOS:
#   el original conserva una mitad y este hermano recibe la otra).
#
#   Qué se movió, SIN cambios de comportamiento:
#     · Helpers de logging compartidos por las dos mitades: `_get_caller_info`,
#       `_log_event`, `_log_error`, `_log_router_decision`,
#       `_log_agent_execution`, `_log_model_call`. Viven acá para que la
#       dependencia sea UNIDIRECCIONAL (dispatcher_handlers → este módulo):
#       si quedaran en el original, este módulo tendría que importarlos de
#       vuelta y se crearía un ciclo (AUD-CIR-001).
#     · Detección de carpeta/fs_*: constantes `_FOLDER_HINTS`, `_FOLDER_VERBS`,
#       `_FS_TOOL_TOKENS`, `_FS_CONTENT_VERBS` y funciones
#       `_is_folder_analysis_query`, `_extract_folder_candidates`,
#       `_is_fs_tool_intent`, `_extract_content_keyword`. Las usan AMBAS
#       mitades (`_handle_file_search` quedó en el original), por eso son del
#       hermano y el original las re-importa.
#     · Métodos de `DispatcherHandlersMixin` → `DispatcherHandlersFilesMixin`:
#       `_handle_file_list_folder`, `_handle_file_content_search`,
#       `_handle_folder_analysis` (análisis de carpeta con grounding real).
#
#   Por qué la API pública NO cambia:
#     `DispatcherHandlersMixin` (en el original) hereda de
#     `DispatcherHandlersFilesMixin`, así que los tres métodos siguen respondiendo en
#     cualquier instancia de Dispatcher; y el original re-importa TODOS los
#     nombres movidos con los MISMOS nombres, de modo que
#     `from src.core.dispatcher_handlers import _log_event` (y los imports de
#     dispatcher_process*.py / dispatcher_handlers_extra*.py) siguen igual.
#
#   `Path(__file__).resolve().parents[2]` sigue apuntando a la raíz del repo:
#   este archivo vive en la MISMA carpeta (src/core/) que el original.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from pathlib import Path
from typing import Optional

from config.settings import settings
from src.domain.entities import Message, Response
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ✅ Mismo guard que el original (dueño de la feature: tools.detailed_logger).
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False


# ── Helpers de logging ───────────────────────────────────────────────────────

def _get_caller_info() -> dict:
    frame = inspect.currentframe()
    try:
        if frame and frame.f_back:
            return {
                "file": frame.f_back.f_code.co_filename,
                "line": frame.f_back.f_lineno,
                "function": frame.f_back.f_code.co_name,
            }
    finally:
        del frame
    return {}


def _log_event(category: str, message: str, level: str = "INFO", extra: dict = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category=category, message=message, level=level, extra=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_handlers.py:_log_event")


def _log_error(error: Exception, context: str, extra_info: dict = None):
    caller = _get_caller_info()
    if not LOGGER_AVAILABLE:
        logger.error(f"dispatcher - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(
                error=error,
                context=context,
                extra_info=extra_info,
                file_path=caller.get("file", ""),
                line_number=caller.get("line"),
                function_name=f"dispatcher.{context}",
            )
    except Exception:
        logger.error(f"dispatcher - {context}: {error}")


def _log_router_decision(
    query: str, task_type: str, handler: str,
    confidence: float, duration: float = None,
):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_router_decision(
                query=query, task_type=task_type, model_selected=handler,
                confidence=confidence, duration=duration,
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_handlers.py:_log_router_decision")


def _log_agent_execution(
    agent: str, task: str, state: str,
    duration: float = None, success: bool = True, error: str = None,
):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_agent_execution(
                agent_name=agent, task_type=task, state=state,
                duration=duration, success=success, error=error,
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_handlers.py:_log_agent_execution")


def _log_model_call(
    model: str, action: str, task_type: str,
    duration: float = None, success: bool = True,
    input_size: int = None, output_size: int = None,
):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_model_call(
                model_name=model, action=action, task_type=task_type,
                duration=duration, success=success,
                input_size=input_size, output_size=output_size,
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_handlers.py:_log_model_call")


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.8ii: análisis de carpeta/directorio con grounding
# (fix de logs/reg_error.txt: "analiza la carpeta X" no leía el FS
#  → general_chat/code_explanation divagaban sin leer archivos)
# ═══════════════════════════════════════════════════════════════
_FOLDER_HINTS = ("carpeta", "directorio", "folder")


_FOLDER_VERBS = (
    "analiz", "explor", "revis", "examin", "estudi", "inspeccion",
    "qué hace", "que hace", "qué hacen", "que hacen",
    "para qué sirve", "para que sirve", "cómo funciona", "como funciona",
    "qué es", "que es", "resum", "explica",
)


def _is_folder_analysis_query(query: str) -> bool:
    """True si el usuario pide analizar/entender UNA CARPETA (no un archivo).

    Requiere un verbo de análisis + (hint de carpeta | ruta tools/src/... |
    "sus archivos/módulos"). Así no pisa búsquedas tipo "buscá la carpeta X"
    (ubicación) ni "leé el archivo X".
    """
    if not query:
        return False
    q = query.lower()
    has_verb = any(v in q for v in _FOLDER_VERBS)
    if not has_verb:
        return False
    if any(h in q for h in _FOLDER_HINTS):
        return True
    if re.search(r"(?:^|\s)(?:tools|src|config|tests|docs|data)/[a-z0-9_./\-]+", q):
        return True
    if "sus archivos" in q or "sus módulos" in q or "sus modulos" in q:
        return True
    return False


def _extract_folder_candidates(query: str, root: "Path"):
    """
    Resuelve carpetas candidatas (Path existentes) para la consulta.

    Entiende: "la carpeta netron", "netron que se encuentra dentro de la
    carpeta tools" (padre explícito), rutas tipo "tools/netron" o
    "src/core". Devuelve lista de directorios existentes, ordenada por
    preferencia (padre explícito > raíz > tools/ > src/).
    """
    from pathlib import Path

    q = query.lower()
    names: list = []

    # 1) Nombre tras "carpeta/directorio/folder"
    m = re.search(
        r"(?:carpeta|directorio|folder)\s+(?:de\s+|llamada\s+|llamado\s+"
        r"|del\s+|el\s+|la\s+|con\s+nombre\s+)?([a-z0-9_./\-]+)",
        q,
    )
    if m:
        name = m.group(1).strip(" .,:;¿?")
        if name:
            names.append(name.split()[0] if name.split() else name)

    # 2) Ruta explícita (tools/..., src/...) sin palabra "carpeta"
    m2 = re.search(
        r"(?:^|\s)((?:tools|src|config|tests|docs|data|axioma)/[a-z0-9_./\-]+)",
        q,
    )
    if m2:
        names.append(m2.group(1).strip(" .,:;¿?"))

    # 3) Carpeta padre explícita: "... X que se encuentra dentro de la carpeta Y"
    parent = None
    mp = re.search(
        r"dentro\s+de\s+(?:la\s+|el\s+)?(?:carpeta|directorio|folder\s+)?"
        r"([a-z0-9_./\-]+)",
        q,
    )
    if mp:
        parent = mp.group(1).strip(" .,:;¿?") or None

    # 4) Fallback: nombre desnudo al final ("... sus archivos dentro de netron")
    if not names and parent:
        names.append(parent)
    if not names:
        m4 = re.search(r"(?:de|sobre)\s+([a-z0-9_./\-]{2,})$", q)
        if m4:
            names.append(m4.group(1).strip(" .,:;¿?"))

    root = Path(root)
    candidates: list = []
    for name in dict.fromkeys(names):
        if not name:
            continue
        p = Path(name)
        if p.is_absolute():
            if p.is_dir():
                candidates.append(p)
            continue
        tries = []
        if parent and parent not in name:
            tries.append(root / parent / name)
        tries += [root / name, root / "tools" / name, root / "src" / name]
        for t in tries:
            if t.is_dir():
                candidates.append(t)
                break
    return candidates


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.8pp (C): rutear intenciones fs_* / "archivos que hablan
# de X" a file_search con grounding (no a web_search/general_chat).
# ═══════════════════════════════════════════════════════════════
_FS_TOOL_TOKENS = ("fs_search", "fs_read", "fs_list", "fs_write", "fs_")


_FS_CONTENT_VERBS = (
    "hablan de", "habla de", "mencionan", "menciona", "mencionar",
    "contienen", "contiene", "que traten de", "que tratan de",
    "referencias a", "referencia a",
)


def _is_fs_tool_intent(query: str) -> bool:
    """True si el usuario nombra herramientas fs_* o pide archivos cuyo
    CONTENIDO mencione un tema (grep local, no web)."""
    if not query:
        return False
    q = query.lower()
    if any(tok in q for tok in _FS_TOOL_TOKENS):
        return True
    # "...qué archivos hablan/mencionan/contienen X..." (+ archivo/archivos)
    if any(v in q for v in _FS_CONTENT_VERBS) and ("archivo" in q or "files" in q):
        return True
    return False


def _extract_content_keyword(query: str) -> Optional[str]:
    """Extrae el tema entre comillas o tras 'hablan de|mencionan|...'.

    Devuelve None si no hay tema identificable (ahí file_search usa su
    flujo normal por nombre)."""
    if not query:
        return None
    m = re.search(r"['\"]([^'\"]{2,60})['\"]", query)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"(?:hablan de|habla de|mencionan|menciona|contienen|contiene|"
        r"traten de|tratan de)\s+(?:el\s+|la\s+|los\s+|las\s+|un\s+|una\s+)?"
        r"([a-z0-9áéíóúñü][a-z0-9áéíóúñü_.\-]{1,39})",
        query.lower(),
    )
    if m:
        kw = m.group(1).strip(" .,:;¿?")
        # descartar si el resto es pura ruta de carpeta o palabra vacía
        if kw and not kw.startswith(("tools/", "src/", "config/")):
            return kw
    return None


# ── DispatcherHandlersFilesMixin ───────────────────────────────────────────────────

class DispatcherHandlersFilesMixin:
    """
    Handlers de archivos/carpetas (listado, búsqueda por contenido y análisis
    con grounding) más los helpers de logging/folder compartidos con el
    módulo original. Sin estado propio: todo se resuelve por `self`.
    """

    # ── Handler: listado de carpeta vía fs_list (grounded, sin LLM) ──
    async def _handle_file_list_folder(
        self, query: str, session_id: Optional[str], task_type: str,
        start: float,
    ) -> Optional[Response]:
        """
        v0.6.8pp (C): "usá fs_list para listar la carpeta tools/netron" →
        lista las entradas REALES de la carpeta (fs_list). Sin LLM.

        Devuelve None si no hay carpeta identificable (flujo por nombre).
        """
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        try:
            from src.tools_mcp.tool_registry import get_registry
            registry = get_registry()
            if not registry.has("fs_list"):
                return None
            candidates = _extract_folder_candidates(query, repo_root)
            if not candidates:
                return None
            folder = candidates[0]
            lst = await asyncio.to_thread(
                registry.get("fs_list").run, path=str(folder),
            )
            if not lst.success:
                return None
            entries = (lst.data or {}).get("entries", []) or []
            if not entries:
                return Response(
                    content=f"📂 {folder}: la carpeta está vacía.",
                    session_id=session_id, model_used="fs_list",
                    task_type=task_type,
                    latency_ms=(time.time() - start) * 1000,
                    confidence=0.9, validation_passed=True, error=None,
                    sources=[str(folder)],
                )
            shown = entries[:40]
            extra = f"\n… y {len(entries) - len(shown)} entradas más." \
                if len(entries) > len(shown) else ""
            lines = [f"📂 {folder} ({len(entries)} entradas):"]
            for e in shown:
                kind = e.get("kind", "")
                icon = "📁" if kind == "dir" else "📄"
                pth = str(e.get("path", ""))
                rel = str(Path(pth).relative_to(repo_root)) \
                    if str(pth).startswith(str(repo_root)) else pth
                lines.append(f"  {icon} {rel}")
            lines.append(extra or "")
            content = "\n".join(lines)
            _log_event("DISPATCHER", "FileListFolder completed", extra={
                "folder": str(folder), "entries": len(entries),
            })
            return Response(
                content=content, session_id=session_id, model_used="fs_list",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.95, validation_passed=True, error=None,
                sources=[str(folder)],
            )
        except Exception as e:
            _log_error(e, "_handle_file_list_folder", extra_info={"query": query[:60]})
            return None

    # ── Handler: búsqueda de CONTENIDO local (grep grounded, sin LLM) ──
    async def _handle_file_content_search(
        self, query: str, session_id: Optional[str], task_type: str,
        start: float,
    ) -> Optional[Response]:
        """
        v0.6.8pp (C): responde "qué archivos de X hablan/mencionan Y" o
        "usá fs_search para buscar archivos que mencionen Y" listando los
        archivos cuyo CONTENIDO contiene el tema. Cero generación LLM.

        Devuelve None si no hay tema ni carpeta identificables (el llamador
        continúa con el flujo de búsqueda por nombre).
        """
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        keyword = _extract_content_keyword(query)
        if not keyword:
            return None
        try:
            from src.tools_mcp.tool_registry import get_registry
            registry = get_registry()
            if not (registry.has("fs_list") and registry.has("fs_read")):
                return None

            candidates = _extract_folder_candidates(query, repo_root)
            if not candidates:
                # Sin carpeta acotada: al menos no divagar → búsqueda por nombre
                return None

            folders = [c for c in candidates if c.is_dir()][:3]
            needle = keyword.lower()
            hits: list = []          # (relpath, snippet)
            scanned = 0
            for folder in folders:
                lst = await asyncio.to_thread(
                    registry.get("fs_list").run, path=str(folder),
                )
                entries = (lst.data or {}).get("entries", []) if lst.success else []
                _READABLE = {".py", ".md", ".txt", ".json", ".yaml", ".yml",
                             ".toml", ".cfg", ".ini", ".sh", ".csv", ".log"}
                files = []
                for e in entries:
                    pth = str(e.get("path", ""))
                    if e.get("kind") == "dir" or not pth:
                        continue
                    if "__pycache__" in pth or "/." in pth:
                        continue
                    if Path(pth).suffix.lower() in _READABLE:
                        files.append(pth)
                files.sort(key=lambda p2: len(p2))
                for pth in files[:60]:
                    scanned += 1
                    read = await asyncio.to_thread(
                        registry.get("fs_read").run, path=pth, max_chars=6_000,
                    )
                    if not (read.success and read.output):
                        continue
                    low = read.output.lower()
                    if needle not in low:
                        continue
                    rel = str(Path(pth).relative_to(repo_root)) \
                        if str(pth).startswith(str(repo_root)) else pth
                    snippet = ""
                    for line in read.output.splitlines():
                        if needle in line.lower():
                            snippet = line.strip()[:160]
                            break
                    hits.append((rel, snippet))
                    if len(hits) >= 12:
                        break
                if len(hits) >= 12:
                    break

            if not hits:
                return Response(
                    content=f"🔎 En {folders[0]} no encontré archivos cuyo "
                            f"contenido mencione «{keyword}» (revisé {scanned} "
                            f"archivos legibles).",
                    session_id=session_id, model_used="fs_read",
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.9, validation_passed=True, error=None,
                    sources=[str(f) for f in folders],
                )

            lines = [f"🔎 Archivos que mencionan «{keyword}» "
                     f"({len(hits)}):\n"]
            for rel, snippet in hits:
                if snippet:
                    lines.append(f"• {rel}\n    …{snippet}")
                else:
                    lines.append(f"• {rel}")
            content = "\n".join(lines)
            _log_event("DISPATCHER", "FileContentSearch completed", extra={
                "keyword": keyword[:40], "hits": len(hits),
            })
            return Response(
                content=content, session_id=session_id, model_used="fs_read",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.95, validation_passed=True, error=None,
                sources=[h[0] for h in hits][:12],
            )
        except Exception as e:
            _log_error(e, "_handle_file_content_search", extra_info={"query": query[:60]})
            return None

    # ── Handler: Folder analysis (grounding real: lista + lee la carpeta) ──
    async def _handle_folder_analysis(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        """
        Analiza UNA CARPETA leyendo sus archivos de verdad (fs_list + fs_read)
        y resumiendo con el modelo de código. La respuesta queda anclada al
        contenido (sources) — el modelo no puede inventar módulos que no leyó.

        Límites de seguridad: cap de archivos (12) y de contexto (~18k chars).
        """
        from pathlib import Path

        start = time.time()
        query = message.content or ""
        repo_root = Path(__file__).resolve().parents[2]
        _log_event("DISPATCHER", "FolderAnalysis started", extra={
            "query_preview": query[:60],
        })

        try:
            from src.tools_mcp.tool_registry import get_registry
            registry = get_registry()
            if not (registry.has("fs_list") and registry.has("fs_read")):
                return Response(
                    content="El análisis de carpetas no está disponible (faltan fs_list/fs_read).",
                    session_id=session_id, model_used=settings.llm_default_model,
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.5, validation_passed=True, error=None,
                )

            candidates = _extract_folder_candidates(query, repo_root)
            if not candidates:
                return Response(
                    content="No encontré esa carpeta. Probá con el nombre exacto, "
                            "ej: 'analiza la carpeta tools/netron' o 'qué hace src/core'.",
                    session_id=session_id, model_used=settings.llm_default_model,
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.5, validation_passed=True, error=None,
                )

            folder = candidates[0]
            lst = await asyncio.to_thread(
                registry.get("fs_list").run, path=str(folder),
            )
            entries = (lst.data or {}).get("entries", []) if lst.success else []

            _READABLE = {".py", ".md", ".txt", ".json", ".yaml", ".yml",
                         ".toml", ".cfg", ".ini", ".sh"}
            files = []
            for e in entries:
                pth = str(e.get("path", ""))
                if e.get("kind") == "dir" or not pth:
                    continue
                if "__pycache__" in pth or "/." in pth or pth.endswith("__init__.pyc"):
                    continue
                if Path(pth).suffix.lower() in _READABLE:
                    files.append(pth)
            # Preferir .py/.md y nombres cortos; cap de archivos
            files.sort(key=lambda p2: (Path(p2).suffix not in (".py", ".md"), len(p2)))
            files = files[:12]

            if not files:
                return Response(
                    content=f"📂 {folder}: no encontré archivos legibles "
                            f"(.py/.md/.json/…) para analizar.",
                    session_id=session_id, model_used="fs_list",
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.8, validation_passed=True, error=None,
                    sources=[str(folder)],
                )

            blob_parts = []
            total = 0
            budget = 18_000
            read_sources = []
            truncated = False
            for pth in files:
                if total >= budget:
                    truncated = True
                    break
                read = await asyncio.to_thread(
                    registry.get("fs_read").run, path=pth, max_chars=4_000,
                )
                if not (read.success and read.output):
                    continue
                rel = str(Path(pth).relative_to(repo_root)) if str(pth).startswith(str(repo_root)) else pth
                chunk = read.output[:budget - total]
                blob_parts.append(f"===== {rel} =====\n{chunk}")
                total += len(chunk)
                read_sources.append(str(pth))

            if not blob_parts:
                return Response(
                    content=f"📂 {folder}: no pude leer el contenido de los archivos.",
                    session_id=session_id, model_used="fs_read",
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.6, validation_passed=True, error=None,
                    sources=[str(folder)],
                )

            note = ("\n(se truncó la lectura por límite de contexto)" if truncated else "")
            prompt = (
                "Analizá la carpeta de código que sigue. Respondé EN ESPAÑOL, "
                "estructurado:\n"
                "1) qué es/para qué sirve la carpeta en conjunto;\n"
                "2) qué hace cada archivo (una línea por archivo, basándote "
                "SOLO en su contenido);\n"
                "3) cómo se relacionan entre sí.\n"
                "REGLA: basate ÚNICAMENTE en el contenido provisto. Si un "
                "archivo no aporta, decilo. NO inventes módulos, funciones ni "
                "archivos que no estén en el contenido.\n\n"
                f"CARPETA: {folder}\n"
                f"ARCHIVOS LEÍDOS ({len(read_sources)}):\n"
                + "\n".join(f"- {p3}" for p3 in read_sources)
                + f"\n\nCONTENIDO:\n" + "\n".join(blob_parts) + note
            )

            gen = await self._llm_generate_with_retry(
                prompt=prompt,
                model=settings.llm_code_model,
                max_tokens=900,
            )
            text = getattr(gen, "content", "") or ""
            if not text.strip():
                text = "El modelo no devolvió un análisis (vacío)."

            content = f"📂 Análisis de {folder}:\n\n{text.strip()}"
            if truncated:
                content += f"\n\n_⚠️ Se analizaron {len(read_sources)} archivos (límite de contexto)._"
            return Response(
                content=content,
                session_id=session_id,
                model_used=settings.llm_code_model,
                task_type=task_type,
                latency_ms=(time.time() - start) * 1000,
                confidence=0.9,
                validation_passed=True,
                error=None,
                sources=read_sources[:10],
            )
        except Exception as e:
            _log_error(e, "_handle_folder_analysis", extra_info={"task_type": task_type})
            return Response(
                content=f"Error analizando la carpeta: {e}",
                session_id=session_id, model_used=settings.llm_default_model,
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )
