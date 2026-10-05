#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tests/test_axioma_07_seguridad_modos.py
# Autor: SaintWick
# AXIOMA — Suite integral de verificación (archivo 7/7)
# test_axioma_07_seguridad_modos.py
# Sección 7: FASE 1-3 plan_harness — RuntimeMode, fail-closed del
# sandbox, PermissionMode.DENY, auditoría de archivos, log de sesión
# y EventBus/SecurityListener.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTION = "7. Seguridad: RuntimeMode, sandbox fail-closed, auditoría y eventos"


# ═══════════════════════════════════════════════════════════════
# 7A. FASE 1 — RuntimeMode + SandboxUnavailableError + DENY
# ═══════════════════════════════════════════════════════════════


class TestRuntimeMode:
    def test_runtime_mode_enum(self):
        section(SECTION)
        from src.domain.types import RuntimeMode
        values = {m.value for m in RuntimeMode}
        check("RuntimeMode valores",
              values == {"standard", "minimal", "safe_mode", "code_mode"},
              str(sorted(values)), SECTION)
        check("STANDARD permite escritura",
              RuntimeMode.STANDARD.allows_file_write() is True, "True", SECTION)
        check("SAFE_MODE no permite escritura",
              RuntimeMode.SAFE_MODE.allows_file_write() is False, "False", SECTION)
        check("CODE_MODE permite escritura",
              RuntimeMode.CODE_MODE.allows_file_write() is True, "True", SECTION)
        check("MINIMAL no permite escritura",
              RuntimeMode.MINIMAL.allows_file_write() is False, "False", SECTION)

    def test_allowed_permission_level(self):
        from src.domain.types import RuntimeMode
        from src.tools_mcp.tool_base import ToolPermission
        check("SAFE_MODE → READ_ONLY",
              RuntimeMode.SAFE_MODE.allowed_permission_level() == ToolPermission.READ_ONLY,
              "READ_ONLY", SECTION)
        check("MINIMAL → EXECUTE",
              RuntimeMode.MINIMAL.allowed_permission_level() == ToolPermission.EXECUTE,
              "EXECUTE", SECTION)
        check("STANDARD → PRIVILEGED",
              RuntimeMode.STANDARD.allowed_permission_level() == ToolPermission.PRIVILEGED,
              "PRIVILEGED", SECTION)
        check("CODE_MODE → PRIVILEGED",
              RuntimeMode.CODE_MODE.allowed_permission_level() == ToolPermission.PRIVILEGED,
              "PRIVILEGED", SECTION)


class TestSandboxFailClosed:
    def test_sandbox_unavailable_error(self):
        from src.domain.exceptions import SandboxUnavailableError, AxiomaError
        e = SandboxUnavailableError("no confinement", backend="platform")
        check("SandboxUnavailableError es AxiomaError",
              isinstance(e, AxiomaError), type(e).__mro__[1].__name__, SECTION)
        check("código AXIOMA_SANDBOX_UNAVAILABLE",
              e.code == "AXIOMA_SANDBOX_UNAVAILABLE_PLATFORM", e.code, SECTION)

    def test_can_confine_en_linux(self):
        from src.tools_mcp.sandbox import SandboxExecutor, SandboxConfig, SandboxMode
        sb = SandboxExecutor(SandboxConfig(mode=SandboxMode.STRICT))
        can = sb._can_confine()
        # En Linux x86_64 con resource disponible → True
        check("_can_confine() en Linux", can is True or sys.platform == "win32",
              f"can_confine={can} platform={sys.platform}", SECTION)

    def test_fail_closed_en_win32(self, monkeypatch):
        from src.tools_mcp.sandbox import (
            SandboxExecutor, SandboxConfig, SandboxMode,
        )
        from src.domain.exceptions import SandboxUnavailableError
        # Simular plataforma win32 → STRICT debe rechazar (fail-closed)
        sb = SandboxExecutor(SandboxConfig(mode=SandboxMode.STRICT))
        monkeypatch.setattr(sb, "_can_confine", lambda: False)
        try:
            sb._raise_if_cannot_confine()
            check("fail-closed win32 lanza", False, "no lanzó", SECTION)
        except SandboxUnavailableError as e:
            check("fail-closed win32 lanza", True, f"SandboxUnavailableError ({e.code})", SECTION)
        except RuntimeError as e:
            check("fail-closed win32 lanza", True, f"RuntimeError fallback: {e}", SECTION)

    def test_relaxed_no_lanza(self, monkeypatch):
        from src.tools_mcp.sandbox import SandboxExecutor, SandboxConfig, SandboxMode
        sb = SandboxExecutor(SandboxConfig(mode=SandboxMode.RELAXED))
        monkeypatch.setattr(sb, "_can_confine", lambda: False)
        sb._raise_if_cannot_confine()  # no debe lanzar en RELAXED
        check("RELAXED no exige confinamiento", True, "sin excepción", SECTION)


