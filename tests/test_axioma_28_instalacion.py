#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral
# INSTALACIÓN — Fase 1 del plan de contenedores: qué falta, cómo se baja y cómo se configura
# ═══════════════════════════════════════════════════════════════
# Este archivo salió de `test_axioma_27_portabilidad.py` cuando ese llegó al tope de 950 líneas
# (R13 permite dividir POR TEMA). Acá vive lo de INSTALAR y CONFIGURAR: catálogo de modelos,
# preflight, descarga con progreso y cancelación, claves del usuario, placa de video y la pantalla
# "Configurar AXIOMA". En el 27 quedan la portabilidad (rutas) y el empaquetado.
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

class _ClienteFalso:
    def __init__(self, vivo=True, modelos=()):
        self.base_url = "http://127.0.0.1:11434"
        self._vivo, self._modelos = vivo, list(modelos)
    def health_check(self):
        return self._vivo
    def list_models(self):
        return [{"name": m} for m in self._modelos]

def _hw(ram_gb=32.0):
    from types import SimpleNamespace
    return SimpleNamespace(ram_gb=ram_gb, gpu_vram_gb=0.0, cpu_threads=6, backend="cpu")

def _revisar(monkeypatch, modelos, libre_gb=500.0, ram_gb=32.0, vivo=True):
    from types import SimpleNamespace
    from src.core import preflight as pf
    monkeypatch.setattr(pf.shutil, "disk_usage",
                        lambda _p: SimpleNamespace(free=int(libre_gb * 1024 ** 3)))
    monkeypatch.setattr(pf, "_probar_audio",
                        lambda _c: pf.Requisito("audio", "Audio (voz)", pf.OK, "falso"))
    return pf.revisar(cliente=_ClienteFalso(vivo=vivo, modelos=modelos), hw=_hw(ram_gb))

def _todos_los_modelos():
    from config.model_catalog import cargar_catalogo
    return [n for n, m in cargar_catalogo().modelos.items() if m.tipo == "ollama"]

def _pre_fabricado():
    from src.core.preflight import AVISO, FALTA, Preflight, Requisito
    return Preflight(requisitos=[
        Requisito("ollama", "Motor de modelos (Ollama)", FALTA,
                  "no responde en http://127.0.0.1:11434", "instalalo con `ollama serve`"),
        Requisito("modelo_chat", "Modelo de chat", FALTA,
                  "no está descargado: qwen3:8b (4.87 GB en disco)", "`ollama pull qwen3:8b`",
                  {"modelo": "qwen3:8b", "obligatorio": True, "disco_gb": 4.87}),
        Requisito("modelo_vision", "Modelo de vision", AVISO, "no está descargado",
                  "`ollama pull qwen3-vl:4b`",
                  {"modelo": "qwen3-vl:4b", "obligatorio": False, "disco_gb": 3.3}),
        Requisito("audio", "Audio (voz)", AVISO, "sin audio", "la voz queda desactivada"),
    ])

def _env_temporal(tmp_path):
    archivo = tmp_path / ".env"
    archivo.write_text("# Configuración de ejemplo\nOLLAMA_HOST=http://127.0.0.1:11434\n"
                       "TAVILY_API_KEY=\n# un comentario que hay que conservar\n"
                       "LLM_DEFAULT_MODEL=qwen3:8b\n", encoding="utf-8")
    return archivo

# ── FASE 1: descarga de modelos con progreso y cancelación (`ISSUE-148`) ──
# Antes `pull_model` pedía `stream: false`: ni progreso ni forma de cancelar (raíz de `ISSUE-137`).

class _RespuestaFalsa:
    """Imita lo que devuelve httpx en modo flujo (context manager + iter_lines)."""

    def __init__(self, lineas, romper_al_final=False):
        self._lineas, self._romper = lineas, romper_al_final
        self.cerrada = False

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self):
        for linea in self._lineas:
            yield json.dumps(linea).encode()
        if self._romper:
            raise RuntimeError("conexión cerrada (cancelación)")

    def close(self):
        self.cerrada = True

def _cliente_de_descarga(monkeypatch, respuesta):
    from src.llm.client import OllamaClient
    cliente = OllamaClient.__new__(OllamaClient)
    cliente.base_url = "http://falso"
    monkeypatch.setattr(cliente, "_get_client", lambda timeout=None: cliente)
    monkeypatch.setattr(cliente, "_release_client", lambda _s: None)
    monkeypatch.setattr(cliente, "_log_extra", lambda *a, **k: None, raising=False)
    cliente._respuesta = respuesta
    cliente.stream = lambda *a, **k: respuesta
    return cliente

def test_la_descarga_informa_progreso_real(monkeypatch):
    eventos = []
    respuesta = _RespuestaFalsa([
        {"status": "pulling manifest"},
        {"status": "downloading", "completed": 25, "total": 100},
        {"status": "downloading", "completed": 100, "total": 100},
        {"status": "success"},
    ])
    cliente = _cliente_de_descarga(monkeypatch, respuesta)
    monkeypatch.setattr(
        "src.llm.client.OllamaClient._get_client", lambda self, timeout=None: cliente, raising=False)
    cliente._get_client = lambda timeout=None: cliente
    cliente.pull_model("modelo:x", on_progress=eventos.append)      # no debe levantar
    porcentajes = [e["porcentaje"] for e in eventos]
    assert porcentajes == [None, 25.0, 100.0, None], porcentajes
    assert eventos[0]["estado"] == "pulling manifest"
    assert eventos[-1]["modelo"] == "modelo:x"

def test_la_descarga_se_puede_cancelar_de_verdad(monkeypatch):
    """Cancelar cierra la conexión: eso NO es una falla, es una cancelación."""
    from src.llm.client_base import GeneracionCancelada, TokenCancelacion
    respuesta = _RespuestaFalsa([
        {"status": "downloading", "completed": 10, "total": 100},
    ], romper_al_final=True)
    cliente = _cliente_de_descarga(monkeypatch, respuesta)
    cliente._get_client = lambda timeout=None: cliente
    token = TokenCancelacion()
    token.cancelado = True                      # como si el usuario hubiera apretado DETENER
    try:
        cliente.pull_model("modelo:x", token=token)
        raise AssertionError("tendría que haber cortado por cancelación")
    except GeneracionCancelada:
        pass

