#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.11 — Lógica de Negocio (Extraída de app.py)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/web/app_logic.py
# Autor: SaintWick
# Fecha: 2026-04-05
# Propósito: Funciones puras y helpers de negocio extraídos de app.py
#            para testabilidad unitaria sin dependencias directas de
#            renderizado UI NiceGUI (diálogos, páginas, header).
#            Contiene: logging, parseo, formateo, persistencia,
#            health checks, helpers de código y settings.
#
# ✅ FIX v0.0.11: _setting_select ahora valida que value esté en options
#   Previene ValueError: Invalid value cuando el valor por defecto
#   no está en la lista de opciones del selector
#
# ✅ v0.1.5: Selector de modelo de visión integrado
#   - save_settings() persiste vision_model
#   - load_settings() carga vision_model y actualiza settings.vision_model_selector
#
# ✅ FIX v0.1.5: Regex _extract_code_blocks — salto de línea literal → \n
#   SyntaxError: unterminated string literal (detected at line 210)
# ✅ v0.0.11: Eliminado moondream:v2 de validación (v0.6.5)
# ═══════════════════════════════════════════════════════════════
import asyncio
import json
import logging
import os
import re
import sys
import time
import warnings
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable, Union, Tuple
from nicegui import ui, app as nicegui_app
from src.utils.degradation import log_degraded

# ============================================================================
# CONFIGURACIÓN DE RUTAS E IMPORTS
# ============================================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from config.paths import Paths

logger = logging.getLogger(__name__)

# ============================================================================
# LOGGING — INTEGRACIÓN CON DETAILEDLOGGER (Test-friendly)
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False

# ✅ CORREGIDO: Exponer explícitamente para permitir mocking en tests
LOGGER_AVAILABLE = _LOGGER_AVAILABLE