class TestPermissionModeDeny:
    def test_deny_en_enum(self):
        from src.tools_mcp.permissions import PermissionMode, Decision
        check("PermissionMode.DENY existe", PermissionMode.DENY.value == "deny",
              PermissionMode.DENY.value, SECTION)
        check("Decision.DENY existe", Decision.DENY.value == "deny",
              Decision.DENY.value, SECTION)

    def test_gateway_reglas_reales(self):
        """El permissions.json real carga y las reglas DENY protegen rutas críticas."""
        from src.tools_mcp.permissions import PermissionGateway
        from src.tools_mcp.tool_base import ToolPermission
        import threading, os

        gw = PermissionGateway.__new__(PermissionGateway)
        gw._config_path = PROJECT_ROOT / "config" / "permissions.json"
        gw._rules = []
        gw._lock = threading.RLock()
        gw._observer = None
        gw._load_stats = {}
        gw._reload_rules()

        root = str(PROJECT_ROOT)
        # ✅ FIX 2026-08-29: el comprehension anterior usaba "mode" como clave
        # literal → modes quedaba {"mode": ...} y modes.get(".git/**") daba
        # FALTA siempre (checks rojos desde la creación del test, ocultos por
        # reportes parciales). La key real del JSON es "pattern".
        rules_data = json.loads((PROJECT_ROOT / "config" / "permissions.json").read_text())["rules"]
        modes = {m["pattern"]: m["mode"] for m in rules_data}
        check("rules cargadas (>=8)", len(gw._rules) >= 8, f"{len(gw._rules)} reglas", SECTION)
        check("existe regla deny .git", modes.get(".git/**") == "deny",
              modes.get(".git/**", "FALTA"), SECTION)
        check("existe regla deny .env", modes.get(".env*") == "deny",
              modes.get(".env*", "FALTA"), SECTION)
        check("existe regla deny config", modes.get("config/**") == "deny",
              modes.get("config/**", "FALTA"), SECTION)
        check("existe regla deny data", modes.get("data/**") == "deny",
              modes.get("data/**", "FALTA"), SECTION)
        check("src/** ya no es acceptEdits", modes.get("src/**") != "acceptEdits",
              modes.get("src/**", "FALTA"), SECTION)

        def ev(path, tool, perm, conf=0.9, risk=0.1):
            return gw.evaluate(path, tool, conf, risk, tool_permission=perm).decision.value

        check(".git write → deny",
              ev(root + "/.git/config", "fs_write", ToolPermission.LOCAL_WRITE) == "deny",
              ev(root + "/.git/config", "fs_write", ToolPermission.LOCAL_WRITE), SECTION)
        check(".env read → deny",
              ev(root + "/.env", "fs_read", ToolPermission.READ_ONLY) == "deny",
              ev(root + "/.env", "fs_read", ToolPermission.READ_ONLY), SECTION)
        check("config write → deny",
              ev(root + "/config/settings.py", "fs_write", ToolPermission.LOCAL_WRITE) == "deny",
              ev(root + "/config/settings.py", "fs_write", ToolPermission.LOCAL_WRITE), SECTION)
        check("config read → allow (whitelist)",
              ev(root + "/config/settings.py", "fs_read", ToolPermission.READ_ONLY) == "allow",
              ev(root + "/config/settings.py", "fs_read", ToolPermission.READ_ONLY), SECTION)
        check("data write → deny",
              ev(root + "/data/axioma.db", "fs_write", ToolPermission.LOCAL_WRITE) == "deny",
              ev(root + "/data/axioma.db", "fs_write", ToolPermission.LOCAL_WRITE), SECTION)
        check("src write → ask (default)",
              ev(root + "/src/core/dispatcher.py", "fs_write", ToolPermission.LOCAL_WRITE,
                 conf=0.95, risk=0.05) == "ask",
              ev(root + "/src/core/dispatcher.py", "fs_write", ToolPermission.LOCAL_WRITE,
                 conf=0.95, risk=0.05), SECTION)
        check("src read → allow (regla plan *)",
              ev(root + "/src/core/dispatcher.py", "fs_read", ToolPermission.READ_ONLY) == "allow",
              ev(root + "/src/core/dispatcher.py", "fs_read", ToolPermission.READ_ONLY), SECTION)