def test_cancelar_en_medio_de_la_descarga_no_se_reporta_como_falla_de_red(monkeypatch):
    """Cancelar cierra la conexión: eso llega como error de red y NO es una falla.

    Es la rama que distingue "el usuario apretó DETENER" de "se cayó la red": sin ella, una
    cancelación se reportaría como error y el usuario vería un fallo que no ocurrió.
    """
    from src.llm.client_base import GeneracionCancelada, TokenCancelacion
    token = TokenCancelacion()

    class _RespuestaQueSeCorta(_RespuestaFalsa):
        def iter_lines(self):
            yield json.dumps({"status": "downloading", "completed": 5, "total": 100}).encode()
            token.cancelado = True          # el usuario aprieta DETENER en el medio
            raise RuntimeError("conexión cerrada por el cierre de la respuesta")

    cliente = _cliente_de_descarga(monkeypatch, _RespuestaQueSeCorta([]))
    cliente._get_client = lambda timeout=None: cliente
    try:
        cliente.pull_model("modelo:x", token=token)
        raise AssertionError("tendría que haber cortado por cancelación")
    except GeneracionCancelada:
        pass
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"se reportó como falla en vez de cancelación: {type(e).__name__}")

def test_si_ollama_reporta_un_error_la_descarga_falla(monkeypatch):
    from src.domain.exceptions import LLMError
    respuesta = _RespuestaFalsa([{"error": "model not found"}])
    cliente = _cliente_de_descarga(monkeypatch, respuesta)
    cliente._get_client = lambda timeout=None: cliente
    try:
        cliente.pull_model("modelo:no-existe")
        raise AssertionError("tendría que haber fallado")
    except LLMError as e:
        assert "model not found" in str(e), e

class _ClienteDeInstalacion:
    """Cliente falso: registra qué se pidió y puede fallar o cancelarse."""

    def __init__(self, fallan=(), cancelar_en=None):
        self.pedidos = []
        self.fallan, self.cancelar_en = set(fallan), cancelar_en

    def pull_model(self, model, timeout=0, on_progress=None, token=None):
        from src.domain.exceptions import LLMError
        from src.llm.client_base import GeneracionCancelada
        self.pedidos.append(model)
        if model in self.fallan:
            raise LLMError("sin red", model=model)
        if model == self.cancelar_en:
            raise GeneracionCancelada("lo pediste vos")

def test_el_instalador_baja_solo_lo_obligatorio_que_falta(monkeypatch):
    """Los opcionales pesan GB de más: sólo entran si se piden a propósito."""
    from config.model_catalog import cargar_catalogo
    from src.core import preflight as pf
    catalogo = cargar_catalogo()
    pre = _revisar(monkeypatch, [])                     # no hay ningún modelo instalado
    falso = _ClienteDeInstalacion()
    hechas = pf.instalar_faltantes(pre, falso)
    assert falso.pedidos == [catalogo.por_rol["chat"], catalogo.por_rol["codigo"]], falso.pedidos
    assert all(d.estado == "instalado" for d in hechas), hechas
    falso2 = _ClienteDeInstalacion()
    pf.instalar_faltantes(pre, falso2, incluir_opcionales=True)
    assert catalogo.por_rol["vision"] in falso2.pedidos, falso2.pedidos

def test_una_descarga_que_falla_no_frena_a_las_demas(monkeypatch):
    from config.model_catalog import cargar_catalogo
    from src.core import preflight as pf
    catalogo = cargar_catalogo()
    chat = catalogo.por_rol["chat"]
    falso = _ClienteDeInstalacion(fallan=[chat])
    hechas = pf.instalar_faltantes(_revisar(monkeypatch, []), falso)
    estados = {d.modelo: d.estado for d in hechas}
    assert estados[chat] == "fallido", estados
    assert estados[catalogo.por_rol["codigo"]] == "instalado", estados

def test_si_se_cancela_la_descarga_se_corta_y_no_sigue(monkeypatch):
    from config.model_catalog import cargar_catalogo
    from src.core import preflight as pf
    chat = cargar_catalogo().por_rol["chat"]
    falso = _ClienteDeInstalacion(cancelar_en=chat)
    hechas = pf.instalar_faltantes(_revisar(monkeypatch, []), falso)
    assert [d.estado for d in hechas] == ["cancelado"], hechas
    assert falso.pedidos == [chat], "no debería seguir con el siguiente modelo"

# ═══════════════════════════════════════════════════════════════
# FASE 1 — el preflight desde la interfaz (§3.1: "CLI y API")
# ═══════════════════════════════════════════════════════════════
# El pedido del usuario es que se pueda PREGUNTAR por lo que falta antes de que funcione; si
# sólo se puede por consola, quien usa la interfaz no tiene forma de enterarse.

def test_el_preflight_se_puede_preguntar_desde_la_interfaz(monkeypatch):
    import asyncio
    import src.core.preflight as pf
    from src.interfaces.web import routes_preflight as rp
    monkeypatch.setattr(pf, "revisar", lambda *a, **k: _pre_fabricado())
    datos = asyncio.run(rp.preflight_estado())
    assert datos["puede_responder"] is False
    assert datos["faltantes"] == ["ollama", "modelo_chat"], datos["faltantes"]
    assert datos["avisos"] == ["modelo_vision", "audio"], datos["avisos"]
    a_bajar = {d["modelo"]: d for d in datos["a_descargar"]}
    assert a_bajar["qwen3:8b"]["obligatorio"] is True
    assert a_bajar["qwen3-vl:4b"]["obligatorio"] is False
    assert "requisitos" in datos and "duracion_ms" in datos

def test_si_el_preflight_falla_la_interfaz_no_se_cae(monkeypatch):
    """Una pantalla que no carga no debe tumbar la aplicación (convención de las rutas)."""
    import asyncio
    import src.core.preflight as pf
    from src.interfaces.web import routes_preflight as rp

    def _romper(*_a, **_k):
        raise RuntimeError("sin permisos para leer el hardware")

    monkeypatch.setattr(pf, "revisar", _romper)
    datos = asyncio.run(rp.preflight_estado())
    assert datos["puede_responder"] is False
    assert "RuntimeError" in datos["error"], datos

def test_el_catalogo_se_puede_consultar_desde_la_interfaz():
    import asyncio
    from src.interfaces.web import routes_preflight as rp
    datos = asyncio.run(rp.preflight_catalogo())
    assert datos["por_rol"]["chat"], datos["por_rol"]
    assert datos["espacio_obligatorios_gb"] > 0
    chat = next(m for m in datos["modelos"] if "chat" in m["roles"])
    assert chat["instalar"].startswith("ollama pull"), chat
    assert chat["si_falta"], "el catálogo tiene que decir qué se rompe si falta"

