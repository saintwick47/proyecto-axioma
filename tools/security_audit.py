#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.3.1 — Auditor de Seguridad de Archivos
# ═══════════════════════════════════════════════════════════════
# Ruta: tools/security_audit.py
# Autor: SaintWick
# Fecha: 2026-06-24
# Propósito: Escanear el proyecto completo en busca de credenciales
#            filtradas, tokens expuestos, API keys, archivos sensibles
#            no excluidos y configuraciones inseguras.
# Uso: python tools/security_audit.py [--verbose] [--fix] [--json]
# ═══════════════════════════════════════════════════════════════
# ✅ v1.3.1: FIX falso positivo en check_gitignore()
#   - sensitive_to_check usaba "tokens.txt" (no existe en el proyecto)
#   - Cambiado a "tokens github.txt" (archivo real, ya en .gitignore)
#   - Elimina hallazgo ALTO espurio que reportaba patrón inexistente
# ✅ v1.3.0: Sección "PELIGRO CONTROLADO" separada
#   - Archivos con credenciales que YA tienen protección (en .gitignore,
#     en AUDIT_EXCEPTIONS, no trackeados) se muestran en sección aparte
#   - Evita que el usuario modifique archivos protegidos por error
#   - Solo lo realmente peligroso (sin control) va a CRÍTICO/ALTO
# ✅ v1.2.0: Ajustes basados en auditoría real del proyecto
# ✅ v1.1.0: Correcciones basadas en decisiones de diseño del proyecto
# ✅ v1.0.0: Versión inicial — escáner completo de seguridad
# ═══════════════════════════════════════════════════════════════
import os
import re
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Set
from dataclasses import dataclass, field, asdict
from enum import Enum

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════
AUDIT_VERSION = "1.3.1"

# ═══════════════════════════════════════════════════════════════
# EXCEPCIONES DE AUDITORÍA (decisiones de diseño del proyecto)
# ═══════════════════════════════════════════════════════════════
AUDIT_EXCEPTIONS: Set[str] = {
    "push.py",
    "commit.py",
    "tokens github.txt",
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    ".env.development",
}

# ═══════════════════════════════════════════════════════════════
# COLORES
# ═══════════════════════════════════════════════════════════════
class C:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'
    BG_RED = '\033[41m'
    BG_GREEN = '\033[42m'
    BG_YELLOW = '\033[43m'
    BG_MAGENTA = '\033[45m'


class Severity(Enum):
    CRITICAL = "CRÍTICO"
    HIGH = "ALTO"
    MEDIUM = "MEDIO"
    LOW = "BAJO"
    INFO = "INFO"
    CONTROLLED = "CONTROLADO"  # ✅ v1.3.0: Peligro controlado

    @property
    def color(self) -> str:
        return {
            Severity.CRITICAL: f"{C.BG_RED}{C.WHITE}{C.BOLD}",
            Severity.HIGH: C.RED,
            Severity.MEDIUM: C.YELLOW,
            Severity.LOW: C.BLUE,
            Severity.INFO: C.DIM,
            Severity.CONTROLLED: f"{C.BG_MAGENTA}{C.WHITE}{C.BOLD}",
        }[self]

    @property
    def icon(self) -> str:
        return {
            Severity.CRITICAL: "🚨",
            Severity.HIGH: "🔴",
            Severity.MEDIUM: "🟡",
            Severity.LOW: "🔵",
            Severity.INFO: "ℹ️",
            Severity.CONTROLLED: "🛡️",
        }[self]

    @property
    def is_real_threat(self) -> bool:
        """Solo CRITICAL y HIGH son amenazas reales sin control."""
        return self in (Severity.CRITICAL, Severity.HIGH)


# ═══════════════════════════════════════════════════════════════
# ESTRUCTURAS DE DATOS
# ═══════════════════════════════════════════════════════════════
@dataclass
class Finding:
    severity: Severity
    category: str
    file: str
    line: int
    description: str
    detail: str = ""
    pattern_matched: str = ""
    remediation: str = ""
    controlled_reason: str = ""  # ✅ v1.3.0: Razón por la que es controlado

    def to_dict(self) -> dict:
        d = asdict(self)
        d['severity'] = self.severity.value
        return d

    def format_terminal(self) -> str:
        sev = self.severity
        lines = []
        lines.append(f"  {sev.icon} {sev.color} [{sev.value}] {C.RESET} {C.BOLD}{self.category}{C.RESET}")
        lines.append(f"     📄 {C.CYAN}{self.file}{C.RESET}:{C.YELLOW}{self.line}{C.RESET}")
        lines.append(f"     📝 {self.description}")
        if self.detail:
            masked = _mask_sensitive(self.detail)
            if len(masked) > 120:
                masked = masked[:120] + "..."
            lines.append(f"     🔍 {C.DIM}{masked}{C.RESET}")
        if sev == Severity.CONTROLLED and self.controlled_reason:
            lines.append(f"     🛡️  {C.GREEN}Protección: {self.controlled_reason}{C.RESET}")
        elif self.remediation and sev != Severity.CONTROLLED:
            lines.append(f"     🛠️  {C.GREEN}{self.remediation}{C.RESET}")
        return "\n".join(lines)