class TestRegistryExecutorMode:
    def test_list_for_mode(self):
        from src.tools_mcp.tool_registry import get_registry
        from src.domain.types import RuntimeMode
        reg = get_registry()
        safe = reg.list_for_mode(RuntimeMode.SAFE_MODE)
        std = reg.list_for_mode(RuntimeMode.STANDARD)
        check("list_for_mode(SAFE_MODE) solo READ_ONLY",
              all(t["permission_level"] <= 0 for t in safe),
              f"{len(safe)} tools", SECTION)
        check("STANDARD ⊇ SAFE_MODE",
              len(std) >= len(safe), f"{len(std)} vs {len(safe)}", SECTION)

    def test_executor_bloquea_por_modo(self):
        from src.tools_mcp.tool_executor import ToolExecutor, ExecutionContext
        from src.domain.types import RuntimeMode
        from src.tools_mcp.tool_registry import get_registry

        tool = get_registry().get("fs_write")  # LOCAL_WRITE
        check("fs_write es LOCAL_WRITE", tool.permission_level == 1, str(tool.permission_level), SECTION)

        async def _run():
            ex = ToolExecutor()
            r = await ex.run(
                "fs_write",
                context=ExecutionContext(
                    caller="test_mode", resource_path="/tmp/x.txt",
                    runtime_mode=RuntimeMode.SAFE_MODE,
                    skip_permission_check=True,
                ),
                path="/tmp/x.txt", content="x",
            )
            return r

        r = asyncio.run(_run())
        check("SAFE_MODE bloquea fs_write",
              r.metadata.get("runtime_mode_blocked") is True,
              r.error or "sin error", SECTION)

    def test_settings_runtime_mode(self):
        from config.settings import settings
        check("settings.runtime_mode existe",
              getattr(settings, "runtime_mode", None) == "standard",
              str(getattr(settings, "runtime_mode", "FALTA")), SECTION)


# ═══════════════════════════════════════════════════════════════
# 7B. FASE 2 — SessionEventLog + FileAuditTrail
# ═══════════════════════════════════════════════════════════════


class TestSessionEventLog:
    def test_ciclo_completo(self, tmp_path):
        from src.learning.session_event_log import SessionEventLog, SessionEventType
        db = tmp_path / "events.db"
        log = SessionEventLog(db_path=db)
        try:
            e1 = log.append("s1", SessionEventType.MESSAGE_ADDED,
                            {"role": "user", "content_preview": "hola", "chars": 4})
            log.append("s1", SessionEventType.MESSAGE_ADDED,
                       {"role": "assistant", "content_preview": "mundo", "chars": 5})
            log.append("s1", SessionEventType.TOOL_CALLED, {"tool": "fs_read"})
            check("append devuelve id", e1 is not None and e1 >= 1, str(e1), SECTION)

            msgs = log.derive_messages("s1")
            check("derive_messages reconstruye historial",
                  [m["role"] for m in msgs] == ["user", "assistant"],
                  str([m["role"] for m in msgs]), SECTION)

            inv = log.verify_invariant("s1")
            check("verify_invariant ok", inv["ok"] is True, str(inv["checks"]), SECTION)

            copied = log.fork("s1", "s1-hija")
            check("fork copia eventos", copied == 3, f"{copied} copiados", SECTION)
            child_events = log.events("s1-hija")
            check("hija tiene eventos + marca fork",
                  len(child_events) == 4, f"{len(child_events)} eventos", SECTION)
        finally:
            log.close()

    def test_no_rompe_sin_pool(self, tmp_path):
        # Degradación silenciosa si la infraestructura falta
        import src.learning.session_event_log as sel
        from src.learning.session_event_log import SessionEventLog, SessionEventType
        # ✅ FIX 2026-08-29: el test parcheaba dbp.SQLiteConnectionPool, pero el
        # módulo importa el pool con `from ... import` (binding propio) y además
        # decide por el flag _POOL_AVAILABLE (session_event_log.py:48-50,99) —
        # el patch no tenía efecto y "no lanzó" sin fin. Se parchea el flag real.
        orig_flag = sel._POOL_AVAILABLE
        try:
            sel._POOL_AVAILABLE = False  # type: ignore[assignment]
            try:
                SessionEventLog(db_path=tmp_path / "x.db")
                check("SessionEventLog sin pool falla limpio", False, "no lanzó", SECTION)
            except ImportError:
                check("SessionEventLog sin pool falla limpio", True, "ImportError", SECTION)
        finally:
            sel._POOL_AVAILABLE = orig_flag