def test_los_puntos_de_acceso_del_preflight_quedan_registrados():
    """Cableado REAL: se inspecciona la aplicación que arma el servidor.

    Ojo: importar el módulo de rutas acá NO alcanza (sería una prueba auto-complaciente: el
    import lo hago yo). Lo que hay que comprobar es que el servidor lo registre.
    """
    from src.interfaces.web.server import build_api_app
    rutas = {getattr(r, "path", "") for r in build_api_app().routes}
    # ✅ 2026-10-08: la dirección PÚBLICA (el `/api` lo pone el montaje; el router aporta `/v1`). Antes
    # esta prueba miraba las rutas del router, que pasaban aunque la pública estuviera duplicada.
    rutas = {f"/api{p}" for p in rutas}
    assert "/api/v1/preflight" in rutas, sorted(p for p in rutas if "preflight" in p)
    assert "/api/v1/preflight/catalogo" in rutas, sorted(p for p in rutas if "preflight" in p)

# ═══════════════════════════════════════════════════════════════
# Memoria POR NIVELES y placa de video (decisión del usuario, 2026-10-04)
# ═══════════════════════════════════════════════════════════════
# Corrección del usuario: "que el piso sea 16 GB y tenga 14,7 GB no significa que AXIOMA no
# puede funcionar: funcionaría con restricciones, y eso hay que mencionarlo para saber cuál es
# el límite. Para que funcione bien se necesitan 16 GB y para que funcione ideal 24/32 GB".

def test_la_memoria_se_informa_por_niveles_y_no_como_un_si_o_no(monkeypatch):
    from src.core import preflight as pf
    casos = [(8.0, pf.FALTA, "no_alcanza"), (12.0, pf.AVISO, "restringido"),
             (16.0, pf.OK, "bien"), (32.0, pf.OK, "ideal")]
    for ram, esperado, nivel in casos:
        pre = _revisar(monkeypatch, _todos_los_modelos(), ram_gb=ram)
        req = next(r for r in pre.requisitos if r.clave == "ram")
        assert req.estado == esperado, (ram, req.estado, req.detalle)
        assert req.datos.get("nivel") == nivel, (ram, req.datos)

def test_con_menos_de_16_gb_dice_la_restriccion_concreta(monkeypatch):
    """No alcanza con decir "funciona con restricciones": hay que decir CUÁL."""
    from src.core import preflight as pf
    pre = _revisar(monkeypatch, _todos_los_modelos(), ram_gb=13.0)
    req = next(r for r in pre.requisitos if r.clave == "ram")
    assert req.estado == pf.AVISO
    assert "un solo modelo cargado a la vez" in req.remedio, req.remedio
    assert "16 GB es el nivel recomendado" in req.detalle, req.detalle
    assert pre.puede_responder(), "con 13 GB tiene que poder responder igual"

def test_la_placa_de_video_se_detecta_y_no_bloquea(monkeypatch):
    from types import SimpleNamespace
    from src.core import preflight as pf
    monkeypatch.setattr(pf.shutil, "disk_usage",
                        lambda _p: SimpleNamespace(free=500 * 1024 ** 3))
    monkeypatch.setattr(pf, "_probar_audio",
                        lambda _c: pf.Requisito("audio", "Audio (voz)", pf.OK, "falso"))

    def _con_gpu(vram, utilizable, nombre):
        hw = SimpleNamespace(ram_gb=32.0, gpu_vram_gb=vram, cpu_threads=8,
                             backend="nvidia" if utilizable else "amd_igpu",
                             gpu_name=nombre,
                             effective_budget_gb=vram * 0.88 if utilizable else 1.0)
        hw.is_gpu_available = utilizable          # propiedad en el original
        return pf.revisar(cliente=_ClienteFalso(vivo=True, modelos=_todos_los_modelos()), hw=hw)

    chica = _con_gpu(2.0, False, "AMD GPU")
    req = next(r for r in chica.requisitos if r.clave == "gpu")
    assert req.estado == pf.AVISO, req
    assert "VRAM" in req.detalle and req.datos["modelos_en_placa"] == [], req
    assert chica.puede_responder(), "sin placa utilizable tiene que poder responder igual"

    grande = _con_gpu(12.0, True, "NVIDIA RTX")
    req = next(r for r in grande.requisitos if r.clave == "gpu")
    assert req.estado == pf.OK, req
    assert "VRAM" in req.detalle and req.datos["vram_gb"] == 12.0, req