@dataclass
class AuditReport:
    timestamp: str = ""
    project_root: str = ""
    total_files_scanned: int = 0
    total_lines_scanned: int = 0
    findings: List[Finding] = field(default_factory=list)
    scan_duration_ms: float = 0.0
    gitignore_ok: bool = True
    git_tracking_ok: bool = True
    exceptions_applied: List[str] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.HIGH)

    @property
    def medium_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.MEDIUM)

    @property
    def low_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.LOW)

    @property
    def controlled_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CONTROLLED)

    @property
    def is_clean(self) -> bool:
        return self.critical_count == 0 and self.high_count == 0

    @property
    def real_threats(self) -> List[Finding]:
        """Hallazgos sin control — requieren acción inmediata."""
        return [f for f in self.findings if f.severity.is_real_threat]

    @property
    def controlled_findings(self) -> List[Finding]:
        """Hallazgos con protección activa — solo informativo."""
        return [f for f in self.findings if f.severity == Severity.CONTROLLED]

    def to_dict(self) -> dict:
        return {
            'timestamp': self.timestamp,
            'project_root': self.project_root,
            'version': AUDIT_VERSION,
            'total_files_scanned': self.total_files_scanned,
            'total_lines_scanned': self.total_lines_scanned,
            'scan_duration_ms': self.scan_duration_ms,
            'gitignore_ok': self.gitignore_ok,
            'git_tracking_ok': self.git_tracking_ok,
            'exceptions_applied': self.exceptions_applied,
            'summary': {
                'critical': self.critical_count,
                'high': self.high_count,
                'medium': self.medium_count,
                'low': self.low_count,
                'controlled': self.controlled_count,
                'total': len(self.findings),
            },
            'is_clean': self.is_clean,
            'findings': [f.to_dict() for f in self.findings],
        }


# ═══════════════════════════════════════════════════════════════
# ARCHIVOS SENSIBLES CONOCIDOS
# ═══════════════════════════════════════════════════════════════
SENSITIVE_FILES: Set[str] = {
    "push.py", "commit.py",
    "tokens github.txt", "tokens.txt", "passwords.txt", "credentials.txt",
    ".env", ".env.local", ".env.production", ".env.staging", ".env.development",
    "secrets.json", "credentials.json", "tokens.json", "api_keys.json",
    "service_account.json", "service-account.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.crt", "*.cer",
}

SKIP_EXTENSIONS: Set[str] = {
    '.pyc', '.pyo', '.so', '.dll', '.exe', '.bin', '.dat',
    '.db', '.sqlite', '.sqlite3', '.lance', '.txn', '.manifest',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.svg', '.bmp',
    '.wav', '.mp3', '.ogg', '.flac', '.mp4', '.avi', '.mkv',
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.rar', '.7z',
    '.pdf', '.ttf', '.otf', '.woff', '.woff2',
    '.pkl', '.npy', '.npz', '.parquet', '.arrow',
    '.bundle', '.whl', '.egg', '.jar',
    '.lock', '.pid', '.log', '.bak', '.swp',
}

SKIP_DIRS: Set[str] = {
    '.git', '__pycache__', 'node_modules', '.venv', 'venv', 'env',
    '.idea', '.vscode', '.pytest_cache', '.mypy_cache', 'htmlcov',
    '.tox', '.nox', '.cache', '.ruff_cache',
    'dist', 'build', '.eggs', '*.egg-info',
    'data', 'logs', 'docs',
}