class TestFileAuditTrail:
    def test_ciclo_completo(self, tmp_path):
        from src.learning.file_audit import FileAuditTrail, FileOpStatus
        db = tmp_path / "audit.db"
        audit = FileAuditTrail(db_path=db)
        try:
            f = tmp_path / "archivo.txt"
            f.write_text("contenido v1")
            rid = audit.record("s1", "write", str(f),
                               status=FileOpStatus.COMMITTED, proposal_id="p1")
            check("record devuelve id", rid is not None, str(rid), SECTION)

            ops = audit.get_session_operations("s1")
            check("get_session_operations", len(ops) == 1 and ops[0]["status"] == "committed",
                  str([(o["operation"], o["status"]) for o in ops]), SECTION)

            f.write_text("contenido v2")
            v1 = audit.verify_checksum("s1", str(f))
            check("verify_checksum detecta cambio", v1["matches"] is False,
                  v1["detail"], SECTION)

            f.write_text("contenido v1")
            v2 = audit.verify_checksum("s1", str(f))
            check("verify_checksum ok tras restaurar", v2["matches"] is True,
                  v2["detail"], SECTION)
        finally:
            audit.close()

    def test_integracion_promotion_gate(self, tmp_path):
        """PromotionGate._audit_record registra en FileAuditTrail (FASE 2+3)."""
        from src.core.promotion_gate import PromotionGate
        from src.learning.file_audit import get_file_audit, FileOpStatus
        gate = PromotionGate.__new__(PromotionGate)
        # Instancia sin __init__ (evita init pesado); solo probamos _audit_record
        gate._audit_record("s-1", "write", str(tmp_path / "a.py"),
                           FileOpStatus.PROPOSED, proposal_id="prop_x")
        audit = get_file_audit()
        ops = audit.get_session_operations("s-1") if audit else []
        check("PromotionGate audita propuestas",
              any(o["proposal_id"] == "prop_x" and o["status"] == "proposed" for o in ops),
              f"{len(ops)} ops", SECTION)


# ═══════════════════════════════════════════════════════════════
# 7C. FASE 3 — EventBus + SecurityListener
# ═══════════════════════════════════════════════════════════════


class TestPropagacionRuntimeModeAgentes:
    """gap.txt (2026-08-26): /mode safe_mode debe bloquear fs_write desde BaseAgent."""

    def _make_agent(self):
        from src.agents.base import BaseAgent

        class TestAgent(BaseAgent):
            AGENT_TYPE = "test_gap_agent"

            def execute(self, input_msg, **kwargs):
                pass

        return TestAgent()

    def test_safe_mode_bloquea_fs_write_desde_agente(self, monkeypatch):
        section(SECTION)
        from config.settings import settings
        from src.domain.types import RuntimeMode
        monkeypatch.setattr(settings, "runtime_mode", RuntimeMode.SAFE_MODE.value)

        agent = self._make_agent()
        result = agent.execute_tool_sync(
            "fs_write", path="/tmp/axioma_gap_test.txt", content="test"
        )
        check("safe_mode bloquea fs_write desde agente",
              getattr(result, "metadata", {}).get("runtime_mode_blocked") is True,
              str(getattr(result, "error", "")), SECTION)
        check("mensaje menciona safe_mode",
              "safe_mode" in str(getattr(result, "error", "")),
              str(getattr(result, "error", "")), SECTION)

    def test_standard_permite_fs_write_por_modo(self, monkeypatch):
        """En standard el filtro de modo NO bloquea (otras capas pueden fallar, no esta)."""
        from config.settings import settings
        monkeypatch.setattr(settings, "runtime_mode", "standard")
        agent = self._make_agent()
        result = agent.execute_tool_sync(
            "fs_write", path="/tmp/axioma_gap_test2.txt", content="test"
        )
        check("standard no bloquea por modo",
              getattr(result, "metadata", {}).get("runtime_mode_blocked") is not True,
              str(getattr(result, "error", "")), SECTION)

    def test_execute_tool_async_propaga_modo(self, monkeypatch):
        """La versión async (execute_tool) también propaga el modo."""
        from config.settings import settings
        from src.domain.types import RuntimeMode
        monkeypatch.setattr(settings, "runtime_mode", RuntimeMode.SAFE_MODE.value)
        agent = self._make_agent()

        async def _run():
            return await agent.execute_tool(
                "fs_write", path="/tmp/axioma_gap_test3.txt", content="test"
            )

        result = asyncio.run(_run())
        check("execute_tool async propaga safe_mode",
              getattr(result, "metadata", {}).get("runtime_mode_blocked") is True,
              str(getattr(result, "error", "")), SECTION)

    def test_modo_invalido_en_settings_no_rompe(self, monkeypatch):
        """Valor inválido en settings → degradación silenciosa (None), no rompe."""
        from config.settings import settings
        monkeypatch.setattr(settings, "runtime_mode", "modo_inexistente")
        agent = self._make_agent()
        # Debe resolver a None y NO bloquear por modo (fallará por otro motivo, no por RuntimeMode)
        result = agent.execute_tool_sync(
            "fs_write", path="/tmp/axioma_gap_test4.txt", content="test"
        )
        check("modo inválido no bloquea por RuntimeMode",
              getattr(result, "metadata", {}).get("runtime_mode_blocked") is not True,
              str(getattr(result, "error", "")), SECTION)


