#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# permissions.py - Permission Gateway (Control Granular de Permisos)
# Ruta: /ruta/a/axioma/src/tools_mcp/permissions.py
# Autor: SaintWick
# Versión: 0.5.1
#
# Controla permisos para ejecución de tools con modos DEFAULT,
# ACCEPT_EDITS, BYPASS, PLAN y AUTO. Evalúa reglas por path y
# tool, integra con watchdog para recarga en caliente y persiste
# configuración en config/permissions.json. Incluye mitigaciones
# de seguridad como bloqueo de BYPASS en caliente, path traversal
# y clampeo de inputs.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tool_base import ToolPermission

logger = logging.getLogger(__name__)

# ── Import seguro watchdog ────────────────────────────────────────────────────
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileModifiedEvent
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
    Observer = None          # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]

# Ruta por defecto del archivo de configuración
_DEFAULT_CONFIG_PATH = Path("config/permissions.json")


# ============================================================================
# PERMISSION MODE — 5 modos de operación
# ============================================================================
class PermissionMode(str, Enum):
    """
    Modos de operación del gateway para una regla dada.

    DEFAULT      → Consulta al usuario antes de ejecutar.
                   Comportamiento seguro para rutas sensibles no clasificadas.

    ACCEPT_EDITS → Aprueba automáticamente ediciones en paths del whitelist.
                   Útil para directorios de trabajo donde el agente opera libremente.

    BYPASS       → Sin preguntas, sin restricciones de confianza/riesgo.
                   Solo para entorno de desarrollo. NUNCA en producción.

    PLAN         → Solo permite operaciones de planificación (read, analyze).
                   Bloquea cualquier escritura o ejecución efectiva.

    AUTO         → Autonomía total: ejecuta sin preguntar pero registra
                   todo para auditoría post-hoc. Requiere alta confianza mínima.

    DENY         → ✅ FASE 1 (plan_harness): deniega TODO bajo el pattern
                   excepto las tools explícitamente listadas en allowed_tools
                   (whitelist de solo lectura típicamente). Es la regla
                   "NUNCA tocar" para rutas críticas (.git, .env, config, data).
    """
    DEFAULT      = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS       = "bypass"
    PLAN         = "plan"
    AUTO         = "auto"
    DENY         = "deny"


# ============================================================================
# DECISION — Resultado de evaluación del gateway
# ============================================================================
class Decision(str, Enum):
    """Resultado de PermissionGateway.evaluate()."""
    ALLOW = "allow"   # Ejecutar sin intervención
    DENY  = "deny"    # Bloquear — retornar ToolResult.fail()
    ASK   = "ask"     # Pausar y consultar al usuario


