#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — tool_fs_implementations.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/tools_mcp/tool_fs_implementations.py
#
# ✅ v0.6.9v (REFACTOR): herramientas de FILESYSTEM extraídas de
#   src/tools_mcp/tool_implementations.py (el original queda intacto).
#
#   Qué se movió desde tool_implementations.py (sin cambios de comportamiento):
#     - Constantes `_TEXT_SUFFIXES` / `_TEXT_FILENAMES`
#     - Helper `_classify_kind`
#     - Clases `FsReadTool` / `FsWriteTool` / `FsListTool` / `FsSearchTool`
#       / `FsDeleteTool` / `FsMoveTool`
#
#   tool_implementations.py re-exporta ahora estos símbolos (import), así la
#   API pública para tool_registry.py (`from ...tool_implementations import
#   FsReadTool, …`) NO cambia.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import fnmatch
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from .tool_base import (
    BaseTool,
    ToolPermission,
    ToolResult,
    ToolSchema,
    ToolParam,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# CLASIFICACIÓN DE KIND (text/program/binary) para el agente filesystem
# ═══════════════════════════════════════════════════════════════
_TEXT_SUFFIXES: frozenset[str] = frozenset({  # noqa: E501
    ".py", ".pyi", ".pyx", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".c", ".h", ".hpp", ".cpp", ".cc", ".cs", ".go", ".rs",
    ".rb", ".php", ".pl", ".lua", ".swift", ".kt", ".scala",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".sql", ".r",
    ".html", ".htm", ".css", ".scss", ".sass", ".xml", ".svg",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".md", ".markdown", ".rst", ".txt", ".log", ".csv", ".tsv",
})
_TEXT_FILENAMES: frozenset[str] = frozenset({  # noqa: E501
    ".gitignore", ".dockerignore", ".editorconfig", ".env.example",
    "dockerfile", "makefile", "readme", "license", "changelog",
    "requirements.txt", "pipfile",
})


def _classify_kind(path: Path) -> str:
    """Clasifica una entrada de filesystem: 'dir' | 'text' | 'program' | 'binary'."""
    if path.is_dir():
        return "dir"
    if path.suffix.lower() in _TEXT_SUFFIXES or path.name.lower() in _TEXT_FILENAMES:
        return "text"
    mime, _ = mimetypes.guess_type(path.name)
    if mime is not None and mime.startswith("text/"):
        return "text"
    try:
        if os.access(path, os.X_OK):
            return "program"
    except OSError as _d2e:
        logging.getLogger(__name__).debug(
            f"[D2 tool_fs_implementations.py] excepción degradada (intencional): {_d2e}")
    return "binary"


class FsReadTool(BaseTool):
    """Lee el contenido de un archivo del sistema."""
    name = "fs_read"
    description = (
        "Lee y retorna el contenido de un archivo. "
        "Soporta texto plano, código, configuraciones y logs."
    )
    permission_level = ToolPermission.READ_ONLY
    tags = ["filesystem", "read"]

    schema = ToolSchema(
        name="fs_read",
        description=description,
        params=[
            ToolParam("path", "str", "Ruta al archivo a leer"),
            ToolParam("encoding", "str", "Encoding del archivo", required=False, default="utf-8"),
            ToolParam("max_chars", "int", "Máximo de caracteres a retornar", required=False, default=50000),
        ],
        returns="str — contenido del archivo",
        tags=["filesystem", "read"],
    )

    def _run(self, path: str = "", encoding: str = "utf-8", max_chars: int = 50000, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            p = Path(path).expanduser().resolve()
            if not p.exists():
                return ToolResult.fail(
                    error=f"Archivo no encontrado: {path}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            if not p.is_file():
                return ToolResult.fail(
                    error=f"La ruta no es un archivo: {path}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            content = p.read_text(encoding=encoding, errors="replace")
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n... [truncado a {max_chars} chars]"
            latency = (time.perf_counter() - t0) * 1000
            # ✅ NUEVO (agente filesystem): kind en data — el caller sabe si
            # estaba leyendo texto real o un binario/programa como texto crudo
            # (relevante, por ejemplo, para no proponer ese contenido como
            # copia/escritura de otro archivo sin saber que es basura decodificada).
            return ToolResult.ok(
                output=content,
                tool_name=self.name,
                latency_ms=latency,
                data={
                    "path": str(p),
                    "size_bytes": p.stat().st_size,
                    "truncated": len(content) > max_chars,
                    "kind": _classify_kind(p),
                },
                metadata={"path": str(p)},
            )
        except PermissionError:
            return ToolResult.fail(
                error=f"Sin permisos para leer: {path}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )


class FsWriteTool(BaseTool):
    """Escribe o sobreescribe un archivo en el sistema."""
    name = "fs_write"
    description = (
        "Escribe contenido en un archivo. Crea el archivo si no existe. "
        "Crea directorios intermedios automáticamente."
    )
    permission_level = ToolPermission.LOCAL_WRITE
    tags = ["filesystem", "write"]

    schema = ToolSchema(
        name="fs_write",
        description=description,
        params=[
            ToolParam("path", "str", "Ruta del archivo a escribir"),
            ToolParam("content", "str", "Contenido a escribir"),
            ToolParam("encoding", "str", "Encoding", required=False, default="utf-8"),
            ToolParam("append", "bool", "Si True, agrega al final en lugar de sobreescribir", required=False, default=False),
        ],
        returns="str — confirmación con ruta y bytes escritos",
        tags=["filesystem", "write"],
    )

    def _run(self, path: str = "", content: str = "", encoding: str = "utf-8", append: bool = False, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            p = Path(path).expanduser().resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with p.open(mode, encoding=encoding) as f:
                f.write(content)
            bytes_written = len(content.encode(encoding))
            latency = (time.perf_counter() - t0) * 1000
            action = "agregado" if append else "escrito"
            return ToolResult.ok(
                output=f"Archivo {action}: {p} ({bytes_written} bytes)",
                tool_name=self.name,
                latency_ms=latency,
                data={"path": str(p), "bytes_written": bytes_written, "append": append},
                metadata={"path": str(p)},
            )
        except PermissionError:
            return ToolResult.fail(
                error=f"Sin permisos para escribir: {path}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )


class FsListTool(BaseTool):
    """Lista el contenido de un directorio."""
    name = "fs_list"
    description = (
        "Lista archivos y subdirectorios de una ruta. "
        "Retorna nombre, tipo, tamaño y fecha de modificación."
    )
    permission_level = ToolPermission.READ_ONLY
    tags = ["filesystem", "read"]

    schema = ToolSchema(
        name="fs_list",
        description=description,
        params=[
            ToolParam("path", "str", "Directorio a listar"),
            ToolParam("pattern", "str", "Patrón glob de filtro, ej: '*.py'", required=False, default="*"),
            ToolParam("recursive", "bool", "Si True, lista recursivamente", required=False, default=False),
        ],
        returns="str — lista de archivos con metadatos",
        tags=["filesystem", "read"],
    )

    def _run(self, path: str = ".", pattern: str = "*", recursive: bool = False, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            p = Path(path).expanduser().resolve()
            if not p.exists():
                return ToolResult.fail(
                    error=f"Directorio no encontrado: {path}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            if not p.is_dir():
                return ToolResult.fail(
                    error=f"La ruta no es un directorio: {path}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )

            glob_fn = p.rglob if recursive else p.glob
            entries = []
            for item in sorted(glob_fn(pattern)):
                stat = item.stat()
                # ✅ NUEVO (agente filesystem): kind distingue texto/programa/
                # binario — ver _classify_kind() en este módulo.
                entries.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "kind": _classify_kind(item),
                    "size_bytes": stat.st_size if item.is_file() else 0,
                    "path": str(item),
                })

            lines = []
            for e in entries:
                icon = "📁" if e["type"] == "dir" else "📄"
                size = f" ({e['size_bytes']} B)" if e["type"] == "file" else ""
                kind_tag = f" [{e['kind']}]" if e["type"] == "file" else ""
                lines.append(f"{icon} {e['name']}{size}{kind_tag}")

            output = f"Contenido de {p} ({len(entries)} elementos):\n" + "\n".join(lines)
            latency = (time.perf_counter() - t0) * 1000
            return ToolResult.ok(
                output=output,
                tool_name=self.name,
                latency_ms=latency,
                data={"path": str(p), "entries": entries, "count": len(entries)},
            )
        except PermissionError:
            return ToolResult.fail(
                error=f"Sin permisos para listar: {path}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )


class FsSearchTool(BaseTool):
    """
    Busca archivos y carpetas por NOMBRE dentro de un árbol de directorios.
    ✅ plan_rafael (2026-08-31): Rafael puede decir la ubicación exacta de un
    archivo/carpeta ("buscá el archivo X", "dónde está la carpeta Y").
    """
    name = "fs_search"
    description = (
        "Busca archivos y carpetas por nombre (substring o glob) dentro de un "
        "directorio base. Default: la carpeta del proyecto AXIOMA. "
        "Retorna la ruta EXACTA de cada coincidencia (nombre y lugar)."
    )
    permission_level = ToolPermission.READ_ONLY
    tags = ["filesystem", "search"]

    schema = ToolSchema(
        name="fs_search",
        description=description,
        params=[
            ToolParam("name", "str", "Nombre o parte del nombre a buscar (ej: 'benchmark', '*.py', 'data')"),
            ToolParam("base_dir", "str", "Directorio raíz de la búsqueda (default: carpeta AXIOMA)", required=False, default=""),
            ToolParam("max_results", "int", "Máximo de resultados (default 20)", required=False, default=20),
            ToolParam("case_sensitive", "bool", "Si False (default), ignora mayúsculas", required=False, default=False),
        ],
        returns="str — lista de rutas exactas encontradas (nombre + ubicación)",
        tags=["filesystem", "search"],
    )

    # Directorios pesados/irrelevantes que se saltan al caminar (velocidad).
    _SKIP_DIRS = frozenset({
        "venv", ".git", "__pycache__", "node_modules", ".cache",
        ".optimizer_backups", "backups", ".venv", "site-packages",
        ".pytest_cache", ".mypy_cache", "dist", "build", ".git_backup",
        # ✅ plan_rafael: dirs pesados/internos que no aportan a la búsqueda
        "memory", "semantic_cache_l3_lance", "semantic_cache_l3", "cache",
        "logs", "outputs", "transcripts", "knowledge", "feedback", "backups",
        "doc_cache", "router_cache", "trace_store", "web_cache", "uploads",
    })

    def _run(
        self, name: str = "", base_dir: str = "", max_results: int = 20,
        case_sensitive: bool = False, **kwargs: Any,
    ) -> ToolResult:
        t0 = time.perf_counter()
        name = (name or "").strip()
        if not name:
            return ToolResult.fail(
                error="Parámetro 'name' vacío — indicá qué archivo/carpeta buscar",
                tool_name=self.name, latency_ms=(time.perf_counter() - t0) * 1000,
            )
        try:
            from config.paths import Paths
            base = Path(base_dir).expanduser() if base_dir else Paths.ROOT
            base = base.resolve()
        except Exception:
            base = Path(".").resolve()
        if not base.exists() or not base.is_dir():
            return ToolResult.fail(
                error=f"Directorio base no encontrado: {base}",
                tool_name=self.name, latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # ¿Es glob (* ?) o substring? Glob → fnmatch; substring → case-insens.
        is_glob = any(ch in name for ch in "*?[")
        needle = name.lower() if not case_sensitive and not is_glob else name

        def _match(candidate: str) -> bool:
            if is_glob:
                return fnmatch.fnmatch(candidate, needle)
            c = candidate.lower() if not case_sensitive else candidate
            return needle in c

        found: List[Dict[str, Any]] = []
        try:
            for dirpath, dirnames, filenames in os.walk(base):
                # Poda de dirs pesados (mutar dirnames afecta el walk).
                dirnames[:] = [
                    d for d in dirnames
                    if d not in self._SKIP_DIRS
                    and not any(part in self._SKIP_DIRS for part in Path(dirpath).parts)
                ]
                for dname in dirnames:
                    if len(found) >= max_results:
                        break
                    if not _match(dname):
                        continue
                    found.append({
                        "name": dname, "type": "dir",
                        "path": str((Path(dirpath) / dname).resolve()),
                    })
                for fname in filenames:
                    if len(found) >= max_results:
                        break
                    if not _match(fname):
                        continue
                    found.append({
                        "name": fname, "type": "file",
                        "path": str((Path(dirpath) / fname).resolve()),
                    })
                if len(found) >= max_results:
                    break
        except PermissionError:
            return ToolResult.fail(
                error=f"Sin permisos para buscar en {base}",
                tool_name=self.name, latency_ms=(time.perf_counter() - t0) * 1000,
            )

        if not found:
            output = f"No se encontraron archivos/carpetas con '{name}' en {base}"
            return ToolResult.ok(
                output=output, tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
                data={"query": name, "base": str(base), "count": 0, "matches": []},
            )

        lines = []
        for e in found[:max_results]:
            icon = "📁" if e["type"] == "dir" else "📄"
            lines.append(f"{icon} {e['name']} → {e['path']}")
        if len(found) > max_results:
            lines.append(f"… y {len(found) - max_results} resultados más")
        output = f"Resultados de '{name}' en {base} ({len(found)}):\n" + "\n".join(lines)
        return ToolResult.ok(
            output=output, tool_name=self.name,
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={"query": name, "base": str(base), "count": len(found), "matches": found[:max_results]},
        )


class FsDeleteTool(BaseTool):
    """Elimina un archivo del sistema (no directorios)."""
    name = "fs_delete"
    description = (
        "Elimina un archivo específico. No elimina directorios. "
        "Requiere confirmación explícita via parámetro confirm=True."
    )
    permission_level = ToolPermission.LOCAL_WRITE
    tags = ["filesystem", "write"]

    schema = ToolSchema(
        name="fs_delete",
        description=description,
        params=[
            ToolParam("path", "str", "Ruta del archivo a eliminar"),
            ToolParam("confirm", "bool", "Debe ser True para ejecutar (protección contra accidentes)"),
        ],
        returns="str — confirmación de eliminación",
        tags=["filesystem", "write"],
    )

    def _run(self, path: str = "", confirm: bool = False, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        if not confirm:
            return ToolResult.fail(
                error="confirm=True requerido para eliminar archivos.",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        try:
            p = Path(path).expanduser().resolve()
            if not p.exists():
                return ToolResult.fail(
                    error=f"Archivo no encontrado: {path}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            if p.is_dir():
                return ToolResult.fail(
                    error=f"No elimina directorios: {path}. Usar solo para archivos.",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            size = p.stat().st_size
            p.unlink()
            latency = (time.perf_counter() - t0) * 1000
            return ToolResult.ok(
                output=f"Archivo eliminado: {p} ({size} bytes)",
                tool_name=self.name,
                latency_ms=latency,
                data={"path": str(p), "size_bytes": size},
            )
        except PermissionError:
            return ToolResult.fail(
                error=f"Sin permisos para eliminar: {path}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )


class FsMoveTool(BaseTool):
    """Mueve o renombra un archivo."""
    name = "fs_move"
    description = "Mueve o renombra un archivo de una ruta a otra."
    permission_level = ToolPermission.LOCAL_WRITE
    tags = ["filesystem", "write"]

    schema = ToolSchema(
        name="fs_move",
        description=description,
        params=[
            ToolParam("src", "str", "Ruta origen"),
            ToolParam("dst", "str", "Ruta destino"),
        ],
        returns="str — confirmación del movimiento",
        tags=["filesystem", "write"],
    )

    def _run(self, src: str = "", dst: str = "", **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            import shutil
            s = Path(src).expanduser().resolve()
            d = Path(dst).expanduser().resolve()
            if not s.exists():
                return ToolResult.fail(
                    error=f"Origen no encontrado: {src}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(d))
            latency = (time.perf_counter() - t0) * 1000
            return ToolResult.ok(
                output=f"Movido: {s} → {d}",
                tool_name=self.name,
                latency_ms=latency,
                data={"src": str(s), "dst": str(d)},
            )
        except PermissionError:
            return ToolResult.fail(
                error=f"Sin permisos para mover: {src}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )


__all__ = [
    "FsReadTool",
    "FsWriteTool",
    "FsListTool",
    "FsSearchTool",
    "FsDeleteTool",
    "FsMoveTool",
]