class TestEventBus:
    def test_publish_subscribe_isolation(self):
        from src.core.event_bus import EventBus
        from src.domain.events import AxiomaEvent, EventTypes
        bus = EventBus()
        received = []
        bus.subscribe(EventTypes.TOOL_DENIED, lambda ev: received.append(ev.event_type))
        bus.subscribe(EventTypes.SYSTEM, lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.subscribe(EventTypes.SYSTEM, lambda ev: received.append(ev.event_type))

        bus.publish(AxiomaEvent(event_type=EventTypes.TOOL_DENIED, data={"tool_name": "x"}))
        bus.publish_simple(EventTypes.SYSTEM, {}, source="test")
        check("listener recibe evento", received == ["tool.denied", "system"],
              str(received), SECTION)
        check("error de listener aislado", bus.get_stats()["listener_errors"] == 1,
              str(bus.get_stats()["listener_errors"]), SECTION)

    def test_disposer(self):
        from src.core.event_bus import EventBus
        from src.domain.events import EventTypes
        bus = EventBus()
        n = []
        disposer = bus.subscribe(EventTypes.SYSTEM, lambda ev: n.append(1))
        bus.publish_simple(EventTypes.SYSTEM, {}, source="t")
        disposer()
        bus.publish_simple(EventTypes.SYSTEM, {}, source="t")
        check("disposer desuscribe", len(n) == 1, str(len(n)), SECTION)

    def test_security_listener(self):
        from src.core.event_bus import get_event_bus
        from src.core.security_listener import get_security_listener
        from src.domain.events import EventTypes
        lst = get_security_listener(enabled=True)
        try:
            bus = get_event_bus()
            bus.publish_simple(EventTypes.TOOL_DENIED,
                               {"tool_name": "fs_write", "reason": "test"}, source="t")
            bus.publish_simple(EventTypes.SANDBOX_UNAVAILABLE,
                               {"backend": "platform", "message": "test"}, source="t")
            stats = lst.get_stats()
            check("SecurityListener cuenta eventos",
                  stats["events_by_type"].get("tool.denied", 0) >= 1,
                  str(stats["events_by_type"]), SECTION)
            check("SecurityListener escucha sandbox.unavailable",
                  stats["events_by_type"].get("sandbox.unavailable", 0) >= 1,
                  str(stats["events_by_type"]), SECTION)
        finally:
            lst.dispose()

    def test_executor_publica_eventos(self):
        from src.core.event_bus import get_event_bus
        from src.domain.events import EventTypes
        from src.tools_mcp.tool_executor import _publish_event
        bus = get_event_bus()
        seen = []
        disposer = bus.subscribe(EventTypes.TOOL_CALLED, lambda ev: seen.append(ev.data.get("tool_name")))
        try:
            _publish_event(EventTypes.TOOL_CALLED, {"tool_name": "fs_read", "caller": "test"})
            check("ToolExecutor publica TOOL_CALLED", seen == ["fs_read"], str(seen), SECTION)
        finally:
            disposer()