# ============================================================================
# PERMISSION RULE — Regla individual
# ============================================================================
@dataclass
class PermissionRule:
    """
    Regla de permiso para un path + conjunto de tools.

    pattern          : Glob que matchea paths afectados.
                       Ejemplos: "src/*.py", "~/.config/**", "*" (catch-all)
    mode             : PermissionMode que aplica cuando la regla coincide.
    min_confidence   : Confianza mínima del clasificador para ALLOW en AUTO/ACCEPT_EDITS.
                       En DEFAULT/PLAN/BYPASS se ignora.
    max_risk_score   : Score de riesgo máximo tolerado (0.0–1.0).
                       Si risk > max_risk_score → fuerza ASK aunque mode sea AUTO.
    allowed_tools    : Lista de tool names permitidas explícitamente.
                       None = cualquier tool permitida por el modo.
    description      : Texto libre para documentar la regla en permissions.json.
    """
    pattern: str
    mode: PermissionMode = PermissionMode.DEFAULT
    min_confidence: float = 0.92
    max_risk_score: float = 0.30
    allowed_tools: Optional[List[str]] = None
    description: str = ""

    def matches_path(self, path: str) -> bool:
        """True si path coincide con el glob pattern de la regla."""
        # ✅ FIX: pattern "*" es un catch-all universal — no debe resolverse
        # con os.path.realpath(), que lo trata como un segmento de path
        # relativo al CWD ("/home/.../axioma/*") en vez de "cualquier path".
        # Un resource_path fuera del árbol del proyecto (ej. /tmp/algo)
        # nunca hacía fnmatch contra ese prefijo — la regla catch-all
        # jamás matcheaba para paths externos, cayendo siempre en
        # "sin regla coincidente" en vez de aplicar el mode de la regla.
        # Confirmado con test_rafael_bridge.py: list_files sobre
        # /tmp/rafael_diagnostic_test nunca matcheaba ni el catch-all
        # original ni una regla "plan" agregada para lecturas.
        if self.pattern == "*":
            return True

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 2: Path Traversal mitigado.
        # Se resuelve la ruta real (siguiendo symlinks y ../ ) antes de
        # aplicar el fnmatch, evadiendo reglas basadas en directorio.
        # ═══════════════════════════════════════════════════════════════
        expanded_pattern = os.path.realpath(os.path.expanduser(self.pattern))
        expanded_path = os.path.realpath(os.path.expanduser(path))
        return fnmatch.fnmatch(expanded_path, expanded_pattern)

    def allows_tool(self, tool_name: str) -> bool:
        """True si la regla permite la tool dada (o si no tiene restricción de tools)."""
        if self.allowed_tools is None:
            return True
        return tool_name in self.allowed_tools

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern": self.pattern,
            "mode": self.mode.value,
            "min_confidence": self.min_confidence,
            "max_risk_score": self.max_risk_score,
            "allowed_tools": self.allowed_tools,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PermissionRule":
        return cls(
            pattern=data["pattern"],
            mode=PermissionMode(data.get("mode", "default")),
            min_confidence=float(data.get("min_confidence", 0.92)),
            max_risk_score=float(data.get("max_risk_score", 0.30)),
            allowed_tools=data.get("allowed_tools"),
            description=data.get("description", ""),
        )


# ============================================================================
# EVALUATION RESULT — Resultado detallado de evaluate()
# ============================================================================
@dataclass
class EvaluationResult:
    """
    Resultado detallado de PermissionGateway.evaluate().
    Incluye la decisión y el contexto para logging y trazabilidad.
    """
    decision: Decision
    rule: Optional[PermissionRule]       # Regla que tomó la decisión (None = default)
    reason: str                          # Texto legible del motivo
    confidence: float
    risk_score: float
    tool_name: str
    path: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    @property
    def denied(self) -> bool:
        return self.decision == Decision.DENY

    @property
    def needs_confirmation(self) -> bool:
        return self.decision == Decision.ASK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "risk_score": self.risk_score,
            "tool_name": self.tool_name,
            "path": self.path,
            "rule_pattern": self.rule.pattern if self.rule else None,
            "rule_mode": self.rule.mode.value if self.rule else None,
            "metadata": self.metadata,
        }


# ============================================================================
# WATCHDOG HANDLER — Recarga en caliente
# ============================================================================
class _PermissionsFileHandler(FileSystemEventHandler if _WATCHDOG_AVAILABLE else object):
    """
    Handler de watchdog que notifica al gateway cuando permissions.json cambia.
    Solo activo si watchdog está instalado.
    """
    def __init__(self, gateway: "PermissionGateway") -> None:
        self._gateway = gateway
        if _WATCHDOG_AVAILABLE:
            super().__init__()

    def on_modified(self, event: Any) -> None:
        if _WATCHDOG_AVAILABLE and isinstance(event, FileModifiedEvent):
            target = Path(event.src_path).resolve()
            config_path = self._gateway._config_path.resolve()
            if target == config_path:
                logger.info(f"[PermissionGateway] Detectado cambio en {config_path.name} — recargando reglas")
                self._gateway._reload_rules()