def _log_web_event(
    category: str,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
    level: str = "INFO"
) -> None:
    """
    Registra evento web en DetailedLogger con verificación robusta.
    Args:
        category: Categoría del evento (UI, CHAT, API, SESSION, SYSTEM)
        message: Mensaje descriptivo del evento
        extra: Información adicional para el log (dict opcional)
        level: Nivel de logging (INFO, WARNING, ERROR)
    Returns:
        None
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category, message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 app_logic.py:86] excepción degradada (intencional): {_d2e}")


def _log_error(
    error: Exception,
    context: str,
    extra: Optional[Dict[str, Any]] = None
) -> None:
    """
    Registra error con traceback completo en DetailedLogger.
    Args:
        error: Excepción capturada
        context: Contexto donde ocurrió el error (nombre de función)
        extra: Información adicional para el log
    Returns:
        None
    """
    if not LOGGER_AVAILABLE:
        logger.error(f"web.{context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(
                error=error,
                context=context,
                extra_info=extra,
                function_name=f"web.{context}"
            )
    except Exception:
        logger.error(f"web.{context}: {error}")


# ============================================================================
# ✅ FUNCIONES PURAS — LÓGICA DE NEGOCIO SIN UI (Testeables)
# ============================================================================
def _format_api_status(is_configured: bool) -> Tuple[str, str]:
    """
    Formatea estado de API para UI.
    Args:
        is_configured: Si la API está configurada
    Returns:
        Tuple[str, str]: (texto estado, clase de color)
    """
    if is_configured:
        return "✅ Configurada", "text-green"
    return "❌ No configurada", "text-red"


def _build_response_metadata(
    response: Any,
    request: str,
    start_time: float
) -> Dict[str, Any]:
    """
    Construye metadata para UI con detección de código.
    Función pura — no depende de estado global.
    Args:
        response: Objeto Response del dispatcher
        request: Texto original de la consulta
        start_time: Timestamp de inicio para cálculo de latencia
    Returns:
        Dict[str, Any]: Metadata estructurada para ChatComponent
    """
    meta: Dict[str, Any] = {
        'request': request,
        'task_type': getattr(response, 'task_type', 'UNKNOWN'),
        'model_used': getattr(response, 'model_used', 'unknown'),
        'latency_ms': getattr(response, 'latency_ms', (time.time() - start_time) * 1000),
        'ambiguity_level': 'none',
    }

    # Manejo seguro de metadata None
    if hasattr(response, 'metadata') and response.metadata is not None:
        for key in ['has_code', 'extracted_code', 'suggested_filename']:
            if key in response.metadata:
                meta[key] = response.metadata[key]

        # ✅ Fase 5: contrato de código verificado (Fase 1-4) — cuando está
        # presente es más rico que has_code/extracted_code (multi-archivo,
        # dependencias, tests, y el resultado REAL de validación, no solo
        # la heurística). Coexiste con has_code/extracted_code (coder.py
        # sigue seteando ambos) — legacy queda intacto para
        # CODE_EXPLANATION/CODE_REVIEW/contrato deshabilitado/needs_chunked,
        # que nunca traen code_artifact.
        if 'code_artifact' in response.metadata:
            meta['code_artifact'] = response.metadata['code_artifact']
        if 'validation_report' in response.metadata:
            meta['validation_report'] = response.metadata['validation_report']

    if meta.get('code_artifact'):
        _autosave_code_artifact(meta['code_artifact'])
    elif meta.get('has_code') and meta.get('extracted_code'):
        # Path legacy — código sin contrato (explicación/revisión/etc.),
        # se sigue guardando solo el primer bloque como antes.
        try:
            code_data = meta['extracted_code']
            if isinstance(code_data, list) and len(code_data) > 0:
                save_code_to_file(
                    code_data[0].get('code', ''),
                    meta.get('suggested_filename', 'code.py')
                )
        except Exception as e:
            logger.warning(f"Auto-save code failed: {e}")

    # ✅ v0.6.9p: fuentes/citas — paths/URLs que respaldan la respuesta
    # (file_search grounded, folder-analysis, content-search, etc.)
    raw_sources = getattr(response, 'sources', None)
    if not raw_sources and getattr(response, 'metadata', None):
        raw_sources = (response.metadata or {}).get('sources')
    srcs: List[str] = []
    for s in (raw_sources or []):
        if isinstance(s, str) and s.strip() and s not in srcs:
            srcs.append(s.strip())
    meta['sources'] = srcs[:8]

    # ✅ v0.6.9v (P0 del plan nuevo): TRANSPARENCIA por respuesta.
    # `Response` ya traía `confidence`, `confidence_label` y `validation_passed`
    # (los setean los agentes y el validador), pero **se descartaban acá**: la UI
    # nunca podía mostrar por qué confiar en una respuesta. Passthrough de lo que
    # ya existe — sin banderas new y sin costo (son datos del propio objeto).
    # `None` = "no se pudo determinar" (mejor que 0.0, que se leería como
    # "0% de confianza": una afirmación que el sistema no hizo).
    _conf = _safe_float(getattr(response, 'confidence', None), None)
    meta['confidence'] = _conf
    meta['confidence_label'] = (
        getattr(response, 'confidence_label', None)
        or (_confidence_label(_conf) if _conf is not None else None)
    )
    meta['validation_passed'] = bool(getattr(response, 'validation_passed', False))
    # Estado REAL de la validación (tri-estado honesto): el dispatcher marca
    # `validation_failed` / `validation_skipped_<razón>`; si no hay ninguna marca
    # ni `validation_passed`, la validación **no se aplicó** a esta tarea.
    _rmeta = getattr(response, 'metadata', None) or {}
    meta['validation_state'] = _validation_state(meta['validation_passed'], _rmeta)
    meta['agent_role'] = (
        _rmeta.get('agent_role')
        or getattr(response, 'agent_role', None)
        or ''
    )
    meta['classification_confidence'] = _safe_float(
        _rmeta.get('classification_confidence'), None)

    return meta


def _safe_float(value: Any, default: Optional[float]) -> Optional[float]:
    """Convierte a float sin romper (None si no es numérico)."""
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return default


def _confidence_label(value: float) -> str:
    """Etiqueta de confianza cuando `Response` no la trae.

    Umbrales elegidos para que coincidan con lo que ya comunica el sistema:
    alto ≥ 0.8 (fuentes verificadas), moderado ≥ 0.5, bajo por debajo.
    """
    if value >= 0.8:
        return 'Alto'
    if value >= 0.5:
        return 'Moderado'
    return 'Bajo'


def _validation_state(passed: bool, response_meta: Dict[str, Any]) -> str:
    """'aprobada' | 'fallida' | 'en curso' | 'omitida' | 'no_aplicada' (honesto).

    ✅ 2026-10-03: se agregó **'en curso'**. Desde que la validación corre en
    segundo plano (medido: 41-43 s por respuesta), cuando el mensaje se dibuja el
    veredicto todavía no existe: antes eso se mostraba como 'no_aplicada' (una
    mentira por omisión). Ahora se distingue "se está validando" de "no se validó".
    """
    if passed:
        return 'aprobada'
    if response_meta.get('validation_failed'):
        return 'fallida'
    if response_meta.get('validation_en_fondo') and \
            not response_meta.get('validation_en_fondo_completada'):
        return 'en curso'
    if response_meta.get('validation_en_fondo_completada'):
        # Terminó en segundo plano: aprobada si dejó score, si no "no aplicada".
        return ('aprobada' if response_meta.get('validation_score') is not None
                else 'no_aplicada')
    if any(str(k).startswith('validation_skipped') for k in response_meta):
        return 'omitida'
    return 'no_aplicada'


# ═══════════════════════════════════════════════════════════════
# Explorador de archivos (v0.6.9p) — navegación UI 📁 → adjuntar
# ═══════════════════════════════════════════════════════════════
_EXPLORE_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".optimizer_backups", "deepseek-harness",
    ".idea", ".gradle", ".cache", "cache", ".local",
})
_EXPLORE_MAX = 500


def explore_dir(path: str) -> Optional[Dict[str, Any]]:
    """Lista un directorio para el explorador de archivos de la UI (📁).

    Oculta dotfiles y carpetas ruidosas (venv, .git, node_modules, …).
    Devuelve {"path", "parent", "dirs", "files"} con entradas
    {name, path, is_dir, size}; None si el path no es un directorio accesible.
    Función pura y testeable — sin UI.
    """
    p = Path(path)
    try:
        if not p.is_dir():
            return None
        dirs: List[Dict[str, Any]] = []
        files: List[Dict[str, Any]] = []
        with os.scandir(p) as it:
            for entry in it:
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        if name in _EXPLORE_SKIP_DIRS:
                            continue
                        dirs.append({"name": name, "path": entry.path,
                                     "is_dir": True, "size": 0})
                    elif entry.is_file():
                        try:
                            size = entry.stat().st_size
                        except OSError:
                            size = 0
                        files.append({"name": name, "path": entry.path,
                                      "is_dir": False, "size": size})
                except OSError:
                    continue
    except OSError:
        return None
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    parent = str(p.parent) if p.parent != p else None
    return {
        "path": str(p),
        "parent": parent,
        "dirs": dirs[:_EXPLORE_MAX],
        "files": files[:_EXPLORE_MAX],
    }


def _autosave_code_artifact(code_artifact: Dict[str, Any]) -> None:
    """
    Guarda TODOS los archivos de un code_artifact (Fase 1-4) en
    data/outputs/ — código y tests, no solo el primer bloque como el path
    legacy. Se guarda igual aunque validation_report.passed sea False: la
    transparencia importa más que ocultar un intento fallido, pero nunca
    se presenta como éxito (eso lo decide el caller vía validation_report,
    no esta función).
    """
    try:
        for f in code_artifact.get('files', []) + code_artifact.get('test_files', []):
            name = f.get('name')
            content = f.get('content')
            if name and content:
                save_code_to_file(content, name)
    except Exception as e:
        logger.warning(f"Auto-save code_artifact failed: {e}")


def compact_code_artifact(code_artifact: Any) -> List[Dict[str, Any]]:
    """Convierte un `code_artifact` (CodeContract.to_dict) en artefactos del mensaje.

    ✅ ISSUE-055: la respuesta trae el contrato con `files` + `test_files`; se
    pasa a la forma que guarda `MessageData.artifacts` (nombre, lenguaje y
    contenido), marcando los tests. Función PURA: sin estado ni I/O.
    """
    if not isinstance(code_artifact, dict):
        return []
    arts: List[Dict[str, Any]] = []
    for clave, es_test in (("files", False), ("test_files", True)):
        for f in (code_artifact.get(clave) or []):
            if not isinstance(f, dict):
                continue
            arts.append({
                "name": f.get("name"),
                "language": f.get("language") or "text",
                "content": f.get("content"),
                "is_test": es_test,
            })
    return arts


def _parse_search_context(search_text: Optional[str], limit: int = 2000) -> Optional[str]:
    """
    Parsea y trunca contexto de búsqueda web.
    Args:
        search_text: Texto crudo de búsqueda
        limit: Límite de caracteres
    Returns:
        Optional[str]: Texto truncado o None si vacío
    """
    if not search_text:
        return None
    truncated = search_text[:limit]
    if len(search_text) > limit:
        truncated += " [...]"
    return truncated


def _extract_code_blocks(content: str) -> List[Dict[str, str]]:
    """
    Extrae bloques de código de contenido markdown.
    Función pura para testing unitario.
    Args:
        content: Texto que puede contener bloques ```python
    Returns:
        List[Dict[str, str]]: Lista de {"language": str, "code": str}
    """
    if not content or not content.strip():
        return []
    code_blocks = []
    # ✅ FIX v0.1.5: Patrón con \n explícito (no salto de línea literal)
    pattern = re.compile(r'```(\w+)?\s*\n(.*?)```', re.DOTALL | re.IGNORECASE)
    matches = pattern.findall(content)
    for lang, code in matches:
        code_clean = code.strip()
        if code_clean and len(code_clean) > 5:
            code_blocks.append({
                "language": (lang or "text").lower(),
                "code": code_clean
            })
    return code_blocks


# ============================================================================
# HELPERS PARA MANEJO DE CÓDIGO (Con logging para coverage tracking)
# ============================================================================
def save_code_to_file(code: str, filename: str) -> str:
    """
    Guarda código en data/outputs/ y retorna path completo.
    Args:
        code: Contenido del código a guardar
        filename: Nombre del archivo
    Returns:
        str: Path absoluto del archivo guardado
    Raises:
        OSError: Si falla la escritura del archivo
    """
    outputs_dir = Paths.DATA / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    filepath = outputs_dir / filename
    filepath.write_text(code, encoding="utf-8")
    _log_web_event("CODE", f"Code saved: {filename}", extra={
        "filepath": str(filepath),
        "size": len(code)
    })
    return str(filepath)


def create_code_actions(
    code: str,
    filename: str,
    container: Any,
    on_copy: Optional[Callable[[], None]] = None,
    on_download: Optional[Callable[[str], None]] = None
) -> None:
    """
    Crea botones de acción (copiar/descargar) para código generado.
    Args:
        code: Código a mostrar
        filename: Nombre sugerido del archivo
        container: Container de NiceGUI donde agregar los botones
        on_copy: Callback opcional al copiar (para testing)
        on_download: Callback opcional al descargar (para testing)
    Returns:
        None
    """
    with container:
        with ui.row().classes('gap-2 mt-2'):
            # Botón copiar
            def _on_copy() -> None:
                # ✅ CORREGIDO: Usar warnings.catch_warnings para suprimir RuntimeWarning
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", category=RuntimeWarning)
                        ui.run_javascript(f'navigator.clipboard.writeText({json.dumps(code)});')
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 app_logic.py:308] excepción degradada (intencional): {_d2e}")
                ui.notify('✅ Código copiado', type='positive')
                if on_copy:
                    on_copy()
            ui.button('📋 Copiar', on_click=_on_copy).props('dense flat color=primary size=sm')

            # Botón descargar
            def _on_download() -> None:
                filepath = save_code_to_file(code, filename)
                ui.notify(f'✅ Guardado: {filepath}', type='positive', timeout=3000)
                ui.download(filepath)
                if on_download:
                    on_download(filepath)
            ui.button('💾 Descargar', on_click=_on_download).props('dense flat color=primary size=sm')
            ui.label(f'📄 {filename}').classes('text-sm text-gray-500 ml-2')





# ============================================================================
# INICIALIZACIÓN DE STORAGE (Test-mockeable)
# ============================================================================
def _init_browser_storage() -> None:
    """
    Inicializa storage del usuario de forma thread-safe.
    ✅ FIX v0.6.8t: storage.browser → storage.user (ver _browser_user_id):
    en NiceGUI 3.11 browser storage es de solo lectura tras la respuesta.
    Returns:
        None
    """
    start = time.time()
    _log_web_event("UI", "User storage initialization started")
    try:
        for key in ['session_id', 'settings', 'user_id']:
            if key not in nicegui_app.storage.user:
                nicegui_app.storage.user[key] = ''
        duration = time.time() - start
        _log_web_event("UI", "User storage initialized", extra={
            "duration": round(duration, 4)
        })
    except RuntimeError as e:
        duration = time.time() - start
        _log_error(e, "_init_browser_storage", extra={
            "reason": "not in page context",
            "duration": round(duration, 4)
        })
        logger.warning(f"Storage initialization skipped (not in page context): {e}")


# ============================================================================
# ✅ FUNCIONES DE API HEALTH CHECK — INYECTABLES PARA TESTING
# ============================================================================
async def _fetch_api_health(
    endpoint: str,
    timeout: float = 5.0,
    http_client: Optional[Any] = None
) -> bool:
    """
    Verificación real de conexión HTTP para health checks.
    Inyectable para testing con mock.
    Args:
        endpoint: URL del endpoint a verificar
        timeout: Timeout en segundos
        http_client: Cliente HTTP inyectable (default: httpx.AsyncClient)
    Returns:
        bool: True si la respuesta es 200, False en caso contrario
    """
    import httpx
    client = http_client or httpx
    try:
        async with client.AsyncClient(timeout=timeout) as c:
            response = await c.get(endpoint)
            return response.status_code == 200
    except Exception as e:
        log_degraded(logger, e, "src/interfaces/web/app_logic.py:_fetch_api_health")
        return False


async def collect_system_status(
    timeout: float = 5.0,
    http_client: Optional[Any] = None,
) -> Dict[str, bool]:
    """
    Recolecta el estado del sistema para el diálogo "Estado" de la UI web.

    Puro (sin NiceGUI) → testeable. Inyectable para testing con mock.

    Returns:
        Dict con: ollama (health HTTP), tavily/news/serper (API keys en settings).
    """
    ollama_ok = False
    try:
        ollama_endpoint = f"{settings.ollama_host}/api/tags"
        ollama_ok = await _fetch_api_health(
            ollama_endpoint, timeout=timeout, http_client=http_client,
        )
    except Exception:
        ollama_ok = False
    return {
        "ollama": ollama_ok,
        "tavily": bool(getattr(settings, "tavily_api_key", None)),
        "news": bool(getattr(settings, "news_api_key", None)),
        "serper": bool(getattr(settings, "serper_api_key", None)),
        "serpapi": bool(getattr(settings, "serpapi_api_key", None)),  # ✅ v0.6.9v
        "dolar": True,  # dolarapi público (sin key)
    }


def _create_api_status_row(
    label: str,
    is_configured: bool,
    container: Any
) -> None:
    """
    Helper para filas de estado de API en status dialog.
    Args:
        label: Nombre de la API
        is_configured: Si está configurada
        container: Container de NiceGUI
    Returns:
        None
    """
    with container:
        with ui.row().classes('w-full justify-between').props('align-center'):
            ui.label(label)
            status_text, color_class = _format_api_status(is_configured)
            ui.label(status_text).classes(color_class)


# ============================================================================
# HELPERS DE SETTINGS Y LOGGING COMÚN
# ============================================================================
def _setting_select(
    label: str,
    options: List[str],
    value: Any,
    on_change: Callable[[Any], None]
) -> None:
    """
    Helper para selects de configuración.
    ✅ FIX v0.0.11: Validación defensiva para prevenir ValueError.
    Args:
        label: Texto de la etiqueta
        options: Lista de opciones
        value: Valor seleccionado actual
        on_change: Callback al cambiar
    Returns:
        None
    """
    # ✅ Validación defensiva: asegurar que value esté en options
    _safe_value = value
    _safe_options = list(options)  # Copia para no mutar la lista original
    if value not in _safe_options:
        _safe_options.insert(0, value)
        logger.warning(
            f"_setting_select: valor '{value}' no estaba en opciones para '{label}'. "
            f"Agregado dinámicamente para prevenir ValueError."
        )

    ui.select(
        options=_safe_options,
        label=label,
        value=_safe_value,
        on_change=lambda e: on_change(e.value) if hasattr(e, 'value') else None
    )


def save_settings(state: Any, sidebar: Optional[Any] = None) -> None:
    """
    Guarda configuración en user storage (server-side).
    ✅ FIX v0.6.8t: storage.browser → storage.user — en NiceGUI 3.11 las
    escrituras a browser storage tras la respuesta de la página se
    descartan (ReadOnlyDict); por eso la configuración nunca persistía.
    Args:
        state: Estado de sesión actual
        sidebar: SidebarComponent opcional — si se pasa, actualiza indicador Jarvis
    Returns:
        None
    """
    _jarvis_mode = getattr(state, "jarvis_mode", "normal") or "normal"
    _vision_model = getattr(state, "vision_model", "auto") or "auto"

    data = {
        'model': state.model,
        'temperature': state.temperature,
        'validation': state.enable_validation,
        'jarvis_mode': _jarvis_mode,
        'vision_model': _vision_model,
    }
    nicegui_app.storage.user['settings'] = json.dumps(data)

    if sidebar is not None and hasattr(sidebar, "update_jarvis_mode_info"):
        try:
            sidebar.update_jarvis_mode_info(_jarvis_mode)
        except Exception as _e:
            logger.warning(f"save_settings: sidebar.update_jarvis_mode_info error: {_e}")

    _log_web_event("SETTINGS", "Settings saved", extra={
        "session_id": state.session_id[:16] if state.session_id else "new",
        **data
    })
    ui.notify('✅ Configuración guardada', color='positive')


def load_settings(state: Any, sidebar: Optional[Any] = None) -> None:
    """
    Carga configuración desde user storage (server-side).
    ✅ v1.0.9: Validación de vision_model con el modelo REAL (settings.llm_vision_model).
    Args:
        state: Estado de sesión actual
    Returns:
        None
    """
    raw = nicegui_app.storage.user.get('settings')
    if raw and isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                # ✅ CORREGIDO: Mapeo explícito de 'validation' → 'enable_validation'
                for key in data:
                    if key == 'validation':
                        setattr(state, 'enable_validation', bool(data[key]))
                    elif key in ['model', 'temperature']:
                        setattr(state, key, data[key])
                    elif key == 'jarvis_mode':
                        _valid = {'normal', 'jarvis', 'voice_only', 'silent'}
                        _mode = data[key] if data[key] in _valid else 'normal'
                        try:
                            state.jarvis_mode = _mode
                        except Exception as _d2e:
                            logging.getLogger(__name__).debug(f"[D2 app_logic.py:536] excepción degradada (intencional): {_d2e}")
                        _ctx = getattr(state, 'context', None)
                        if _ctx is not None:
                            if hasattr(_ctx, 'update_mode'):
                                try:
                                    _ctx.update_mode(_mode)
                                except Exception as _d2e:
                                    logging.getLogger(__name__).debug(f"[D2 app_logic.py:543] excepción degradada (intencional): {_d2e}")
                            elif hasattr(_ctx, 'jarvis_mode'):
                                try:
                                    _ctx.jarvis_mode = _mode
                                except Exception as _d2e:
                                    logging.getLogger(__name__).debug(f"[D2 app_logic.py:548] excepción degradada (intencional): {_d2e}")

                        _loaded_mode = data.get("jarvis_mode", "normal") or "normal"
                        if sidebar is not None and hasattr(sidebar, "update_jarvis_mode_info"):
                            try:
                                sidebar.update_jarvis_mode_info(_loaded_mode)
                            except Exception as _e:
                                logger.warning(f"load_settings: sidebar update error: {_e}")

                    # ✅ v1.0.9: Validar vision_model contra el modelo REAL configurado
                    # (qwen3-vl:4b). Eliminado gemma4:e4b (desinstalado).
                    elif key == 'vision_model':
                        _vision_valid = {'auto', settings.llm_vision_model}
                        _vision = data[key] if data[key] in _vision_valid else 'auto'
                        try:
                            state.vision_model = _vision
                        except Exception as _d2e:
                            logging.getLogger(__name__).debug(f"[D2 app_logic.py:565] excepción degradada (intencional): {_d2e}")

                        # Actualizar settings global para que VisionAgent use el selector
                        try:
                            settings.vision_model_selector = _vision
                        except Exception as _ve:
                            logger.warning(f"load_settings: settings.vision_model_selector update error: {_ve}")

                _log_web_event("SETTINGS", "Settings loaded", extra={
                    "session_id": state.session_id[:16] if state.session_id else "new",
                    **data
                })
        except json.JSONDecodeError as e:
            _log_error(e, "load_settings")


def _log_extra(
    state: Any,
    start_time: float,
    response: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Helper para campos comunes de logging.
    Args:
        state: Estado de sesión
        start_time: Timestamp de inicio
        response: Respuesta opcional del dispatcher
    Returns:
        Dict[str, Any]: Campos extra para logging
    """
    extra: Dict[str, Any] = {
        "session_id": state.session_id[:16] if state.session_id else "new",
        "duration": round(time.time() - start_time, 4)
    }
    if response:
        extra.update({
            "response_length": len(response.content) if hasattr(response, 'content') else 0,
            "has_code": getattr(getattr(response, 'metadata', None) or {}, 'get', lambda k, d=None: d)('has_code', False)
        })
    return extra


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN — app_logic.py (~590 líneas)
# ═══════════════════════════════════════════════════════════════
# | Función                | Tipo         | Testable sin UI |
# |------------------------|--------------|-----------------|
# | LOGGER_AVAILABLE       | Variable     | ✅ Mockable     |
# | _log_web_event         | Logging      | ✅              |
# | _log_error             | Logging      | ✅              |
# | _format_api_status     | Pura         | ✅              |
# | _build_response_metadata| Pura*       | ✅              |
# | _parse_search_context  | Pura         | ✅              |
# | _extract_code_blocks   | Pura         | ✅              |
# | save_code_to_file      | I/O          | ✅ (mock Path)  |
# | create_code_actions    | UI helper    | ❌ (usa ui.*)   |
# | _init_browser_storage  | Storage      | ✅ (mock app)   |
# | _fetch_api_health      | Async HTTP   | ✅ (injectable) |
# | _create_api_status_row | UI helper    | ❌ (usa ui.*)   |
# | _setting_select        | UI helper    | ❌ (usa ui.*)   |
# | save_settings          | Storage+UI   | ⚠️ (mock app)   |
# | load_settings          | Storage      | ✅ (mock app)   |
# | _log_extra             | Pura         | ✅              |
# ═══════════════════════════════════════════════════════════════
# ✅ v0.0.11: Eliminado moondream:v2 de validación (v0.6.5)
#   - load_settings() ahora valida solo {'auto', 'gemma4:e4b'}
#   - Si se encuentra 'moondream:v2' en storage, fuerza reset a 'auto'
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
