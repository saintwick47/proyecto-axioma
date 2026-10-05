#!/usr/bin/env python3

# ═══════════════════════════════════════════════════════════════
# AXIOMA — Filtro de Archivos y Contenido Sensible v1.0
# Ruta: tools/documentador/sensitive_filter.py
# Autor: SaintWick
# Propósito: Evitar que secrets (tokens, keys, credenciales) lleguen
#            a la documentación generada (.md / .toon).
# ═══════════════════════════════════════════════════════════════

import re
import fnmatch
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════
# ESTRUCTURAS
# ═══════════════════════════════════════════════════════════════

@dataclass
class SecretMatch:
    """Un secret detectado en un contenido."""
    pattern_name: str
    start: int
    end: int
    original: str
    replacement: str


@dataclass
class FilterDecision:
    """Decisión del filtro sobre un archivo."""
    is_sensitive: bool
    reason: str = ""
    matched_pattern: Optional[str] = None
    file_metadata: Dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# FILTRO DE ARCHIVOS SENSIBLES
# ═══════════════════════════════════════════════════════════════

class SensitiveFileFilter:
    """
    Decide si un archivo debe ocultarse o si su contenido debe
    ser redactado antes de pasar al documentador.

    Carga su configuración desde sensitive_patterns.yaml.
    Si el archivo no existe, usa defaults seguros.
    """

    DEFAULT_SENSITIVE_FILES = [
        "tools/push.py",
        "tools/commit.py",
        ".env",
        ".env.*",
        "**/.env",
        "**/secrets.*",
        "**/credentials.*",
        "**/*.key",
        "**/*.pem",
        "**/id_rsa",
        "**/id_ed25519",
        "**/.netrc",
    ]

    DEFAULT_EXCLUDED = [
        ".git/**",
        "data/**",
        "logs/**",
        "**/__pycache__/**",
        "**/*.pyc",
    ]

    DEFAULT_SECRET_PATTERNS = [
        ("GitHub PAT (classic)", r'ghp_[A-Za-z0-9]{20,}', "[REDACTED_GITHUB_PAT]"),
        ("GitHub Fine-Grained", r'github_pat_[A-Za-z0-9_]{20,}',
         "[REDACTED_GITHUB_FG_PAT]"),
        ("OpenAI API Key", r'sk-(proj-)?[A-Za-z0-9]{20,}', "[REDACTED_OPENAI_KEY]"),
        ("Anthropic API Key", r'sk-ant-[A-Za-z0-9-]{20,}', "[REDACTED_ANTHROPIC_KEY]"),
        ("Google API Key", r'AIza[0-9A-Za-z_-]{35}', "[REDACTED_GOOGLE_KEY]"),
        ("AWS Access Key", r'AKIA[0-9A-Z]{16}', "[REDACTED_AWS_KEY]"),
        ("Tavily API Key", r'tvly-[A-Za-z0-9]{20,}', "[REDACTED_TAVILY]"),
        ("Serper API Key", r'serper-[A-Za-z0-9]{20,}', "[REDACTED_SERPER]"),
        ("PEM Private Key", r'-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
         "[REDACTED_PEM_BLOCK]"),
        ("Generic Bearer", r'(?i)(authorization|bearer|api[_-]?key)["\']?[ \t]*[:=][ \t]*(?!["\']?(?:settings|self|os|config|cls|app|env)\.)["\']?(?:bearer[ \t]+)?[A-Za-z0-9._\-]{20,}',
         "[REDACTED_BEARER]"),
    ]

    def __init__(self, config_path: Optional[Path] = None,
                 project_root: Optional[Path] = None):
        self.logger = logging.getLogger("axioma.sensitive_filter")
        self.project_root = project_root
        self.sensitive_files: List[str] = list(self.DEFAULT_SENSITIVE_FILES)
        self.excluded_from_scan: List[str] = list(self.DEFAULT_EXCLUDED)
        self.secret_patterns: List[Tuple[str, re.Pattern, str]] = []
        self.placeholder = "[🔐 ARCHIVO SENSIBLE — CONTENIDO RESTRINGIDO]"
        self.audit_log_path: Optional[Path] = None
        self.fail_on_secret_found = True

        # Cargar config si existe
        if config_path and config_path.exists():
            self._load_config(config_path)
        else:
            # Cargar defaults compilados
            self._compile_default_patterns()

    def _load_config(self, path: Path):
        if not YAML_AVAILABLE:
            self.logger.warning("PyYAML no disponible, usando defaults")
            self._compile_default_patterns()
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            self.logger.error(f"Error leyendo {path}: {e}, usando defaults")
            self._compile_default_patterns()
            return

        self.sensitive_files = cfg.get("sensitive_files", self.DEFAULT_SENSITIVE_FILES)
        self.excluded_from_scan = cfg.get("excluded_from_scan", self.DEFAULT_EXCLUDED)

        sm = cfg.get("sensitive_file_metadata", {})
        self.placeholder = sm.get("placeholder", self.placeholder)

        audit = cfg.get("audit", {})
        self.fail_on_secret_found = audit.get("fail_on_secret_found", True)
        audit_log = audit.get("log_file")
        if audit_log and self.project_root:
            self.audit_log_path = self.project_root / audit_log

        # Compilar patrones
        self.secret_patterns = []
        for entry in cfg.get("secret_patterns", []):
            try:
                pat = re.compile(entry["pattern"])
                self.secret_patterns.append((entry["name"], pat, entry["replacement"]))
            except (KeyError, re.error) as e:
                self.logger.warning(f"Patrón inválido {entry.get('name')}: {e}")

    def _compile_default_patterns(self):
        self.secret_patterns = [
            (name, re.compile(pat), repl)
            for name, pat, repl in self.DEFAULT_SECRET_PATTERNS
        ]

    # ─── Decisiones sobre archivos ─────────────────────────────────

    def is_sensitive_file(self, file_path: Path) -> FilterDecision:
        """Devuelve FilterDecision indicando si el archivo es sensible."""
        try:
            rel = str(file_path.resolve().relative_to(self.project_root.resolve())) \
                if self.project_root else str(file_path)
        except ValueError:
            rel = str(file_path)

        rel_posix = rel.replace("\\", "/")

        for pattern in self.sensitive_files:
            if self._matches_glob(rel_posix, pattern):
                return FilterDecision(
                    is_sensitive=True,
                    reason=f"Match con patrón sensible: {pattern}",
                    matched_pattern=pattern,
                    file_metadata={"relative_path": rel_posix}
                )

        for pattern in self.excluded_from_scan:
            if self._matches_glob(rel_posix, pattern):
                return FilterDecision(
                    is_sensitive=True,
                    reason=f"Archivo excluido del escaneo: {pattern}",
                    matched_pattern=pattern,
                    file_metadata={"relative_path": rel_posix}
                )

        return FilterDecision(is_sensitive=False, file_metadata={"relative_path": rel_posix})

    def should_skip_scan(self, file_path: Path) -> bool:
        """True si el archivo ni siquiera debe leerse."""
        return self.is_sensitive_file(file_path).is_sensitive

    def _matches_glob(self, path: str, pattern: str) -> bool:
        """
        Compara una ruta (en formato POSIX) con un patrón glob.
        Soporta '**' (cualquier número de directorios) reemplazándolo
        por '*' para simular el comportamiento con fnmatch.
        """
        pat = pattern.replace("\\", "/")
        # Para manejar **, convertimos a * y probamos con y sin prefijo **/
        if "**" in pat:
            # Reemplazar ** por * (no es exacto pero cubre la mayoría de casos)
            simple_pat = pat.replace("**", "*")
            return fnmatch.fnmatch(path, simple_pat) or fnmatch.fnmatch(path, f"**/{simple_pat}")
        return fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, f"**/{pat}")

    # ─── Redacción de contenido ────────────────────────────────────

    def redact_secrets(self, content: str) -> Tuple[str, List[SecretMatch]]:
        """
        Aplica todos los patrones de secret_patterns al contenido.
        Retorna el contenido redactado y la lista de matches.
        """
        if not content:
            return content, []

        matches: List[SecretMatch] = []
        redacted = content

        for name, pattern, replacement in self.secret_patterns:
            def _sub(m):
                sm = SecretMatch(
                    pattern_name=name,
                    start=m.start(),
                    end=m.end(),
                    original=m.group(0),
                    replacement=replacement,
                )
                matches.append(sm)
                return replacement
            redacted = pattern.sub(_sub, redacted)

        if matches and self.audit_log_path:
            self._log_audit(matches)

        return redacted, matches

    def _log_audit(self, matches: List[SecretMatch]):
        """Registra los secrets detectados en el audit log."""
        if not self.audit_log_path:
            return

        try:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                from datetime import datetime
                ts = datetime.now().isoformat()
                for m in matches:
                    f.write(f"[{ts}] REDACTED | {m.pattern_name} "
                            f"| range=({m.start},{m.end})\n")
        except Exception as e:
            self.logger.warning(f"No se pudo escribir audit log: {e}")

    # ─── Redacción específica para constantes ─────────────────────

    def safe_constant_value(self, name: str, value: str) -> str:
        """
        Redacta el valor de una constante si su nombre sugiere que es sensible.
        Ej: GITHUB_TOKEN, *_KEY, *_SECRET, etc.
        """
        if not value:
            return value

        name_upper = name.upper()
        sensitive_name_hints = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

        # Si el nombre de la constante sugiere sensibilidad Y el valor parece token
        if any(h in name_upper for h in sensitive_name_hints):
            # Si ya matchea un patrón, redactar
            redacted, matches = self.redact_secrets(value)
            if matches:
                return redacted
            # Si el nombre es sensible pero el valor no parece token, truncar
            if len(value) > 8:
                return f"{value[:4]}***{value[-4:]} [REDACTED_BY_NAME]"
            return "[REDACTED]"

        # Si no, aplicar redacción general por si acaso
        redacted, _ = self.redact_secrets(value)
        return redacted

    # ─── Metadatos seguros para archivos sensibles ────────────────

    def safe_file_metadata(self, file_path: Path, original_purpose: str = "") -> Dict[str, Any]:
        """
        Genera metadatos seguros para mostrar en la doc cuando un archivo
        es sensible. Reemplaza el contenido por el placeholder.
        """
        decision = self.is_sensitive_file(file_path)
        return {
            "is_sensitive": True,
            "placeholder": self.placeholder,
            "reason": decision.reason,
            "matched_pattern": decision.matched_pattern,
            "original_purpose": original_purpose,  # puede quedar si es genérico
        }


# ═══════════════════════════════════════════════════════════════
# UTILIDAD DE USO RÁPIDO
# ═══════════════════════════════════════════════════════════════

def make_default_filter(project_root: Optional[Path] = None) -> SensitiveFileFilter:
    """
    Crea el filtro con la config estándar del proyecto AXIOMA.
    Busca sensitive_patterns.yaml en tools/documentador/.
    """
    candidates = [
        Path(__file__).parent / "sensitive_patterns.yaml",
        Path.cwd() / "tools" / "documentador" / "sensitive_patterns.yaml",
    ]
    config_path = next((p for p in candidates if p.exists()), None)
    return SensitiveFileFilter(config_path=config_path, project_root=project_root)


if __name__ == "__main__":
    # Test rápido
    f = make_default_filter()
    print(f"Filtro cargado con {len(f.secret_patterns)} patrones de secretos")
    print(f"Archivos sensibles por default: {len(f.sensitive_files)}")

    # Test de redacción
    test = 'GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"'
    redacted, matches = f.redact_secrets(test)
    print(f"\nTest:\n  Original: {test}\n  Redacted: {redacted}\n  Matches:  {len(matches)}")