# ============================================================================
# PERMISSION GATEWAY — Evaluador central
# ============================================================================
class PermissionGateway:
    """
    Gateway de permisos para el subsistema de tools de AXIOMA.

    Responsabilidades:
        1. Cargar reglas desde config/permissions.json al inicializar
        2. Recargar reglas en caliente con watchdog si está disponible
        3. Evaluar (path, tool, confidence, risk) → EvaluationResult
        4. Exponer add_rule() / remove_rule() para gestión en runtime

    Thread-safe via RLock. Singleton accesible via get_gateway().

    La evaluación sigue orden de prioridad:
        - Primera regla matching gana (orden del JSON/lista)
        - Si ninguna regla coincide → Decision.ASK (conservador)
        - PLAN mode → DENY para escrituras/ejecución, ALLOW para lectura
        - BYPASS mode → ALLOW siempre (ignorar confianza y riesgo)
        - AUTO/ACCEPT_EDITS → ALLOW si confidence y risk OK, else ASK
        - DEFAULT → ASK siempre

    Integración con PLAN mode y ToolPermission:
        Las tools de solo lectura (READ_ONLY) en modo PLAN son permitidas.
        Todo lo demás (LOCAL_WRITE, EXECUTE, NETWORK, PRIVILEGED) → DENY.
    """

    _instance: Optional["PermissionGateway"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, config_path: Optional[Path] = None) -> "PermissionGateway":
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, config_path: Optional[Path] = None) -> None:
        if self._initialized:
            return

        self._config_path: Path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        self._rules: List[PermissionRule] = []
        self._lock = threading.RLock()
        self._observer: Optional[Any] = None
        self._load_stats: Dict[str, Any] = {
            "last_loaded": None,
            "rules_count": 0,
            "load_errors": 0,
        }

        # Cargar reglas iniciales
        self._reload_rules()

        # Iniciar watchdog si disponible
        self._start_watcher()

        self._initialized = True
        logger.info(
            f"[PermissionGateway] Inicializado — "
            f"{len(self._rules)} reglas desde {self._config_path}"
        )

    # ── Carga de reglas ───────────────────────────────────────────────────────

    def _reload_rules(self) -> None:
        """Carga o recarga reglas desde el archivo de configuración."""
        rules: List[PermissionRule] = []

        if not self._config_path.exists():
            logger.warning(
                f"[PermissionGateway] {self._config_path} no encontrado — "
                "usando reglas vacías (todo pedirá confirmación)"
            )
            with self._lock:
                self._rules = rules
                self._load_stats["last_loaded"] = time.time()
                self._load_stats["rules_count"] = 0
            return

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_rules = data if isinstance(data, list) else data.get("rules", [])
            for raw in raw_rules:
                try:
                    rule = PermissionRule.from_dict(raw)
                    # ═══════════════════════════════════════════════════════════════
                    # ✅ FIX FASE 2: Anti-Bypass en recarga en caliente.
                    # Si el LLM modifica permissions.json para inyectar modo BYPASS,
                    # el gateway lo rechaza y loguea un evento de seguridad crítico.
                    # ═══════════════════════════════════════════════════════════════
                    if rule.mode == PermissionMode.BYPASS:
                        logger.critical(
                            f"[PermissionGateway] SECURITY: Regla con mode='bypass' "
                            f"rechazada en carga/recarga en caliente. Pattern: {rule.pattern}"
                        )
                        self._load_stats["load_errors"] += 1
                        continue

                    rules.append(rule)
                except Exception as e:
                    logger.warning(f"[PermissionGateway] Regla inválida ignorada: {raw} — {e}")
                    self._load_stats["load_errors"] += 1

            with self._lock:
                self._rules = rules
                self._load_stats["last_loaded"] = time.time()
                self._load_stats["rules_count"] = len(rules)

            logger.info(f"[PermissionGateway] {len(rules)} reglas cargadas desde {self._config_path}")

        except json.JSONDecodeError as e:
            logger.error(f"[PermissionGateway] JSON inválido en {self._config_path}: {e}")
            self._load_stats["load_errors"] += 1
        except OSError as e:
            logger.error(f"[PermissionGateway] Error leyendo {self._config_path}: {e}")
            self._load_stats["load_errors"] += 1

    def _start_watcher(self) -> None:
        """Inicia watchdog para recarga en caliente. Silencioso si no disponible."""
        if not _WATCHDOG_AVAILABLE:
            logger.debug("[PermissionGateway] watchdog no disponible — sin recarga en caliente")
            return

        try:
            watch_dir = str(self._config_path.parent.resolve())
            handler = _PermissionsFileHandler(self)
            self._observer = Observer()
            self._observer.schedule(handler, path=watch_dir, recursive=False)
            self._observer.daemon = True
            self._observer.start()
            logger.debug(f"[PermissionGateway] Watchdog activo en {watch_dir}")
        except Exception as e:
            logger.warning(f"[PermissionGateway] No se pudo iniciar watchdog: {e}")

    # ── Evaluación ────────────────────────────────────────────────────────────

    def evaluate(
        self,
        path: str,
        tool_name: str,
        confidence: float,
        risk_score: float,
        tool_permission: ToolPermission = ToolPermission.READ_ONLY,
    ) -> EvaluationResult:
        """
        Evalúa si una acción está permitida.

        Args:
            path            : Path afectado por la acción (archivo, directorio, URL).
            tool_name       : Nombre de la tool que va a ejecutarse.
            confidence      : Confianza del clasificador (0.0–1.0).
            risk_score      : Score de riesgo estimado (0.0–1.0).
            tool_permission : Nivel ToolPermission de la tool (para modo PLAN).

        Returns:
            EvaluationResult con Decision.ALLOW | DENY | ASK + contexto completo.
        """
        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 2: Clampeo de inputs numéricos.
        # Evita comportamientos impredecibles si los valores están fuera de rango.
        # ═══════════════════════════════════════════════════════════════
        confidence = max(0.0, min(1.0, confidence))
        risk_score = max(0.0, min(1.0, risk_score))

        with self._lock:
            rules_snapshot = list(self._rules)

        matching_rule: Optional[PermissionRule] = None
        for rule in rules_snapshot:
            # ═══════════════════════════════════════════════════════════
            # ✅ FASE 1 (plan_harness): las reglas DENY matchean por pattern
            # ANTES de filtrar por tool. El whitelist de allowed_tools de una
            # regla DENY se aplica DENTRO de _apply_mode (permite las tools
            # explícitas, deniega el resto). Si filtráramos por tool acá,
            # una regla DENY con allowed_tools=[fs_read,fs_list] nunca
            # matchearía una fs_write y la escritura caería al catch-all
            # (ASK) en vez de DENY — exactamente lo que la regla prohíbe.
            # ═══════════════════════════════════════════════════════════
            if rule.mode == PermissionMode.DENY:
                if rule.matches_path(path):
                    matching_rule = rule
                    break
            elif rule.matches_path(path) and rule.allows_tool(tool_name):
                matching_rule = rule
                break

        # Sin regla coincidente → conservador: ASK
        if matching_rule is None:
            return EvaluationResult(
                decision=Decision.ASK,
                rule=None,
                reason="Sin regla coincidente para path+tool — se requiere confirmación",
                confidence=confidence,
                risk_score=risk_score,
                tool_name=tool_name,
                path=path,
                metadata={"no_rule_match": True},
            )

        return self._apply_mode(
            rule=matching_rule,
            tool_name=tool_name,
            path=path,
            confidence=confidence,
            risk_score=risk_score,
            tool_permission=tool_permission,
        )

    def _apply_mode(
        self,
        rule: PermissionRule,
        tool_name: str,
        path: str,
        confidence: float,
        risk_score: float,
        tool_permission: ToolPermission,
    ) -> EvaluationResult:
        """Aplica la lógica del PermissionMode de la regla."""
        mode = rule.mode

        # ── BYPASS: sin restricciones ─────────────────────────────────────────
        if mode == PermissionMode.BYPASS:
            return EvaluationResult(
                decision=Decision.ALLOW,
                rule=rule,
                reason="BYPASS: sin restricciones (solo dev)",
                confidence=confidence,
                risk_score=risk_score,
                tool_name=tool_name,
                path=path,
                metadata={"bypass": True},
            )

        # ── PLAN: solo lectura permitida ──────────────────────────────────────
        if mode == PermissionMode.PLAN:
            if tool_permission == ToolPermission.READ_ONLY:
                return EvaluationResult(
                    decision=Decision.ALLOW,
                    rule=rule,
                    reason="PLAN: operación de lectura permitida",
                    confidence=confidence,
                    risk_score=risk_score,
                    tool_name=tool_name,
                    path=path,
                )
            return EvaluationResult(
                decision=Decision.DENY,
                rule=rule,
                reason=f"PLAN: operación {tool_permission.name} no permitida en modo planificación",
                confidence=confidence,
                risk_score=risk_score,
                tool_name=tool_name,
                path=path,
                metadata={"plan_mode_blocked": True, "permission_level": tool_permission.name},
            )

        # ── DENY: denegar todo excepto whitelist explícito ──────────────────
        # ✅ FASE 1 (plan_harness): reglas "NUNCA tocar" para rutas críticas
        # (.git, .env, config, data). Solo las tools listadas en
        # allowed_tools pasan (típicamente fs_read/fs_list); cualquier otra
        # tool sobre el pattern → DENY, sin preguntar.
        if mode == PermissionMode.DENY:
            if rule.allowed_tools is not None and tool_name in rule.allowed_tools:
                return EvaluationResult(
                    decision=Decision.ALLOW,
                    rule=rule,
                    reason=f"DENY whitelist: tool '{tool_name}' permitida explícitamente",
                    confidence=confidence,
                    risk_score=risk_score,
                    tool_name=tool_name,
                    path=path,
                    metadata={"deny_whitelisted": True, "permission_level": tool_permission.name},
                )
            return EvaluationResult(
                decision=Decision.DENY,
                rule=rule,
                reason=f"DENY: operación {tool_name} prohibida sobre {path}",
                confidence=confidence,
                risk_score=risk_score,
                tool_name=tool_name,
                path=path,
                metadata={"deny_rule": True, "permission_level": tool_permission.name},
            )

        # ── DEFAULT: siempre preguntar ────────────────────────────────────────
        if mode == PermissionMode.DEFAULT:
            return EvaluationResult(
                decision=Decision.ASK,
                rule=rule,
                reason="DEFAULT: se requiere confirmación del usuario",
                confidence=confidence,
                risk_score=risk_score,
                tool_name=tool_name,
                path=path,
            )

        # ── AUTO / ACCEPT_EDITS: evaluar confianza y riesgo ──────────────────
        # Primero chequear riesgo (tiene prioridad sobre confianza)
        if risk_score > rule.max_risk_score:
            return EvaluationResult(
                decision=Decision.ASK,
                rule=rule,
                reason=(
                    f"Riesgo {risk_score:.2f} supera umbral {rule.max_risk_score:.2f} — "
                    "se requiere confirmación"
                ),
                confidence=confidence,
                risk_score=risk_score,
                tool_name=tool_name,
                path=path,
                metadata={"risk_exceeded": True},
            )

        # Verificar confianza mínima
        if confidence < rule.min_confidence:
            return EvaluationResult(
                decision=Decision.ASK,
                rule=rule,
                reason=(
                    f"Confianza {confidence:.2f} por debajo del umbral {rule.min_confidence:.2f} — "
                    "se requiere confirmación"
                ),
                confidence=confidence,
                risk_score=risk_score,
                tool_name=tool_name,
                path=path,
                metadata={"low_confidence": True},
            )

        # ACCEPT_EDITS en modo PLAN: igual que PLAN
        if mode == PermissionMode.ACCEPT_EDITS and tool_permission > ToolPermission.LOCAL_WRITE:
            return EvaluationResult(
                decision=Decision.ASK,
                rule=rule,
                reason=f"ACCEPT_EDITS: permiso {tool_permission.name} requiere confirmación",
                confidence=confidence,
                risk_score=risk_score,
                tool_name=tool_name,
                path=path,
                metadata={"accept_edits_escalation": True},
            )

        return EvaluationResult(
            decision=Decision.ALLOW,
            rule=rule,
            reason=f"{mode.value}: confianza y riesgo dentro de umbrales",
            confidence=confidence,
            risk_score=risk_score,
            tool_name=tool_name,
            path=path,
        )

    # ── Gestión de reglas en runtime ──────────────────────────────────────────

    def add_rule(self, rule: PermissionRule, persist: bool = True) -> None:
        """
        Agrega una regla al inicio de la lista (mayor prioridad).
        Si persist=True, guarda en permissions.json.
        """
        # ✅ FIX FASE 2: Anti-Bypass runtime
        if rule.mode == PermissionMode.BYPASS:
            logger.critical(f"[PermissionGateway] SECURITY: Intento de agregar regla BYPASS en runtime bloqueado. Pattern: {rule.pattern}")
            return

        with self._lock:
            self._rules.insert(0, rule)
            if persist:
                self._save_rules()
        logger.info(f"[PermissionGateway] Regla agregada: pattern={rule.pattern!r} mode={rule.mode.value}")

    def remove_rule(self, pattern: str, persist: bool = True) -> bool:
        """
        Elimina la primera regla con el pattern dado.
        Retorna True si se encontró y eliminó.
        """
        with self._lock:
            for i, rule in enumerate(self._rules):
                if rule.pattern == pattern:
                    self._rules.pop(i)
                    if persist:
                        self._save_rules()
                    logger.info(f"[PermissionGateway] Regla eliminada: pattern={pattern!r}")
                    return True
        logger.warning(f"[PermissionGateway] Regla no encontrada: pattern={pattern!r}")
        return False

    def list_rules(self) -> List[Dict[str, Any]]:
        """Retorna lista serializable de todas las reglas activas."""
        with self._lock:
            return [r.to_dict() for r in self._rules]

    def _save_rules(self) -> None:
        """Persiste las reglas actuales en el archivo de configuración."""
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"rules": [r.to_dict() for r in self._rules]}
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug(f"[PermissionGateway] Reglas persistidas en {self._config_path}")
        except OSError as e:
            logger.error(f"[PermissionGateway] Error guardando reglas: {e}")

    # ── Introspección ─────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "rules_count": len(self._rules),
                "config_path": str(self._config_path),
                "watchdog_active": self._observer is not None and _WATCHDOG_AVAILABLE,
                **self._load_stats,
            }

    def stop(self) -> None:
        """Detiene el watchdog. Llamar en shutdown del proceso."""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 permissions.py:658] excepción degradada (intencional): {_d2e}")
        logger.debug("[PermissionGateway] Detenido")