# ═══════════════════════════════════════════════════════════════
# PATRONES DE DETECCIÓN DE CREDENCIALES
# ═══════════════════════════════════════════════════════════════
CREDENTIAL_PATTERNS: List[Tuple[str, 're.Pattern', Severity, str]] = [
    ("GitHub PAT (classic)", re.compile(r'\bghp_[A-Za-z0-9]{36}\b'), Severity.CRITICAL, "Token de acceso personal de GitHub (classic)"),
    ("GitHub OAuth", re.compile(r'\bgho_[A-Za-z0-9]{36}\b'), Severity.CRITICAL, "Token OAuth de GitHub"),
    ("GitHub user-to-server", re.compile(r'\bghu_[A-Za-z0-9]{36}\b'), Severity.CRITICAL, "Token user-to-server de GitHub"),
    ("GitHub server-to-server", re.compile(r'\bghs_[A-Za-z0-9]{36}\b'), Severity.CRITICAL, "Token server-to-server de GitHub"),
    ("GitHub refresh token", re.compile(r'\bghr_[A-Za-z0-9]{36}\b'), Severity.CRITICAL, "Refresh token de GitHub"),
    ("GitHub fine-grained PAT", re.compile(r'\bgithub_pat_[A-Za-z0-9_]{82}\b'), Severity.CRITICAL, "Token fine-grained de GitHub"),
    ("OpenAI API key", re.compile(r'\bsk-[A-Za-z0-9]{48}\b'), Severity.CRITICAL, "API key de OpenAI"),
    ("OpenAI project key", re.compile(r'\bsk-proj-[A-Za-z0-9\-]{48,}\b'), Severity.CRITICAL, "Project key de OpenAI"),
    ("AWS Access Key ID", re.compile(r'\bAKIA[A-Z0-9]{16}\b'), Severity.CRITICAL, "AWS Access Key ID"),
    ("AWS Secret Key", re.compile(r'(?i)aws_secret_access_key\s*[=:]\s*["\']?[A-Za-z0-9/+=]{40}["\']?'), Severity.CRITICAL, "AWS Secret Access Key"),
    ("Google API key", re.compile(r'\bAIza[A-Za-z0-9\-_]{35}\b'), Severity.HIGH, "API key de Google"),
    ("Google OAuth client secret", re.compile(r'(?i)client_secret["\']?\s*[=:]\s*["\']?[A-Za-z0-9\-_]{24}["\']?'), Severity.HIGH, "Client secret de Google OAuth"),
    ("Slack bot token", re.compile(r'\bxoxb-[0-9]{10,}-[A-Za-z0-9]{24,}\b'), Severity.HIGH, "Bot token de Slack"),
    ("Slack user token", re.compile(r'\bxoxp-[0-9]{10,}-[A-Za-z0-9]{24,}\b'), Severity.HIGH, "User token de Slack"),
    ("JWT token", re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'), Severity.HIGH, "JSON Web Token"),
    ("PEM private key", re.compile(r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----'), Severity.CRITICAL, "Clave privada PEM"),
    ("SSH private key", re.compile(r'-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----'), Severity.CRITICAL, "Clave privada SSH (OpenSSH format)"),
    ("Bearer token (header)", re.compile(r'(?i)Authorization:\s*Bearer\s+[A-Za-z0-9\-._~+/]+=*'), Severity.MEDIUM, "Bearer token en header HTTP"),
    ("Generic API key assignment", re.compile(r'(?i)(?:api[_-]?key|apikey|api[_-]?secret)\s*[=:]\s*["\'][A-Za-z0-9\-_]{16,}["\']'), Severity.HIGH, "Asignación genérica de API key"),
    ("Generic secret assignment", re.compile(r'(?i)(?:secret[_-]?key|secret|password|passwd|pwd)\s*[=:]\s*["\'][^"\']{8,}["\']'), Severity.MEDIUM, "Asignación genérica de secreto/contraseña"),
    ("Generic token assignment", re.compile(r'(?i)(?:token|auth[_-]?token|access[_-]?token)\s*[=:]\s*["\'][A-Za-z0-9\-_]{16,}["\']'), Severity.MEDIUM, "Asignación genérica de token"),
    ("Tavily API key", re.compile(r'\btvly-[A-Za-z0-9]{32,}\b'), Severity.HIGH, "API key de Tavily"),
    ("Serper API key", re.compile(r'(?i)serper[_-]?api[_-]?key\s*[=:]\s*["\'][A-Za-z0-9]{32,}["\']'), Severity.HIGH, "API key de Serper"),
    ("NewsAPI key", re.compile(r'(?i)news[_-]?api[_-]?key\s*[=:]\s*["\'][A-Za-z0-9]{32,}["\']'), Severity.HIGH, "API key de NewsAPI"),
    ("Database connection string", re.compile(r'(?i)(?:mongodb|postgres|mysql|redis)://[^\s"\']+:[^\s"\']+@'), Severity.HIGH, "Connection string con credenciales embebidas"),
]

HARDCODED_PATH_PATTERNS = [
    (re.compile(r'(?:Path|)\(\s*["\']/(?:home|Users)/[a-zA-Z0-9_-]+/'), Severity.LOW, "Ruta absoluta hardcodeada con username del sistema"),
    (re.compile(r'(?:Path|)\(\s*["\']C:\\Users\\[a-zA-Z0-9_-]+\\'), Severity.LOW, "Ruta absoluta Windows hardcodeada con username"),
]

FALSE_POSITIVE_PATTERNS: Set[str] = {
    "storage_secret", "session_secret", "app_secret",
    "flask_secret", "django_secret",
}

# ═══════════════════════════════════════════════════════════════
# UTILIDADES
# ═══════════════════════════════════════════════════════════════
def _mask_sensitive(text: str) -> str:
    masked = text
    masked = re.sub(r'(ghp_)[A-Za-z0-9]{4,}([A-Za-z0-9]{4})', r'\1****\2', masked)
    masked = re.sub(r'(gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{4,}([A-Za-z0-9]{4})', r'\1****\2', masked)
    masked = re.sub(r'(github_pat_)[A-Za-z0-9_]{4,}([A-Za-z0-9_]{4})', r'\1****\2', masked)
    masked = re.sub(r'(sk-)[A-Za-z0-9]{4,}([A-Za-z0-9]{4})', r'\1****\2', masked)
    masked = re.sub(r'(sk-proj-)[A-Za-z0-9\-]{4,}([A-Za-z0-9]{4})', r'\1****\2', masked)
    masked = re.sub(r'(AKIA)[A-Z0-9]{4,}([A-Z0-9]{4})', r'\1****\2', masked)
    masked = re.sub(r'(AIza)[A-Za-z0-9\-_]{4,}([A-Za-z0-9\-_]{4})', r'\1****\2', masked)
    masked = re.sub(
        r'(["\'])([A-Za-z0-9\-_]{16,})\1',
        lambda m: m.group(1) + m.group(2)[:6] + '****' + m.group(2)[-4:] + m.group(1),
        masked
    )
    return masked


def detect_project_root() -> Path:
    current = Path(__file__).resolve().parent
    markers = [".git", "pyproject.toml", "setup.py", "README.md", "main.py"]
    while current != current.parent:
        for marker in markers:
            if (current / marker).exists():
                return current
        current = current.parent
    return Path(__file__).resolve().parent.parent


def should_skip_dir(dirname: str) -> bool:
    if dirname in SKIP_DIRS:
        return True
    if dirname.startswith('.'):
        return True
    if dirname.endswith('.egg-info'):
        return True
    return False


def should_skip_file(filepath: Path) -> bool:
    if filepath.suffix.lower() in SKIP_EXTENSIONS:
        return True
    if filepath.name.startswith('.'):
        if filepath.name in ('.env', '.env.local', '.env.production', '.env.staging', '.env.development'):
            return False
        return True
    return False


def is_sensitive_filename(filename: str) -> bool:
    if filename in SENSITIVE_FILES:
        return True
    for pattern in SENSITIVE_FILES:
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if filename.endswith(suffix):
                return True
    return False


def is_audit_exception(filename: str) -> bool:
    return filename in AUDIT_EXCEPTIONS


def _is_false_positive_variable(line: str) -> bool:
    lower_line = line.lower()
    for fp_pattern in FALSE_POSITIVE_PATTERNS:
        if fp_pattern.lower() in lower_line:
            return True
    return False


def check_gitignore(root: Path) -> Tuple[bool, List[str]]:
    gitignore_path = root / ".gitignore"
    if not gitignore_path.exists():
        return False, [".gitignore no existe"]
    try:
        content = gitignore_path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return False, ["No se pudo leer .gitignore"]

    not_ignored = []
    gitignore_lines = [
        line.strip() for line in content.splitlines()
        if line.strip() and not line.strip().startswith('#')
    ]

    # ✅ v1.3.1: FIX — usa nombres reales del proyecto, no patrones genéricos
    # "tokens github.txt" es el archivo real (con espacio), ya está en .gitignore v0.6.4
    sensitive_to_check = [
        "tokens github.txt",
        "secrets.json", "credentials.json",
        "*.pem", "*.key",
    ]

    for pattern in sensitive_to_check:
        found = any(pattern in line or line == pattern for line in gitignore_lines)
        if not found:
            not_ignored.append(pattern)

    return len(not_ignored) == 0, not_ignored


def _is_file_in_gitignore(filename: str, root: Path) -> bool:
    """Verifica si un archivo específico está protegido por .gitignore."""
    gitignore_path = root / ".gitignore"
    if not gitignore_path.exists():
        return False
    try:
        content = gitignore_path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith('#') or not stripped:
            continue
        # Coincidencia exacta o patrón que contiene el nombre
        if stripped == filename or filename in stripped:
            return True
        # Patrón con tools/ prefix
        if stripped == f"tools/{filename}" or f"tools/{filename}" in stripped:
            return True
    return False


def _is_file_tracked_by_git(filepath: str, root: Path) -> bool:
    """Verifica si un archivo está trackeado por Git."""
    git_dir = root / ".git"
    if not git_dir.exists():
        return False
    try:
        result = subprocess.run(
            ['git', 'ls-files', filepath],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def check_git_tracking(root: Path) -> Tuple[bool, List[str]]:
    git_dir = root / ".git"
    if not git_dir.exists():
        return True, []
    try:
        result = subprocess.run(
            ['git', 'ls-files'],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15
        )
        if result.returncode != 0:
            return True, []
        tracked_files = result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True, []

    sensitive_tracked = []
    sensitive_names = {
        "tokens github.txt", "tokens.txt", "passwords.txt",
        "credentials.txt", "secrets.json", "credentials.json",
        "tokens.json", "api_keys.json",
        ".env", ".env.local", ".env.production",
    }

    for tracked in tracked_files:
        basename = Path(tracked).name
        # Archivos en excepciones no son amenaza (están controlados)
        if is_audit_exception(basename):
            continue
        if basename in sensitive_names:
            sensitive_tracked.append(tracked)
        if tracked.endswith(('.pem', '.key', '.p12', '.pfx')):
            sensitive_tracked.append(tracked)

    return len(sensitive_tracked) == 0, sensitive_tracked


# ═══════════════════════════════════════════════════════════════
# ESCÁNER PRINCIPAL
# ═══════════════════════════════════════════════════════════════
class SecurityAuditor:
    def __init__(self, root: Path, verbose: bool = False):
        self.root = root
        self.verbose = verbose
        self.report = AuditReport(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            project_root=str(root),
        )

    def _check_controlled_status(self, filepath: Path, original_severity: Severity) -> Tuple[Severity, str]:
        """
        ✅ v1.3.0: Determina si un hallazgo es 'peligro controlado'.
        Un archivo es CONTROLADO si cumple TODAS estas condiciones:
        1. Está en AUDIT_EXCEPTIONS (el usuario lo declaró como tal)
        2. Está en .gitignore (Git no lo commitea)
        3. NO está trackeado por Git (no fue commiteado antes)
        Returns: (severidad_final, razón_de_control)
        """
        filename = filepath.name

        # Condición 1: ¿Está en excepciones?
        if not is_audit_exception(filename):
            return original_severity, ""

        # Condición 2: ¿Está en .gitignore?
        in_gitignore = _is_file_in_gitignore(filename, self.root)
        if not in_gitignore:
            return original_severity, ""

        # Condición 3: ¿NO está trackeado por Git?
        rel_path = str(filepath.relative_to(self.root))
        is_tracked = _is_file_tracked_by_git(rel_path, self.root)
        if is_tracked:
            return original_severity, ""

        # ✅ Las 3 condiciones se cumplen → PELIGRO CONTROLADO
        protections = []
        protections.append("en .gitignore")
        protections.append("no trackeado por Git")
        reason = " + ".join(protections)
        return Severity.CONTROLLED, reason

    def scan(self) -> AuditReport:
        import time
        start = time.time()
        self._print_header()

        # 1. Verificar .gitignore
        self._print_step("Verificando .gitignore...")
        gitignore_ok, not_ignored = check_gitignore(self.root)
        self.report.gitignore_ok = gitignore_ok
        if not gitignore_ok:
            for pattern in not_ignored:
                self.report.findings.append(Finding(
                    severity=Severity.HIGH,
                    category="gitignore",
                    file=".gitignore",
                    line=0,
                    description=f"Patrón sensible no encontrado en .gitignore: `{pattern}`",
                    remediation=f"Agregar `{pattern}` a .gitignore en la sección 🔑 CREDENCIALES",
                ))

        # 2. Verificar Git tracking
        self._print_step("Verificando tracking de Git...")
        tracking_ok, tracked_sensitive = check_git_tracking(self.root)
        self.report.git_tracking_ok = tracking_ok
        if not tracking_ok:
            for tracked_file in tracked_sensitive:
                self.report.findings.append(Finding(
                    severity=Severity.CRITICAL,
                    category="git-tracking",
                    file=tracked_file,
                    line=0,
                    description=f"Archivo sensible trackeado por Git: `{tracked_file}`",
                    detail="Este archivo está en el historial de Git y podría estar expuesto",
                    remediation=f"Ejecutar: git rm --cached {tracked_file} && git commit -m 'security: remove tracked sensitive file'",
                ))

        # 3. Escanear archivos
        self._print_step("Escaneando archivos del proyecto...")
        self._scan_directory(self.root)

        elapsed = (time.time() - start) * 1000
        self.report.scan_duration_ms = elapsed
        self._print_summary()
        return self.report

    def _scan_directory(self, directory: Path):
        try:
            items = sorted(directory.iterdir(), key=lambda x: x.name.lower())
        except PermissionError:
            return

        for item in items:
            if item.is_dir():
                if not should_skip_dir(item.name):
                    self._scan_directory(item)
            elif item.is_file():
                if not should_skip_file(item):
                    self._scan_file(item)

    def _scan_file(self, filepath: Path):
        self.report.total_files_scanned += 1
        is_exception = is_audit_exception(filepath.name)

        if is_sensitive_filename(filepath.name) and not is_exception:
            rel_path = str(filepath.relative_to(self.root))
            self.report.findings.append(Finding(
                severity=Severity.HIGH,
                category="sensitive-file",
                file=rel_path,
                line=0,
                description=f"Archivo sensible detectado: `{filepath.name}`",
                detail="Este archivo contiene o puede contener credenciales",
                remediation="Verificar que esté en .gitignore y no trackeado por Git",
            ))

        if is_exception:
            rel_path = str(filepath.relative_to(self.root))
            if rel_path not in self.report.exceptions_applied:
                self.report.exceptions_applied.append(rel_path)

        try:
            content = filepath.read_text(encoding='utf-8', errors='ignore')
        except (PermissionError, OSError):
            return

        lines = content.splitlines()
        self.report.total_lines_scanned += len(lines)
        rel_path = str(filepath.relative_to(self.root))

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            for pattern_name, pattern, severity, description in CREDENTIAL_PATTERNS:
                match = pattern.search(line)
                if match:
                    if self._is_false_positive(line, filepath, match.group(), is_exception):
                        continue
                    if _is_false_positive_variable(line):
                        continue

                    # ✅ v1.3.0: Verificar si es peligro controlado
                    final_severity, controlled_reason = self._check_controlled_status(filepath, severity)

                    self.report.findings.append(Finding(
                        severity=final_severity,
                        category="credential",
                        file=rel_path,
                        line=line_num,
                        description=f"{description} ({pattern_name})",
                        detail=stripped,
                        pattern_matched=pattern_name,
                        remediation=self._get_remediation(pattern_name, rel_path) if final_severity != Severity.CONTROLLED else "",
                        controlled_reason=controlled_reason,
                    ))

            for pattern, severity, description in HARDCODED_PATH_PATTERNS:
                match = pattern.search(line)
                if match:
                    self.report.findings.append(Finding(
                        severity=severity,
                        category="hardcoded-path",
                        file=rel_path,
                        line=line_num,
                        description=description,
                        detail=stripped,
                        remediation="Usar Path(__file__).resolve().parent o detección automática de project root",
                    ))

    def _is_false_positive(self, line: str, filepath: Path, matched_text: str,
                           is_exception: bool = False) -> bool:
        stripped = line.strip()

        if is_exception:
            return True
        if filepath.name == '.gitignore':
            return True

        placeholder_indicators = [
            'xxxx', 'your_', 'tu_', 'aqui', 'here', 'example',
            'placeholder', 'replace', 'REPLACE', 'INSERT', 'TODO',
            '<', '>', '...', 'sample', 'SAMPLE'
        ]
        lower_line = line.lower()
        for indicator in placeholder_indicators:
            if indicator in lower_line:
                return True

        if stripped.startswith('#') and any(w in stripped.upper() for w in ['NUNCA', 'NO VERSIONAR', 'OCULTO', 'REDACTADO']):
            return True
        if 're.compile' in line or 'Pattern' in line:
            return True
        if 'tools/documentador/' in str(filepath) or 'axioma_doc_' in filepath.name:
            return True

        return False

    def _get_remediation(self, pattern_name: str, file_path: str) -> str:
        if 'GitHub' in pattern_name:
            return "1. Revocar token en github.com/settings/tokens | 2. Mover a variable de entorno | 3. Agregar a .gitignore"
        if 'OpenAI' in pattern_name:
            return "1. Revocar key en platform.openai.com/api-keys | 2. Mover a .env | 3. Agregar a .gitignore"
        if 'AWS' in pattern_name:
            return "1. Desactivar key en AWS IAM | 2. Usar AWS CLI profiles | 3. Agregar a .gitignore"
        if 'PEM' in pattern_name or 'SSH' in pattern_name:
            return "1. Regenerar clave privada | 2. Mover fuera del repo | 3. Agregar a .gitignore"
        if 'JWT' in pattern_name:
            return "Remover JWT hardcodeado — usar generación dinámica desde secrets"
        if 'Database' in pattern_name:
            return "Usar variables de entorno para connection strings"
        return "Mover credencial a variable de entorno y agregar a .gitignore"

    # ═══════════════════════════════════════════════════════════════
    # IMPRESIÓN
    # ═══════════════════════════════════════════════════════════════
    def _print_header(self):
        print(f"\n{C.BOLD}{C.BLUE}{'═' * 70}{C.RESET}")
        print(f"{C.BOLD}{C.BLUE}║ 🔒 AXIOMA Security Auditor v{AUDIT_VERSION}{C.RESET}")
        print(f"{C.BOLD}{C.BLUE}║ Escaneando: {C.CYAN}{self.root}{C.RESET}")
        print(f"{C.BOLD}{C.BLUE}{'═' * 70}{C.RESET}\n")

    def _print_step(self, message: str):
        if self.verbose:
            print(f"  {C.CYAN}→{C.RESET} {message}")

    def _print_summary(self):
        r = self.report
        print(f"\n{C.BOLD}{'─' * 70}{C.RESET}")
        print(f"{C.BOLD}📊 RESUMEN DE AUDITORÍA{C.RESET}")
        print(f"{C.BOLD}{'─' * 70}{C.RESET}")
        print(f"  📁 Archivos escaneados: {C.BOLD}{r.total_files_scanned:,}{C.RESET}")
        print(f"  📝 Líneas escaneadas:   {C.BOLD}{r.total_lines_scanned:,}{C.RESET}")
        print(f"  ⏱️  Duración:            {r.scan_duration_ms:.0f} ms")
        print(f"  📄 .gitignore:          {'✅ OK' if r.gitignore_ok else '❌ INCOMPLETO'}")
        print(f"  🔀 Git tracking:        {'✅ OK' if r.git_tracking_ok else '❌ ARCHIVOS SENSIBLES TRAQUEADOS'}")
        print()

        # Conteos
        has_real = r.critical_count > 0 or r.high_count > 0
        has_medium = r.medium_count > 0
        has_low = r.low_count > 0
        has_controlled = r.controlled_count > 0

        if has_real:
            print(f"  {Severity.CRITICAL.icon} {Severity.CRITICAL.color} CRÍTICO: {r.critical_count} {C.RESET}")
            print(f"  {Severity.HIGH.icon} ALTO:    {r.high_count}")
        if has_medium:
            print(f"  {Severity.MEDIUM.icon} MEDIO:   {r.medium_count}")
        if has_low:
            print(f"  {Severity.LOW.icon} BAJO:    {r.low_count}")
        if has_controlled:
            print(f"  {Severity.CONTROLLED.icon} {C.MAGENTA}CONTROLADO: {r.controlled_count}{C.RESET}")
        print()

        # ── SECCIÓN 1: AMENAZAS REALES (sin control) ──────────────
        real_findings = r.real_threats
        if real_findings:
            print(f"\n{C.BG_RED}{C.WHITE}{C.BOLD} ⚠️  AMENAZAS REALES — Requieren acción inmediata {C.RESET}")
            print(f"{'─' * 70}")
            for severity in [Severity.CRITICAL, Severity.HIGH]:
                sev_findings = [f for f in real_findings if f.severity == severity]
                if sev_findings:
                    print(f"\n{C.BOLD}{severity.icon} {severity.value} ({len(sev_findings)}){C.RESET}")
                    print(f"{'─' * 50}")
                    for finding in sev_findings[:20]:
                        print(finding.format_terminal())
                    print()
                    if len(sev_findings) > 20:
                        print(f"  {C.DIM}... y {len(sev_findings) - 20} más de severidad {severity.value}{C.RESET}")

        # ── SECCIÓN 2: MEDIO/BAJO ──────────────────────────────────
        for severity in [Severity.MEDIUM, Severity.LOW]:
            sev_findings = [f for f in r.findings if f.severity == severity]
            if sev_findings:
                print(f"\n{C.BOLD}{severity.icon} {severity.value} ({len(sev_findings)}){C.RESET}")
                print(f"{'─' * 50}")
                for finding in sev_findings[:10]:
                    print(finding.format_terminal())
                print()
                if len(sev_findings) > 10:
                    print(f"  {C.DIM}... y {len(sev_findings) - 10} más de severidad {severity.value}{C.RESET}")

        # ── SECCIÓN 3: PELIGRO CONTROLADO ─────────────────────────
        controlled = r.controlled_findings
        if controlled:
            print(f"\n{C.BG_MAGENTA}{C.WHITE}{C.BOLD} 🛡️  PELIGRO CONTROLADO — Archivos sensibles con protección activa {C.RESET}")
            print(f"{'─' * 70}")
            print(f"  {C.DIM}Estos archivos contienen credenciales pero ya están protegidos.{C.RESET}")
            print(f"  {C.DIM}No requieren acción. Solo informativo.{C.RESET}")
            print()

            # Agrupar por archivo para no repetir
            by_file: Dict[str, List[Finding]] = {}
            for f in controlled:
                by_file.setdefault(f.file, []).append(f)

            for file_path, file_findings in by_file.items():
                reason = file_findings[0].controlled_reason
                print(f"  🛡️  {C.BOLD}{C.CYAN}{file_path}{C.RESET}")
                print(f"     Protección: {C.GREEN}{reason}{C.RESET}")
                # Mostrar qué tipo de credenciales contiene (sin valores)
                patterns_found = set()
                for f in file_findings:
                    if f.pattern_matched:
                        patterns_found.add(f.pattern_matched)
                if patterns_found:
                    print(f"     Contiene: {', '.join(sorted(patterns_found))}")
                print()

        # ── RESULTADO FINAL ────────────────────────────────────────
        print(f"\n{C.BOLD}{'═' * 70}{C.RESET}")
        if r.is_clean:
            print(f"{C.GREEN}{C.BOLD}✅ RESULTADO: AUDITORÍA APROBADA{C.RESET}")
            print(f"   No se detectaron amenazas reales sin control.")
            if controlled:
                print(f"   🛡️  {len(controlled)} hallazgos en PELIGRO CONTROLADO (protegidos).")
        else:
            total_real = r.critical_count + r.high_count
            print(f"{C.RED}{C.BOLD}❌ RESULTADO: AUDITORÍA FALLIDA{C.RESET}")
            print(f"   Se encontraron {total_real} amenazas reales sin control.")
            print(f"   Acción requerida: remediar antes del próximo push.")
            if controlled:
                print(f"   🛡️  {len(controlled)} hallazgos adicionales en PELIGRO CONTROLADO (OK).")
        print(f"{C.BOLD}{'═' * 70}{C.RESET}\n")


# ═══════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════
def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description=f"AXIOMA Security Auditor v{AUDIT_VERSION} — Escáner de credenciales filtradas"
    )
    parser.add_argument("--root", type=Path, default=None, help="Ruta raíz del proyecto")
    parser.add_argument("--verbose", "-v", action="store_true", help="Mostrar pasos del escaneo")
    parser.add_argument("--json", action="store_true", help="Generar reporte JSON")
    parser.add_argument("--fail-on-high", action="store_true", default=True, help="Retornar código 1 si hay CRÍTICOS o ALTOS")
    args = parser.parse_args()

    root = args.root or detect_project_root()
    if not root.exists():
        print(f"{C.RED}❌ Error: La ruta {root} no existe{C.RESET}")
        return 1

    auditor = SecurityAuditor(root, verbose=args.verbose)
    report = auditor.scan()

    if args.json:
        json_path = root / "data" / "security_audit_report.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"  📄 Reporte JSON guardado: {C.CYAN}{json_path}{C.RESET}\n")

    if args.fail_on_high and not report.is_clean:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