def test_un_catalogo_con_los_niveles_al_reves_falla(tmp_path):
    from config.model_catalog import cargar_catalogo
    malo = tmp_path / "catalogo.yaml"
    malo.write_text(
        "version: 1\n"
        "hardware: {ram_para_funcionar_gb: 32, ram_bien_gb: 16, ram_ideal_gb: 24,\n"
        "           ram_margen_gb: 1.5, disco_factor: 1.5}\n"
        "por_rol: {chat: 'qwen3:8b'}\n"
        "modelos:\n"
        "  'qwen3:8b': {roles: [chat], obligatorio: true, tipo: ollama,\n"
        "    disco_gb: 4.9, ram_gb: 6.7, para: x, si_falta: y, instalar: z}\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="al revés"):
        cargar_catalogo(malo, usar_cache=False)

def test_las_claves_se_guardan_sin_pisar_el_resto_del_archivo(tmp_path):
    from src.core import apikeys
    archivo = _env_temporal(tmp_path)
    apikeys.guardar_valores({"TAVILY_API_KEY": "valor-de-ejemplo-1"}, archivo)
    texto = archivo.read_text(encoding="utf-8")
    assert "valor-de-ejemplo-1" in texto
    assert "# un comentario que hay que conservar" in texto, "se perdieron los comentarios"
    assert "LLM_DEFAULT_MODEL=qwen3:8b" in texto and "OLLAMA_HOST=http://127.0.0.1:11434" in texto
    assert texto.count("TAVILY_API_KEY=") == 1, "la clave quedó duplicada"

def test_una_clave_nueva_se_agrega_al_final(tmp_path):
    from src.core import apikeys
    archivo = _env_temporal(tmp_path)
    apikeys.guardar_valores({"NEWS_API_KEY": "valor-de-ejemplo-news"}, archivo)
    texto = archivo.read_text(encoding="utf-8")
    assert "NEWS_API_KEY=valor-de-ejemplo-news" in texto
    assert "# un comentario que hay que conservar" in texto

def test_el_archivo_de_claves_queda_privado_y_con_copia(tmp_path):
    import os
    from src.core import apikeys
    archivo = _env_temporal(tmp_path)
    apikeys.guardar_valores({"SERPER_API_KEY": "valor-de-ejemplo-serper"}, archivo)
    if os.name == "posix":
        assert (archivo.stat().st_mode & 0o777) == 0o600, oct(archivo.stat().st_mode & 0o777)
    respaldo = archivo.with_name(archivo.name + ".bak")
    assert respaldo.exists(), "no se hizo copia de seguridad"
    assert "SERPER_API_KEY=valor-de-ejemplo-serper" not in respaldo.read_text(encoding="utf-8"), (
        "la copia tiene que guardar el estado ANTERIOR, sin la clave nueva")

def test_el_estado_no_revela_los_valores(tmp_path):
    import json
    from src.core import apikeys
    archivo = _env_temporal(tmp_path)
    apikeys.guardar_valores({"TAVILY_API_KEY": "valor-de-ejemplo-que-no-debe-salir"}, archivo)
    volcado = json.dumps(apikeys.estado(archivo), ensure_ascii=False)
    assert "valor-de-ejemplo-que-no-debe-salir" not in volcado, "el estado no puede mostrar el valor"
    assert apikeys.valores_configurados(archivo)["TAVILY_API_KEY"] is True
    assert apikeys.valores_configurados(archivo)["NEWS_API_KEY"] is False

def test_la_carga_guiada_guarda_lo_que_escribe_el_usuario(tmp_path):
    from src.core import apikeys
    archivo = _env_temporal(tmp_path)
    respuestas = {"TAVILY_API_KEY": "valor-de-ejemplo-2", "SERPER_API_KEY": "", "NEWS_API_KEY": "-"}
    dichos = []
    apikeys.configurar(
        archivo,
        preguntar=lambda _p: "",
        pedir_oculto=lambda p: next(
            (v for k, v in respuestas.items() if k in p), ""),
        avisar=dichos.append)
    puesto = apikeys.valores_configurados(archivo)
    assert puesto["TAVILY_API_KEY"] is True
    assert puesto["SERPER_API_KEY"] is False, "con Enter no se toca"
    assert all("valor-de-ejemplo-2" not in d for d in dichos), "no puede imprimir el valor"
    assert any("Guardado en" in d for d in dichos)

def _hw_con_placa(vram_gb, utilizable=True, nombre="NVIDIA RTX"):
    from types import SimpleNamespace
    margen = 0.88
    return SimpleNamespace(ram_gb=32.0, gpu_vram_gb=vram_gb, cpu_threads=8,
                           backend="nvidia" if utilizable else "amd_igpu",
                           gpu_name=nombre, is_gpu_available=utilizable,
                           effective_budget_gb=vram_gb * margen if utilizable else 1.0)

def _pre_con_placa(monkeypatch, hw):
    from types import SimpleNamespace
    from src.core import preflight as pf
    monkeypatch.setattr(pf.shutil, "disk_usage",
                        lambda _p: SimpleNamespace(free=500 * 1024 ** 3))
    monkeypatch.setattr(pf, "_probar_audio",
                        lambda _c: pf.Requisito("audio", "Audio (voz)", pf.OK, "falso"))
    return pf.revisar(cliente=_ClienteFalso(vivo=True, modelos=_todos_los_modelos()), hw=hw)

def test_una_placa_grande_carga_los_modelos_y_lo_dice_por_rol(monkeypatch):
    from config.model_catalog import cargar_catalogo
    from src.core import preflight as pf
    catalogo = cargar_catalogo()
    pre = _pre_con_placa(monkeypatch, _hw_con_placa(12.0))
    for rol in ("chat", "codigo"):
        req = next(r for r in pre.requisitos if r.clave == f"modelo_{rol}")
        assert req.datos.get("en_placa") is True, (rol, req.detalle)
        assert "entra en la placa" in req.detalle, req.detalle
    gpu = next(r for r in pre.requisitos if r.clave == "gpu")
    assert gpu.estado == pf.OK, gpu
    assert catalogo.por_rol["codigo"] in gpu.datos["modelos_en_placa"], gpu.datos
    assert "código" in gpu.detalle, gpu.detalle

def test_una_placa_justa_carga_unos_modelos_y_no_otros(monkeypatch):
    """Con 7 GB de VRAM (presupuesto 6,16 GB) entra la visión (3,3) y NO el chat (6,67)."""
    from config.model_catalog import cargar_catalogo
    from src.core import preflight as pf
    catalogo = cargar_catalogo()
    pre = _pre_con_placa(monkeypatch, _hw_con_placa(7.0))
    gpu = next(r for r in pre.requisitos if r.clave == "gpu")
    assert gpu.estado == pf.OK
    assert catalogo.por_rol["vision"] in gpu.datos["modelos_en_placa"], gpu.datos
    assert catalogo.por_rol["chat"] not in gpu.datos["modelos_en_placa"], gpu.datos
    chat = next(r for r in pre.requisitos if r.clave == "modelo_chat")
    assert chat.datos.get("en_placa") is False and "no entra en la placa" in chat.detalle

def test_sin_placa_utilizable_todo_va_al_procesador_y_no_bloquea(monkeypatch):
    from src.core import preflight as pf
    pre = _pre_con_placa(monkeypatch, _hw_con_placa(2.0, utilizable=False, nombre="AMD GPU"))
    gpu = next(r for r in pre.requisitos if r.clave == "gpu")
    assert gpu.estado == pf.AVISO, gpu
    assert gpu.datos["modelos_en_placa"] == [], gpu.datos
    for rol in ("chat", "codigo"):
        req = next(r for r in pre.requisitos if r.clave == f"modelo_{rol}")
        assert req.datos.get("en_placa") is False
        assert "procesador" in req.detalle, req.detalle
    assert pre.puede_responder(), "sin placa tiene que poder responder igual"

class _ClienteLento:
    """Cliente falso que avanza hasta que lo cancelan (determinista, sin red)."""

    def __init__(self):
        self.llamadas = []

    def pull_model(self, modelo, timeout=0, on_progress=None, token=None):
        import time
        from src.llm.client_base import GeneracionCancelada
        self.llamadas.append(modelo)
        for porcentaje in (20, 45, 70, 90):
            if token is not None and getattr(token, "cancelado", False):
                raise GeneracionCancelada("cancelado por el usuario")
            if on_progress:
                on_progress({"modelo": modelo, "estado": "downloading", "porcentaje": porcentaje})
            time.sleep(0.05)
        if token is not None and getattr(token, "cancelado", False):
            raise GeneracionCancelada("cancelado por el usuario")
        if on_progress:
            on_progress({"modelo": modelo, "estado": "success", "porcentaje": 100.0})

def _esperar(condicion, intentos=60, pausa=0.05):
    import time
    for _ in range(intentos):
        if condicion():
            return True
        time.sleep(pausa)
    return False

def test_una_descarga_sin_nada_pendiente_termina_al_instante():
    from src.core.preflight import OK, Preflight, Requisito, TrabajoDeDescarga
    pre = Preflight(requisitos=[Requisito("modelo_chat", "chat", OK, "instalado")])
    trabajo = TrabajoDeDescarga(pre, _ClienteLento()).iniciar()
    assert trabajo.estado()["estado"] == "terminado"
    assert trabajo.estado()["modelos"] == []

def test_el_trabajo_informa_avance_real_y_no_bloquea():
    from src.core.preflight import TrabajoDeDescarga
    trabajo = TrabajoDeDescarga(_pre_fabricado(), _ClienteLento()).iniciar()
    assert _esperar(lambda: (trabajo.estado()["modelos"][0]["porcentaje"] or 0) >= 20), \
        trabajo.estado()
    info = trabajo.estado()
    assert info["estado"] == "descargando" and info["puede_detenerse"] is True
    assert "descargando" in trabajo.resumen(), trabajo.resumen()

def test_el_trabajo_se_puede_detener_de_verdad():
    from src.core.preflight import TrabajoDeDescarga
    cliente = _ClienteLento()
    trabajo = TrabajoDeDescarga(_pre_fabricado(), cliente).iniciar()
    assert _esperar(lambda: trabajo.estado()["modelos"][0]["porcentaje"] is not None), \
        "no llegó a empezar"
    trabajo.detener()
    assert _esperar(lambda: trabajo.estado()["estado"] == "cancelado"), trabajo.estado()
    assert cliente.llamadas, "no llegó a pedir ningún modelo"

def test_se_puede_arrancar_ver_avance_y_detener_desde_la_interfaz(monkeypatch):
    import asyncio
    import src.core.preflight as pf
    import src.llm.client as cliente_mod
    from src.interfaces.web import routes_preflight as rp

    monkeypatch.setattr(cliente_mod, "OllamaClient", _ClienteLento)
    monkeypatch.setattr(pf, "revisar", lambda *a, **k: _pre_fabricado())

    arrancado = asyncio.run(rp.preflight_instalar())
    ident = arrancado.get("identificador")
    assert ident, arrancado
    assert arrancado["estado"] in ("descargando", "terminado"), arrancado

    assert _esperar(lambda: (asyncio.run(rp.preflight_avance(ident))["avance_global"] or 0) > 0), \
        "el avance nunca llegó"
    detenido = asyncio.run(rp.preflight_detener(ident))
    assert _esperar(lambda: asyncio.run(rp.preflight_avance(ident))["estado"] == "cancelado"), \
        detenido
    assert asyncio.run(rp.preflight_avance("no-existe"))["estado"] == "desconocido"

def test_el_punto_de_acceso_de_claves_solo_guarda_las_que_el_proyecto_usa(tmp_path, monkeypatch):
    import asyncio
    import src.core.apikeys as apikeys
    from src.interfaces.web import routes_preflight as rp

    monkeypatch.setattr(apikeys, "ENV_POR_DEFECTO", tmp_path / ".env")
    datos = asyncio.run(rp.preflight_guardar_claves(
        {"TAVILY_API_KEY": "valor-de-ejemplo-tavily", "CLAVE_INVENTADA": "no-deberia-guardarse"}))
    assert datos["guardadas"] == ["TAVILY_API_KEY"], datos
    assert datos["ignoradas"] == ["CLAVE_INVENTADA"], datos
    assert apikeys.valores_configurados(tmp_path / ".env")["TAVILY_API_KEY"] is True
    assert "CLAVE_INVENTADA" not in (tmp_path / ".env").read_text(encoding="utf-8")

def test_el_resumen_de_la_pantalla_traduce_los_estados():
    from src.core.preflight import AVISO, FALTA, OK, Preflight, Requisito
    from src.interfaces.components.configurar_axioma import resumen_para_pantalla
    pre = Preflight(requisitos=[
        Requisito("ram", "Memoria RAM", OK, "14,7 GB"),
        Requisito("gpu", "Placa de video", AVISO, "no alcanza", "opcional"),
        Requisito("modelo_chat", "Modelo de chat", FALTA, "no está", "ollama pull qwen3:8b"),
    ])
    filas = resumen_para_pantalla(pre)
    assert [f["icono"] for f in filas] == ["✅", "⚠️", "❌"], filas
    assert filas[2]["remedio"] == "ollama pull qwen3:8b"
    assert filas[0]["remedio"] == "", "lo que está bien no necesita remedio"

def test_la_pantalla_de_configuracion_esta_enganchada_en_el_encabezado():
    """Cableado: un botón que no está en la barra no existe para el usuario."""
    import inspect
    from src.interfaces.web import app as web_app
    barra = inspect.getsource(web_app._crear_barra_de_acciones)
    assert "_on_configurar_click" in barra, "falta el botón en la barra de acciones"
    assert "Configurar AXIOMA" in barra, "el botón tiene que decir qué hace"
    assert callable(web_app._on_configurar_click)
    fuente = inspect.getsource(web_app._on_configurar_click)
    assert "abrir_configurar_axioma" in fuente, "el botón tiene que abrir la pantalla"

# ── Servicio de VOZ en el contenedor (`ISSUE-150`) ──
# Medido: el demonio necesitaba systemd (no hay `systemctl` en el contenedor y abortaba), el nombre
# de dispositivo del equipo (`pulse`) no existe adentro y abrir la placa directo daba "Invalid sample
# rate". Se resolvió con modo directo + el puente de ALSA al servidor de sonido del equipo.

def test_el_daemon_puede_correr_sin_systemd(monkeypatch):
    """En el contenedor no hay `systemctl`: el modo directo corre el daemon en ESTE proceso."""
    import asyncio
    import Rafael

    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("AXIOMA_RAFAEL_DIRECTO", raising=False)
    assert Rafael._modo_directo(["--directo"]) is True
    assert Rafael._modo_directo([]) is False
    monkeypatch.setenv("AXIOMA_RAFAEL_DIRECTO", "1")
    assert Rafael._modo_directo([]) is True

def test_el_daemon_en_modo_directo_no_llama_a_systemctl(monkeypatch):
    """El despacho real: con `--directo` NO tiene que intentar arrancar la unidad de systemd.

    OJO (trampa propia ya anotada): la primera versión de esta prueba marcaba la rama "manual" con
    un ayudante que ignoraba sus argumentos ⇒ **no cazaba la mutación**. Ahora las dos ramas dejan
    una marca de verdad.
    """
    import asyncio
    import sys
    import Rafael

    llamado = {}

    async def _falso_daemon(modo):
        llamado["modo"] = modo

    async def _falso_manual():
        llamado["manual"] = True

    monkeypatch.setattr(Rafael, "_run_daemon", _falso_daemon)
    monkeypatch.setattr(Rafael, "_run_manual_control", _falso_manual)
    monkeypatch.setattr(Rafael, "setup_logging", lambda: None)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("AXIOMA_RAFAEL_DIRECTO", raising=False)
    # ✅ 2026-10-08: DENTRO de la imagen `AXIOMA_SIN_AUDIO=1` viene puesta (el Dockerfile la declara: el
    # contenedor no tiene audio). Medido: sin borrarla, `Rafael._main()` se retira antes de despachar y
    # esta prueba fallaba SÓLO adentro del contenedor (`assert None == 'silent'`). Acá se prueba el
    # DESPACHO, no el guardián del audio: se limpia la bandera a propósito.
    monkeypatch.delenv("AXIOMA_SIN_AUDIO", raising=False)
    monkeypatch.setattr(sys, "argv", ["Rafael.py", "--mode", "silent", "--directo"])
    asyncio.run(Rafael._main())
    assert llamado.get("modo") == "silent", llamado
    assert "manual" not in llamado, "no puede intentar systemctl en modo directo"

    # Y sin el flag (y sin systemd) SÍ tiene que ir por el camino manual.
    llamado.clear()
    monkeypatch.setattr(sys, "argv", ["Rafael.py", "--mode", "silent"])
    asyncio.run(Rafael._main())
    assert llamado.get("manual") is True, llamado

def test_el_dispositivo_de_entrada_cae_al_del_sistema_si_el_configurado_no_existe(monkeypatch):
    """`pulse` existe en el equipo y no en el contenedor: se usa el del sistema en vez de fallar."""
    import config.multimodal_loader as ml
    monkeypatch.setenv("AXIOMA_INPUT_DEVICE", "dispositivo-que-no-existe-en-esta-maquina")
    assert ml.get_input_device() is None, "tiene que devolver None (lo elige la librería)"
    try:
        import sounddevice as sd
        reales = [str(d.get("name")) for d in sd.query_devices()]
    except Exception:  # noqa: BLE001
        reales = []
    if reales:
        monkeypatch.setenv("AXIOMA_INPUT_DEVICE", reales[0])
        assert ml.get_input_device() == reales[0], "si el dispositivo existe, se respeta"

# Los archivos de empaquetado (Dockerfile, compose) NO entran en la imagen: dentro del contenedor
# esta comprobación se saltea con el motivo dicho (igual que en `test_axioma_27`).
_sin_empaquetado = pytest.mark.skipif(
    not (PROJECT_ROOT / "Dockerfile").exists(),
    reason="los archivos de empaquetado no viajan dentro de la imagen")

@_sin_empaquetado
def test_el_servicio_de_voz_esta_listo_para_el_contenedor():
    """Guardián del diseño medido: si alguien saca una pieza, la voz deja de andar en el contenedor."""
    import yaml
    comp = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    voz = comp["services"]["voz"]
    assert voz.get("profiles"), "la voz es opcional: detrás de un perfil"
    assert voz["command"] == ["python", "Rafael.py", "--mode", "jarvis", "--directo"], voz["command"]
    assert voz["environment"]["AXIOMA_RAFAEL_DIRECTO"] == "1", "sin esto pide systemd y aborta"
    assert any("pulse" in str(v) for v in voz["volumes"]), "falta el servidor de sonido del equipo"
    # Y sus registros NO pueden ir a `logs/` del proyecto: sus errores ensucian el informe de salud
    # y el portero bloquea (medido el 2026-10-05 con el contenedor de voz encendido).
    assert not any(v == "./logs:/app/logs" for v in voz["volumes"]), (
        "la voz no puede escribir en los registros del proyecto")
    assert any("registros_voz" in str(v) for v in voz["volumes"]), "faltan sus registros propios"
    assert "PULSE_SERVER" in voz["environment"], "falta apuntar al socket del servidor de sonido"
    assert voz.get("devices"), "faltan los dispositivos de audio (/dev/snd)"
    assert voz.get("group_add"), "falta el grupo de audio del equipo"
    imagen = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "libasound2-plugins" in imagen, "falta el puente de ALSA al servidor de sonido"

# ── Lanzador para doble clic (sin terminal) ──
# Pedido del usuario: "que sea lo más fácil para el usuario que no sabe de programación, para que no
# tenga que usar la terminal para instalar nada". El lanzador decide solo si usa el Ollama del equipo
# o levanta el que viene con AXIOMA. Se prueba la DECISIÓN con `--dry-run` y falsos en el PATH.

def _lanzador_con(tmp_path, docker_ok=True, ollama_en_el_equipo=False, audio_gid=None):
    """Corre el lanzador en modo ensayo con `docker` y `curl` falsos.

    Con `audio_gid` se simula el grupo `audio` de OTRA distribución (Debian/Ubuntu 29, Fedora 63):
    el lanzador tiene que usar el del equipo, no un número fijo.
    """
    import subprocess
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text("#!/bin/sh\nexit %d\n" % (0 if docker_ok else 1), encoding="utf-8")
    (bin_dir / "curl").write_text("#!/bin/sh\nexit %d\n" % (0 if ollama_en_el_equipo else 1),
                                  encoding="utf-8")
    if audio_gid is not None:
        (bin_dir / "getent").write_text(
            '#!/bin/sh\n[ "$1" = "group" ] && echo "audio:x:%s:"\nexit 0\n' % audio_gid,
            encoding="utf-8")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    entorno = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run([str(PROJECT_ROOT / "instalar" / "iniciar_axioma.sh"), "--dry-run"],
                          capture_output=True, text=True, env=entorno, timeout=60)

def test_sin_ollama_en_el_equipo_el_lanzador_levanta_el_de_axioma(tmp_path):
    salida = _lanzador_con(tmp_path, ollama_en_el_equipo=False)
    assert salida.returncode == 0, salida.stderr
    assert "ollama-local" in salida.stdout, salida.stdout
    assert "Levanto el Ollama que viene con AXIOMA" in salida.stdout, salida.stdout

def test_con_ollama_en_el_equipo_el_lanzador_usa_ese_y_no_levanta_otro(tmp_path):
    salida = _lanzador_con(tmp_path, ollama_en_el_equipo=True)
    assert salida.returncode == 0, salida.stderr
    assert "--profile ollama-local" not in salida.stdout, salida.stdout
    assert "uso ese" in salida.stdout, salida.stdout

def test_si_docker_no_responde_el_lanzador_lo_explica_y_no_sigue(tmp_path):
    """Dos casos: "no está instalado" y "no responde". Acá se prueba el segundo (el primero pide un
    PATH sin el Docker del sistema, que no se puede simular sin romper el resto del guion)."""
    salida = _lanzador_con(tmp_path, docker_ok=False)
    assert salida.returncode == 4, (salida.returncode, salida.stdout)
    assert "Docker" in salida.stdout, salida.stdout
    assert "Docker Desktop" in salida.stdout or "docker.com" in salida.stdout, salida.stdout

def test_el_acceso_directo_no_abre_la_terminal():
    """«Que no tenga que usar la terminal»: el acceso directo tiene que arrancar sin consola."""
    import subprocess
    guion = PROJECT_ROOT / "instalar" / "instalar_axioma.sh"
    salida = subprocess.run(["bash", str(guion), "--dry-run"], capture_output=True, text=True,
                            timeout=60, cwd=str(PROJECT_ROOT))
    # Sin Docker (por ejemplo DENTRO de la imagen) sale 4 y lo explica; con Docker, muestra el acceso.
    assert salida.returncode in (0, 4), (salida.returncode, salida.stderr)
    esperado = "Docker" if salida.returncode == 4 else "axioma.desktop"
    assert esperado in salida.stdout, salida.stdout
    fuente = guion.read_text(encoding="utf-8")
    assert "Terminal=false" in fuente, "el acceso directo abriría una terminal"
    assert "iniciar_axioma.sh" in fuente, "el acceso directo tiene que arrancar AXIOMA"
    for reloj in ("iniciar_axioma.sh",):
        assert (PROJECT_ROOT / "instalar" / reloj).exists()

def test_la_guia_de_primer_arranque_se_muestra_una_sola_vez(tmp_path):
    """Se abre sola la PRIMERA vez y después no vuelve a molestar (se marca en `data/`)."""
    from src.interfaces.components.configurar_axioma import (
        es_primer_arranque, marcar_primer_arranque)
    marca = tmp_path / ".primer_arranque_listo"
    assert es_primer_arranque(marca) is True, "la primera vez tiene que abrirse"
    marcar_primer_arranque(marca)
    assert es_primer_arranque(marca) is False, "la segunda vez ya no"

def test_la_pantalla_principal_abre_la_guia_la_primera_vez():
    """Cableado: si la página no llama a la guía, el usuario nuevo no ve nada."""
    import inspect
    from src.interfaces.web import app as web_app
    fuente = inspect.getsource(web_app.main_page)
    assert "es_primer_arranque()" in fuente, "la página no comprueba si es la primera vez"
    assert "abrir_configurar_axioma(primer_arranque=True)" in fuente, "no abre la guía"
    assert "once=True" in fuente, "tiene que ser un único disparo (nada de sondeo — R7)"

# ── Modo de trabajo: de a uno o EN PARALELO (pedido del usuario) ──

def _hw_simulado(vram_gb, ram_gb, utilizable=True):
    from types import SimpleNamespace
    return SimpleNamespace(is_gpu_available=utilizable, effective_budget_gb=vram_gb * 0.88,
                           ram_gb=ram_gb, gpu_vram_gb=vram_gb, backend="nvidia",
                           gpu_name="NVIDIA RTX", cpu_threads=8)

def test_el_modo_paralelo_solo_con_placa_y_memoria_suficientes():
    """Con placa que alcanza para el modelo más pesado Y memoria para el otro: en paralelo."""
    from config.model_catalog import cargar_catalogo
    from src.core.hardware_fit import decidir_modo_de_trabajo
    catalogo = cargar_catalogo()
    assert decidir_modo_de_trabajo(_hw_simulado(12.0, 32.0), catalogo) == "paralelo"
    assert decidir_modo_de_trabajo(_hw_simulado(2.0, 32.0, utilizable=False), catalogo) == "simple"
    # Placa de sobra pero memoria justa: el otro modelo no convive ⇒ de a uno.
    assert decidir_modo_de_trabajo(_hw_simulado(12.0, 7.0), catalogo) == "simple"

def test_en_modo_paralelo_no_se_descarga_el_modelo_anterior(monkeypatch):
    """El comportamiento que pide el usuario: no perder tiempo recargando si pueden convivir."""
    import threading
    from src.core import model_swap

    def _swap(paralelo: bool):
        ms = model_swap.ModelSwap.__new__(model_swap.ModelSwap)
        ms._state_lock = threading.Lock()
        ms._swap_lock = threading.Lock()
        ms._current_model = "modelo-viejo"
        llamado = {"descargas": [], "cargas": []}
        monkeypatch.setattr(ms, "_check_model_fit", lambda *a, **k: None)
        monkeypatch.setattr(ms, "_unload_model", lambda m: llamado["descargas"].append(m))
        monkeypatch.setattr(ms, "_load_model", lambda m: llamado["cargas"].append(m))
        monkeypatch.setattr(ms, "_trabaja_en_paralelo", lambda *a, **k: paralelo)
        ms.ensure("modelo-nuevo")
        return llamado

    en_paralelo = _swap(True)
    assert en_paralelo["descargas"] == [], "en paralelo NO se descarga el anterior"
    assert en_paralelo["cargas"] == ["modelo-nuevo"], en_paralelo

    de_a_uno = _swap(False)
    assert de_a_uno["descargas"] == ["modelo-viejo"], "en modo simple sí se descarga (como siempre)"

# ── Nadie usa el usuario del autor: cada persona crea el suyo (`ISSUE-151`) ──
# Pedido del usuario: "usuario no usará mi usuario, tendrá que crear un usuario nuevo obligatoriamente
# antes de usarlo". Antes el producto traía mi identidad cocida (y como root).

def test_una_instalacion_nueva_no_trae_ningun_usuario_sembrado(monkeypatch):
    """Sin identidad configurada, la lista de usuarios arranca VACÍA (la interfaz pide crear uno)."""
    from config.settings import settings
    from src.memory import user_registry
    monkeypatch.setattr(settings, "axioma_user_id", "")
    assert user_registry._default_users() == [], "no puede sembrar un usuario vacío"
    monkeypatch.setattr(settings, "axioma_user_id", "persona")
    monkeypatch.setattr(settings, "axioma_user_role", "user")
    sembrados = user_registry._default_users()
    assert [u["username"] for u in sembrados] == ["persona"], sembrados
    assert sembrados[0]["role"] == "user", "se respeta el rol configurado"

def test_la_identidad_por_defecto_es_vacia_y_no_de_administrador():
    """La comprobación de fábrica: sin `.env`, AXIOMA no trae identidad ni privilegios."""
    from config.settings import Settings
    campos = Settings.model_fields
    assert campos["axioma_user_id"].default == "", "el usuario por defecto tiene que estar vacío"
    assert campos["axioma_user_role"].default == "user", "por defecto NO se entra como root"

@pytest.mark.skipif(not (PROJECT_ROOT / "docker-compose.yml").exists(),
                    reason="los archivos de empaquetado no viajan dentro de la imagen")
def test_lo_que_se_distribuye_no_lleva_el_usuario_del_autor():
    """Guardián de la DISTRIBUCIÓN: ni el compose ni la plantilla ni los lanzadores traen mi usuario."""
    import yaml
    a_revisar = [PROJECT_ROOT / "docker-compose.yml", PROJECT_ROOT / ".env.example",
                 PROJECT_ROOT / "Dockerfile"] + sorted((PROJECT_ROOT / "instalar").glob("*.sh"))
    ofensas = []
    for archivo in a_revisar:
        for numero, linea in enumerate(archivo.read_text(encoding="utf-8").splitlines(), 1):
            if linea.lstrip().startswith("#"):
                continue
            if "saintwick" in linea or "AXIOMA_USER_ROLE:-root" in linea:
                ofensas.append(f"{archivo.name}:{numero}")
    assert ofensas == [], f"el usuario del autor aparece en lo que se distribuye: {ofensas}"
    comp = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    entorno = comp["services"]["axioma"]["environment"]
    assert entorno["AXIOMA_USER_ID"] in ("", "${AXIOMA_USER_ID:-}"), entorno["AXIOMA_USER_ID"]
    assert entorno["AXIOMA_USER_ROLE"] == "${AXIOMA_USER_ROLE:-user}", entorno["AXIOMA_USER_ROLE"]

def test_el_codigo_del_producto_no_tiene_un_usuario_cocido():
    """Guardián de `ISSUE-151`: ni respaldos en el código ni textos con el usuario del autor.

    Nació de un hallazgo real: tres módulos tenían `getattr(settings, "axioma_user_id", "<usuario
    del autor>")` como respaldo y la interfaz nombraba a ese usuario en pantalla. Con eso, quien
    instalaba AXIOMA podía terminar usando MI identidad.
    """
    import re
    archivos = [PROJECT_ROOT / "main.py", PROJECT_ROOT / "Rafael.py"]
    archivos += sorted((PROJECT_ROOT / "src").rglob("*.py"))
    archivos += sorted((PROJECT_ROOT / "config").rglob("*.py"))
    ofensas = []
    for archivo in archivos:
        for numero, linea in enumerate(archivo.read_text(encoding="utf-8").splitlines(), 1):
            limpia = linea.strip()
            if (limpia.startswith("#") or "Ruta:" in linea or "Autor:" in linea
                    or limpia.startswith("__author__")):
                continue          # los encabezados y metadatos de autoría se conservan (decisión del usuario)
            if re.search(r"saintwick", linea, re.IGNORECASE):
                ofensas.append(f"{archivo.relative_to(PROJECT_ROOT)}:{numero}")
    assert ofensas == [], f"el usuario del autor está cocido en el producto: {ofensas}"

# ── "¿Este modelo va a andar en mi PC?" (pedido del usuario, fácil como Odysseus) ──

def test_el_chequeo_de_modelo_avisa_si_no_entra_en_el_equipo():
    from src.core.preflight import evaluar_modelo_en_la_pc
    grande = evaluar_modelo_en_la_pc("llama3:70b")          # no instalado: se estima por el nombre
    assert grande["nivel"] == "no_fit", grande
    assert "No" in grande["veredicto"] and "46" in grande["detalle"], grande
    assert grande.get("aviso"), "tiene que aclarar que es una estimación"

def test_el_chequeo_de_modelo_usa_el_tamano_real_cuando_esta_instalado():
    from src.core.preflight import evaluar_modelo_en_la_pc
    chico = evaluar_modelo_en_la_pc("qwen3:8b")             # instalado en el equipo de prueba
    if not chico["instalado"]:
        chico = evaluar_modelo_en_la_pc("modelo:7b")        # sin Ollama: cae a la estimación
        assert chico["nivel"] in ("marginal", "no_fit", "good"), chico
        return
    assert chico["memoria_gb"] and chico["donde"], chico
    assert chico["veredicto"].startswith(("✅", "⚠️")), chico

def test_un_nombre_raro_no_inventa_un_veredicto():
    from src.core.preflight import evaluar_modelo_en_la_pc
    d = evaluar_modelo_en_la_pc("cualquier-cosa")
    assert d["nivel"] == "desconocido" and "No puedo" in d["veredicto"], d
    assert evaluar_modelo_en_la_pc("")["nivel"] == "desconocido"

def test_la_pantalla_ofrece_el_chequeo_de_modelo():
    """Cableado: la pantalla tiene que ofrecerlo (si no, el usuario no lo encuentra)."""
    import inspect
    from src.interfaces.components import configurar_axioma as pantalla
    fuente = inspect.getsource(pantalla.abrir_configurar_axioma)
    assert "_seccion_chequeo_de_modelo()" in fuente, "la pantalla no muestra el chequeo"
    seccion = inspect.getsource(pantalla._seccion_chequeo_de_modelo)
    assert "evaluar_modelo_en_la_pc" in seccion and "Revisar" in seccion, seccion


def test_el_lanzador_usa_el_grupo_de_audio_del_equipo(tmp_path):
    """Medido el 2026-10-07: el compose tenía 996 fijo, y el grupo `audio` NO es el mismo en todas las
    distribuciones (Debian/Ubuntu 29 · Fedora 63 · Arch 996). Con el número fijo, la voz no ve los
    dispositivos y el motivo no se entiende.
    """
    salida = _lanzador_con(tmp_path, audio_gid=29)          # como en Debian/Ubuntu
    assert salida.returncode == 0, (salida.returncode, salida.stdout)
    assert "grupo audio 29" in salida.stdout, salida.stdout
    assert "grupo audio 996" not in salida.stdout, "usó el valor fijo en vez del del equipo"


def test_el_compose_no_tiene_uid_ni_gid_de_audio_fijos():
    """El socket del servidor de sonido lleva el UID (`/run/user/<UID>/pulse`): 1000 fijo falla si tu
    usuario tiene otro UID (medido). Y el grupo de audio entra por `AXIOMA_AUDIO_GID`.
    """
    crudo = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    codigo = "\n".join(l for l in crudo.splitlines() if not l.lstrip().startswith("#"))
    assert "/run/user/1000" not in codigo, "quedó el UID 1000 fijo en el compose"
    assert "/run/user/${AXIOMA_UID:-1000}" in codigo, "el socket tiene que usar el UID del equipo"
    assert "${AXIOMA_AUDIO_GID:-996}" in codigo, "el grupo de audio tiene que venir del equipo"
    # Y los dos guiones que arrancan AXIOMA tienen que calcularlos.
    for guion in ("instalar/iniciar_axioma.sh", "instalar/instalar_axioma.sh"):
        fuente = (PROJECT_ROOT / guion).read_text(encoding="utf-8")
        for calculo in ("AXIOMA_UID=\"$(id -u)\"", "getent group audio", "export AXIOMA_AUDIO_GID"):
            assert calculo in fuente, f"{guion} no calcula {calculo}"