# ============================================================================
# SINGLETON ACCESSOR
# ============================================================================
_gateway: Optional[PermissionGateway] = None
_gateway_lock = threading.Lock()


def get_gateway(config_path: Optional[Path] = None) -> PermissionGateway:
    """
    Retorna la instancia singleton del PermissionGateway.
    Thread-safe. Uso desde Dispatcher y ToolExecutor:

        from src.tools_mcp.permissions import get_gateway, Decision

        result = get_gateway().evaluate(
            path="src/core/dispatcher.py",
            tool_name="fs_write",
            confidence=0.95,
            risk_score=0.15,
            tool_permission=ToolPermission.LOCAL_WRITE,
        )
        if result.denied:
            return ToolResult.fail(error=result.reason, ...)
        if result.needs_confirmation:
            # pausar y preguntar al usuario
    """
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                _gateway = PermissionGateway(config_path=config_path)
    return _gateway


# ============================================================================
# TEMPLATE CONFIG — Para generar config/permissions.json si no existe
# ============================================================================
PERMISSIONS_TEMPLATE: Dict[str, Any] = {
    "rules": [
        {
            "pattern": "src/**",
            "mode": "acceptEdits",
            "min_confidence": 0.92,
            "max_risk_score": 0.30,
            "allowed_tools": None,
            "description": "Directorio de código fuente — aprueba ediciones con alta confianza",
        },
        {
            "pattern": "tests/**",
            "mode": "acceptEdits",
            "min_confidence": 0.85,
            "max_risk_score": 0.40,
            "allowed_tools": None,
            "description": "Tests — más permisivo para iteración rápida",
        },
        {
            "pattern": "config/**",
            "mode": "default",
            "min_confidence": 0.95,
            "max_risk_score": 0.20,
            "allowed_tools": None,
            "description": "Configuración — siempre pide confirmación",
        },
        {
            "pattern": "~/.config/**",
            "mode": "default",
            "min_confidence": 0.99,
            "max_risk_score": 0.10,
            "allowed_tools": ["fs_read"],
            "description": "Config de usuario — solo lectura sin confirmación, escritura siempre pregunta",
        },
        {
            "pattern": "*",
            "mode": "default",
            "min_confidence": 0.92,
            "max_risk_score": 0.30,
            "allowed_tools": None,
            "description": "Catch-all — cualquier path no clasificado pide confirmación",
        },
    ]
}


def generate_default_config(output_path: Optional[Path] = None) -> Path:
    """
    Genera config/permissions.json con el template por defecto.
    Útil para primer setup del proyecto.

    Returns:
        Path del archivo generado.
    """
    target = output_path or _DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(PERMISSIONS_TEMPLATE, f, indent=2, ensure_ascii=False)
    logger.info(f"[PermissionGateway] Config generada en {target}")
    return target
