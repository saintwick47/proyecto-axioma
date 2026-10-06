#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Preflight: ¿qué le falta a este equipo para funcionar?
# ═══════════════════════════════════════════════════════════════
# ✅ Fase 1 del plan de contenedores (`docs/PLAN_CONTENEDORES.md` §3.1 y §3.2).
#
# Pedido del usuario: *"permitiendo al usuario preguntar por lo que se necesita instalar
# antes de que funcione"*. Esto es esa respuesta: recorre los requisitos, dice cuáles
# están, cuáles faltan, **qué se rompe** si faltan y **con qué comando** se resuelven, y
# termina en un veredicto honesto (nada de decir "listo" cuando no puede responder —
# `ISSUE-146`).
#
# Decisión de diseño (R14: no crear archivos si la lógica entra en uno existente): el plan
# proponía `scripts/preflight.py`; el proyecto **no tiene** carpeta `scripts/`, así que la
# API y la línea de órdenes viven en este módulo del núcleo:
#     python -m src.core.preflight          (informe legible)
#     python -m src.core.preflight --json   (para la interfaz o para un instalador)
#
# Lo que NO hace: descargar nada. Dice qué falta y cómo hacerlo; la descarga guiada (con
# progreso y cancelación, R7) es el paso siguiente.
from __future__ import annotations

import json
import logging
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Estados posibles de un requisito
OK = "ok"
AVISO = "aviso"          # se puede funcionar, pero algo queda degradado
FALTA = "falta"          # sin esto AXIOMA no puede responder

_ICONO = {OK: "✅", AVISO: "⚠️ ", FALTA: "❌"}


@dataclass(frozen=True)
class Requisito:
    """Un requisito concreto, con qué hacer si falta."""

    clave: str
    titulo: str
    estado: str
    detalle: str
    remedio: str = ""
    datos: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Preflight:
    """Resultado completo del chequeo previo."""

    requisitos: List[Requisito]

    def faltantes(self) -> List[Requisito]:
        return [r for r in self.requisitos if r.estado == FALTA]

    def avisos(self) -> List[Requisito]:
        return [r for r in self.requisitos if r.estado == AVISO]

    def puede_responder(self) -> bool:
        """¿Está todo lo que hace falta para responder? (los avisos no bloquean)"""
        return not self.faltantes()

    def veredicto(self) -> str:
        if self.puede_responder():
            return "AXIOMA puede responder." if not self.avisos() else \
                "AXIOMA puede responder (con avisos)."
        return "AXIOMA todavía NO puede responder: falta lo marcado con ❌."

    def como_dict(self) -> Dict[str, Any]:
        return {
            "puede_responder": self.puede_responder(),
            "veredicto": self.veredicto(),
            "faltantes": [r.clave for r in self.faltantes()],
            "avisos": [r.clave for r in self.avisos()],
            "requisitos": [asdict(r) for r in self.requisitos],
        }


# ═══════════════════════════════════════════════════════════════
# SONDAS (una por requisito; separadas para poder probarlas)
# ═══════════════════════════════════════════════════════════════

def _probar_python() -> Requisito:
    """El intérprete y las dependencias mínimas para importar la configuración."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 12):
        return Requisito("python", "Intérprete de Python", FALTA,
                         f"tenés Python {version} y hace falta 3.12 o más nuevo",
                         "instalá Python 3.12+ (o usá la imagen de contenedor)")
    try:
        import pydantic_settings  # noqa: F401
        tiene_deps = True
    except ImportError:
        tiene_deps = False
    if not tiene_deps:
        return Requisito("python", "Intérprete de Python", FALTA,
                         f"Python {version} está bien, pero faltan las dependencias del proyecto",
                         "instalá las dependencias: `pip install -r requirements.txt`")
    return Requisito("python", "Intérprete de Python", OK, f"Python {version} con dependencias")


def _probar_ollama(cliente: Any) -> Requisito:
    """El motor de modelos: sin él no hay nada que responder."""
    host = getattr(cliente, "base_url", "") or getattr(cliente, "host", "") or "127.0.0.1:11434"
    try:
        vivo = bool(cliente.health_check())
    except Exception as e:  # noqa: BLE001  (cualquier falla de red es "no responde")
        logger.debug(f"[preflight] Ollama no responde: {e}")
        vivo = False
    if vivo:
        return Requisito("ollama", "Motor de modelos (Ollama)", OK, f"responde en {host}",
                         datos={"host": str(host)})
    return Requisito(
        "ollama", "Motor de modelos (Ollama)", FALTA,
        f"no responde en {host}",
        "instalalo con `curl -fsSL https://ollama.com/install.sh | sh` y arrancalo con "
        "`ollama serve`; si ya lo tenés en otro equipo, apuntá `OLLAMA_HOST` a esa dirección",
        datos={"host": str(host)},
    )


def _probar_modelos(cliente: Any, catalogo: Any, instalados: List[str],
                    hw: Any = None) -> List[Requisito]:
    """Modelo por rol: los obligatorios sin los que no se puede responder, y los opcionales."""
    requisitos: List[Requisito] = []
    for rol, nombre in catalogo.por_rol.items():
        modelo = catalogo.modelo_de_rol(rol)
        if modelo is None:                      # el catálogo ya valida esto al leerse
            continue
        if modelo.tipo == "piper":              # la voz se verifica por ARCHIVO, no en Ollama
            archivo = Path(modelo.archivo or "")
            from config.paths import Paths
            completo = Paths.resolve(archivo) if str(archivo) else None
            if completo and completo.exists():
                requisitos.append(Requisito(f"modelo_{rol}", f"Modelo de {rol}", OK,
                                            f"{modelo.nombre} (presente)"))
            else:
                requisitos.append(Requisito(
                    f"modelo_{rol}", f"Modelo de {rol}", AVISO,
                    f"falta el archivo de voz ({modelo.archivo})",
                    "descargalo a `data/piper/` o usá la voz con internet "
                    "(edge-tts) — el texto sigue funcionando"))
            continue
        presente = nombre in instalados or f"{nombre}:latest" in instalados
        # ¿Va a la placa o al procesador? (el modelo de código es el que más lo aprovecha)
        en_placa = entra_en_placa(modelo, hw)
        if bool(getattr(hw, "is_gpu_available", False)):
            donde = ("· entra en la placa de video (más rápido y libera memoria del equipo)"
                     if en_placa else "· no entra en la placa: se usa el procesador")
        else:
            donde = "· se usa el procesador (no hay placa utilizable)"
        if presente:
            requisitos.append(Requisito(f"modelo_{rol}", f"Modelo de {rol}", OK,
                                        f"{nombre} · {modelo.ram_gb} GB en memoria {donde}",
                                        datos={"modelo": nombre, "ram_gb": modelo.ram_gb,
                                               "obligatorio": modelo.obligatorio,
                                               "en_placa": en_placa}))
        else:
            requisitos.append(Requisito(
                f"modelo_{rol}", f"Modelo de {rol}",
                FALTA if modelo.obligatorio else AVISO,
                f"no está descargado: {nombre} ({modelo.disco_gb} GB en disco)",
                f"`{modelo.instalar}` — {modelo.si_falta} {donde}",
                datos={"modelo": nombre, "disco_gb": modelo.disco_gb,
                       "obligatorio": modelo.obligatorio, "en_placa": en_placa}))
    return requisitos


def entra_en_placa(modelo: Any, hw: Any) -> bool:
    """¿Este modelo entra en la placa de video?

    ✅ Pedido del usuario (2026-10-04): *"debería detectar placa de video y la capacidad de ésta,
    eso también sumaría en mejoras y podría utilizarse para los modelos de código"*.

    No inventa un cálculo nuevo: usa `effective_budget_gb` del proyecto (`hardware_fit`), que ya
    aplica el margen según el tipo de placa (88 % en una placa dedicada, 45 % en una integrada, y
    memoria del equipo si no hay placa utilizable). Un modelo que entra en la placa va más rápido y
    **libera memoria del equipo**, que es justo lo que hace falta para el modelo de código.
    """
    if hw is None or not bool(getattr(hw, "is_gpu_available", False)):
        return False
    return float(modelo.ram_gb) <= float(getattr(hw, "effective_budget_gb", 0) or 0)


def menor_modelo_gb(catalogo: Any) -> float:
    """RAM del modelo más chico del catálogo (lo mínimo que habría que meter en la placa)."""
    return min((m.ram_gb for m in catalogo.modelos.values() if m.tipo == "ollama"), default=0.0)


def _probar_hardware(catalogo: Any, hw: Any) -> List[Requisito]:
    """RAM total (piso decidido por el usuario) y RAM disponible para el modelo más grande."""
    req = catalogo.hardware
    ram_gb = float(getattr(hw, "ram_gb", 0) or 0)
    requisitos: List[Requisito] = []
    # ✅ Decisión del usuario (2026-10-04): la memoria va POR NIVELES. Con menos de 16 GB
    # AXIOMA igual funciona, con restricciones; hay que decir CUÁLES en vez de decir "no".
    nivel = req.nivel(ram_gb)
    if nivel == "desconocido":
        requisitos.append(Requisito("ram", "Memoria RAM", AVISO,
                                    "no se pudo medir la memoria del equipo",
                                    "revisá que `/proc/meminfo` sea legible"))
    elif nivel == "no_alcanza":
        requisitos.append(Requisito(
            "ram", "Memoria RAM", FALTA,
            f"el equipo tiene {ram_gb:.1f} GB y el modelo de chat necesita "
            f"{req.ram_para_funcionar_gb:.0f} GB (modelo + margen + núcleo)",
            "con esta memoria el modelo de chat no entra: hace falta más RAM o un equipo "
            "donde correrlo (AXIOMA puede usar el Ollama de otro equipo con `OLLAMA_HOST`)",
            datos={"nivel": nivel, "ram_gb": round(ram_gb, 2)}))
    elif nivel == "restringido":
        requisitos.append(Requisito(
            "ram", "Memoria RAM", AVISO,
            f"{ram_gb:.1f} GB: AXIOMA funciona, con restricciones (16 GB es el nivel "
            f"recomendado, 24 GB el ideal)",
            req.restricciones.get("bajo_16_gb", "un solo modelo cargado a la vez"),
            datos={"nivel": nivel, "ram_gb": round(ram_gb, 2),
                   "restriccion": req.restricciones.get("bajo_16_gb", "")}))
    elif nivel == "bien":
        requisitos.append(Requisito(
            "ram", "Memoria RAM", OK,
            f"{ram_gb:.1f} GB: funciona bien (ideal desde {req.ram_ideal_gb:.0f} GB)",
            datos={"nivel": nivel, "ram_gb": round(ram_gb, 2)}))
    else:
        requisitos.append(Requisito(
            "ram", "Memoria RAM", OK, f"{ram_gb:.1f} GB: nivel ideal",
            datos={"nivel": nivel, "ram_gb": round(ram_gb, 2)}))

    # Placa de video: NO es obligatoria (el diseño es "primero CPU"), pero suma: con VRAM
    # suficiente el modelo vive en la placa, va más rápido y libera memoria del equipo.
    vram = float(getattr(hw, "gpu_vram_gb", 0) or 0)
    nombre_gpu = str(getattr(hw, "gpu_name", "") or "")
    utilizable = bool(getattr(hw, "is_gpu_available", False))
    # Qué modelos del catálogo entrarían en la placa: es la información que sirve para decidir
    # (sobre todo para el modelo de código, que es el que más se usa).
    entran = [m.nombre for m in catalogo.modelos.values()
              if m.tipo == "ollama" and entra_en_placa(m, hw)]
    de_codigo = catalogo.por_rol.get("codigo")
    datos_gpu = {"vram_gb": round(vram, 2), "utilizable": utilizable, "nombre": nombre_gpu,
                 "modelos_en_placa": entran}
    if utilizable and entran:
        extra = ("incluido el de código" if de_codigo in entran else
                 "pero el de código no entra: conviene una placa con más VRAM")
        requisitos.append(Requisito(
            "gpu", "Placa de video", OK,
            f"{nombre_gpu} · {vram:.1f} GB de VRAM · entran en la placa: "
            f"{', '.join(entran)} ({extra})",
            datos=datos_gpu))
    else:
        detalle = (f"{nombre_gpu} · {vram:.1f} GB de VRAM" if nombre_gpu else "no se detectó")
        requisitos.append(Requisito(
            "gpu", "Placa de video", AVISO,
            f"{detalle}: no entra ningún modelo en la placa; se usa el procesador "
            "(funciona igual, más lento)",
            ("opcional: una placa con VRAM suficiente acelera el chat y sobre todo el modelo de "
             "código, y libera memoria del equipo"),
            datos=datos_gpu))

    # Modo de trabajo: ¿los dos modelos pueden convivir (uno en la placa) o hay que ir de a uno?
    try:
        from src.core.hardware_fit import decidir_modo_de_trabajo
        modo = decidir_modo_de_trabajo(hw, catalogo)
        if modo == "paralelo":
            requisitos.append(Requisito(
                "modo", "Modo de trabajo", OK,
                "EN PARALELO: el modelo más pesado vive en la placa y el otro en memoria, así que "
                "podés cambiar de tarea sin esperar recargas",
                datos={"modo": modo}))
        else:
            requisitos.append(Requisito(
                "modo", "Modo de trabajo", AVISO,
                "de a UN modelo por vez: se descarga el anterior al cambiar de tarea (~6 s medidos)",
                "con una placa cuya memoria alcance para el modelo más pesado, AXIOMA pasa solo a "
                "modo paralelo (los dos cargados: uno en la placa y otro en memoria)",
                datos={"modo": modo}))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[preflight] no se pudo decidir el modo de trabajo: {e}")

    mayor = max((m.ram_gb for m in catalogo.obligatorios()), default=0.0)
    if mayor and ram_gb and ram_gb - mayor < req.ram_margen_gb:
        requisitos.append(Requisito(
            "ram_modelo", "Memoria para el modelo", AVISO,
            f"el modelo más grande ocupa {mayor:.2f} GB y quedarían "
            f"{max(ram_gb - mayor, 0):.2f} GB libres (margen pedido: {req.ram_margen_gb} GB)",
            "cerrá programas pesados antes de conversar"))
    return requisitos


def _probar_disco(catalogo: Any, faltantes: List[Requisito]) -> Requisito:
    """Espacio libre ANTES de descargar: 1,5 × lo que falta bajar."""
    # Sólo se cuenta lo que FALTA de verdad: antes, con todo instalado, el informe decía
    # "se necesitan 14,3 GB para descargar" cuando no había nada que descargar.
    modelos = [m for m in catalogo.modelos.values()
               if any(f.datos.get("modelo") == m.nombre for f in faltantes)]
    necesidad = catalogo.espacio_para_descargar_gb(modelos) if modelos else 0.0
    libre = shutil.disk_usage(Path.home()).free / (1024 ** 3)
    if not modelos:
        return Requisito("disco", "Espacio en disco", OK,
                         f"{libre:.1f} GB libres (no hay nada que descargar)",
                         datos={"libre_gb": round(libre, 2), "necesario_gb": 0.0})
    if libre < necesidad:
        return Requisito("disco", "Espacio en disco", FALTA,
                         f"hacen falta {necesidad:.1f} GB para descargar y hay {libre:.1f} GB libres",
                         "liberá espacio o instalá los modelos en otro disco (variable "
                         "`OLLAMA_MODELS`)",
                         datos={"libre_gb": round(libre, 2), "necesario_gb": necesidad})
    return Requisito("disco", "Espacio en disco", OK,
                     f"{libre:.1f} GB libres (se necesitan {necesidad:.1f} GB para descargar)",
                     datos={"libre_gb": round(libre, 2), "necesario_gb": necesidad})


def _probar_audio(catalogo: Any) -> Requisito:
    """La voz es OPCIONAL: si falta el audio, el chat tiene que seguir andando."""
    try:
        import sounddevice as sd
        dispositivos = len(sd.query_devices())
    except Exception as e:  # noqa: BLE001  (sin dispositivos / sin servidor de sonido)
        return Requisito("audio", "Audio (voz)", AVISO,
                         f"no hay audio utilizable ({type(e).__name__})",
                         "el chat y el texto funcionan igual; la voz queda desactivada")
    if dispositivos == 0:
        return Requisito("audio", "Audio (voz)", AVISO, "no se ven dispositivos de audio",
                         "el chat y el texto funcionan igual; la voz queda desactivada")
    return Requisito("audio", "Audio (voz)", OK, f"{dispositivos} dispositivos de audio")


@dataclass(frozen=True)
class Descarga:
    """Resultado de intentar bajar un modelo."""

    modelo: str
    rol: str
    estado: str          # "instalado" | "fallido" | "cancelado"
    detalle: str = ""
    segundos: float = 0.0


def descargas_pendientes(pre: Preflight, incluir_opcionales: bool = False) -> List[Requisito]:
    """Modelos que faltan y conviene bajar.

    Por defecto **sólo los obligatorios** (el respaldo de código, la visión y la voz son
    opcionales y pueden pesar GB de más); con `incluir_opcionales=True` entran todos.
    """
    pendientes: List[Requisito] = []
    for r in pre.requisitos:
        if not r.datos.get("modelo") or r.estado == OK:
            continue
        if r.estado == FALTA or incluir_opcionales:
            pendientes.append(r)
    return pendientes


def instalar_faltantes(pre: Preflight, cliente: Any, on_progress: Optional[Callable] = None,
                       token: Any = None, incluir_opcionales: bool = False,
                       timeout: float = 3600.0) -> List[Descarga]:
    """Baja los modelos que faltan, uno por uno, informando progreso y aceptando cancelación.

    No inventa órdenes nuevas: usa `cliente.pull_model`, que habla con Ollama. Si una
    descarga falla, se sigue con la siguiente (el resultado dice cuál falló y por qué) — así
    un problema de red no impide bajar el resto.
    """
    import time as _time

    from src.llm.client_base import GeneracionCancelada

    resultados: List[Descarga] = []
    for requisito in descargas_pendientes(pre, incluir_opcionales):
        modelo = str(requisito.datos["modelo"])
        inicio = _time.time()
        try:
            # NOTA (auditoría AUD-FT-001: «timeout fijo», MEDIUM, 2026-10-06): es a propósito y NO
            # depende del hardware. Este plazo es de RED (bajar los GB del modelo), no de cómputo: en
            # una PC potente una descarga lenta seguiría siendo lenta, y en una modesta un enlace rápido
            # sigue siendo rápido. `hardware_fit` no aporta nada acá; el usuario ve el avance real y
            # puede cortar con DETENER, que además cancela de verdad (`token`).
            cliente.pull_model(modelo, timeout=timeout, on_progress=on_progress, token=token)
        except GeneracionCancelada:
            resultados.append(Descarga(modelo, requisito.clave, "cancelado",
                                       "lo pediste vos", round(_time.time() - inicio, 1)))
            logger.info(f"[preflight] descarga de {modelo} cancelada")
            break
        except Exception as e:  # noqa: BLE001  (una falla no debe frenar las demás)
            resultados.append(Descarga(modelo, requisito.clave, "fallido",
                                       f"{type(e).__name__}: {e}"[:200],
                                       round(_time.time() - inicio, 1)))
            logger.warning(f"[preflight] no se pudo bajar {modelo}: {e}")
            continue
        resultados.append(Descarga(modelo, requisito.clave, "instalado", "",
                                   round(_time.time() - inicio, 1)))
    return resultados


# ═══════════════════════════════════════════════════════════════
# DESCARGA EN SEGUNDO PLANO (para la interfaz)
# ═══════════════════════════════════════════════════════════════
# Pedido del usuario: *"se podría agregar una forma de instalar los modelos con un clic y dejarlos
# listos para usar"*. La descarga ya informa progreso y se puede cancelar (`ISSUE-148`); lo que falta
# es poder dispararla sin bloquear la pantalla y ver cómo va.
#
# Regla del proyecto (R7): nada de cargas permanentes — el trabajo corre en un hilo mientras dura la
# descarga, no hay sondeo eterno y el token de cancelación corta de verdad la conexión con Ollama.

class TrabajoDeDescarga:
    """Un trabajo de descarga de los modelos que faltan, con avance y DETENER.

    Se usa desde la interfaz: se crea con el resultado del preflight, se arranca y se consulta
    `estado()` mientras dura (~1 descarga por modelo). `detener()` cancela de verdad.
    """

    def __init__(self, pre: Preflight, cliente: Any, incluir_opcionales: bool = False,
                 identificador: str = "") -> None:
        import threading
        import uuid

        self.identificador = identificador or uuid.uuid4().hex[:12]
        self._pre = pre
        self._cliente = cliente
        self._incluir_opcionales = incluir_opcionales
        self._lock = threading.Lock()
        self._token: Any = None
        self._hilo: Any = None
        self._inicio = 0.0
        self._fin = 0.0
        self._estado = "en espera"        # en espera | descargando | terminado | cancelado | con errores
        self._modelos: List[Dict[str, Any]] = [
            {"modelo": r.datos.get("modelo"), "rol": r.clave, "obligatorio":
             bool(r.datos.get("obligatorio")), "estado": "en espera", "porcentaje": None}
            for r in descargas_pendientes(pre, incluir_opcionales)
        ]

    # ── consulta ────────────────────────────────────────────────────────────
    def estado(self) -> Dict[str, Any]:
        """Foto del avance (segura para leer desde otro hilo/pantalla)."""
        import time as _time

        with self._lock:
            modelos = [dict(m) for m in self._modelos]
            estado = self._estado
            inicio, fin = self._inicio, self._fin
        transcurrido = round((fin or _time.time()) - inicio, 1) if inicio else 0.0
        porcentajes = [m["porcentaje"] for m in modelos if m["porcentaje"] is not None]
        return {
            "identificador": self.identificador,
            "estado": estado,
            "modelos": modelos,
            "total": len(modelos),
            "terminados": sum(1 for m in modelos if m["estado"] in ("listo", "instalado")),
            "avance_global": round(sum(porcentajes) / len(porcentajes), 1) if porcentajes else None,
            "segundos": transcurrido,
            "puede_detenerse": estado in ("en espera", "descargando"),
        }

    def resumen(self) -> str:
        """Texto corto para la pantalla ('descargando 2/3 · 45 %')."""
        e = self.estado()
        if e["estado"] == "en espera":
            return "esperando para empezar"
        if e["estado"] == "cancelado":
            return "cancelado"
        if e["estado"] == "terminado":
            return f"listo: {e['terminados']}/{e['total']} modelos"
        avance = f" · {e['avance_global']} %" if e["avance_global"] is not None else ""
        return f"descargando {e['terminados'] + 1}/{e['total']}{avance}"

    # ── control ─────────────────────────────────────────────────────────────
    def iniciar(self) -> "TrabajoDeDescarga":
        """Arranca la descarga en un hilo aparte (no bloquea la pantalla)."""
        import threading
        import time as _time

        from src.llm.client_base import TokenCancelacion

        if self._hilo is not None:
            return self
        self._token = TokenCancelacion()
        self._inicio = _time.time()
        self._estado = "descargando"
        # NOTA (auditoría AUD-FF-001: «thread fire-and-forget», MEDIUM, 2026-10-06): es a propósito.
        # No es un camino caliente: se crea UN hilo por descarga pedida a mano (no por petición ni por
        # evento), el objeto es de UN solo uso (`iniciar()` devuelve `self` si ya hay hilo) y `detener()`
        # lo corta. El hilo muere solo al terminar la descarga y, por ser `daemon`, no impide cerrar el
        # programa. Un pool no aporta: la descarga la hace el cliente HTTP con su propio avance por cola.
        self._hilo = threading.Thread(target=self._correr, name=f"descarga-{self.identificador}",
                                      daemon=True)
        self._hilo.start()
        return self

    def detener(self) -> int:
        """Corta la descarga (devuelve cuántas conexiones cerró)."""
        if self._token is None:
            return 0
        cerradas = self._token.cancelar()
        logger.info(f"[preflight] descarga {self.identificador} detenida por el usuario")
        return cerradas

    def _correr(self) -> None:
        import time as _time

        from src.llm.client_base import GeneracionCancelada

        if not self._modelos:
            with self._lock:
                self._estado = "terminado"
                self._fin = _time.time()
            return

        def _avisar(evento: Dict[str, Any]) -> None:
            with self._lock:
                for m in self._modelos:
                    if m["modelo"] == evento.get("modelo"):
                        m["estado"] = str(evento.get("estado", ""))
                        m["porcentaje"] = evento.get("porcentaje")

        resultados = instalar_faltantes(self._pre, self._cliente, on_progress=_avisar,
                                        token=self._token,
                                        incluir_opcionales=self._incluir_opcionales)
        cancelado = any(d.estado == "cancelado" for d in resultados)
        fallidos = any(d.estado == "fallido" for d in resultados)
        with self._lock:
            self._estado = "cancelado" if cancelado else ("con errores" if fallidos else "terminado")
            self._fin = _time.time()
            for m in self._modelos:
                if m["estado"] not in ("listo", "instalado") and self._estado == "terminado":
                    m["estado"] = "listo"
                    m["porcentaje"] = 100.0
        del GeneracionCancelada      # se usa sólo para documentar el corte que maneja el instalador


# ═══════════════════════════════════════════════════════════════
# COMPOSICIÓN
# ═══════════════════════════════════════════════════════════════

def revisar(cliente: Optional[Any] = None, catalogo: Optional[Any] = None,
            hw: Optional[Any] = None,
            fabrica_cliente: Optional[Callable[[], Any]] = None) -> Preflight:
    """Corre todas las sondas y devuelve el resultado.

    Args:
        cliente: cliente de Ollama ya construido (si no, se crea uno).
        catalogo: catálogo de modelos (si no, se lee `config/model_catalog.yaml`).
        hw: perfil de hardware (si no, se detecta).
        fabrica_cliente: cómo crear el cliente (para las pruebas).
    """
    from config.model_catalog import cargar_catalogo
    cat = catalogo or cargar_catalogo()

    requisitos: List[Requisito] = [_probar_python()]

    if cliente is None:
        try:
            if fabrica_cliente is not None:
                cliente = fabrica_cliente()
            else:
                from src.llm.client import OllamaClient
                cliente = OllamaClient()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[preflight] no se pudo crear el cliente de modelos: {e}")
            cliente = None

    if cliente is None:
        requisitos.append(Requisito("ollama", "Motor de modelos (Ollama)", FALTA,
                                    "no se pudo importar el cliente de modelos",
                                    "revisá la instalación (`src/llm/client.py`)"))
        instalados: List[str] = []
    else:
        sondas_ollama = _probar_ollama(cliente)
        requisitos.append(sondas_ollama)
        instalados = []
        if sondas_ollama.estado == OK:
            try:
                instalados = [m.get("name", "") for m in (cliente.list_models() or [])]
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[preflight] no se pudo listar los modelos: {e}")

    # El hardware se detecta ANTES de los modelos: hace falta la placa para decir si cada modelo
    # entra en ella (VRAM) o va al procesador.
    if hw is None:
        try:
            from src.core.hardware_fit import detect_hardware
            hw = detect_hardware()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[preflight] no se pudo detectar el hardware: {e}")
            hw = None

    requisitos += _probar_modelos(cliente, cat, instalados, hw)
    if hw is not None:
        requisitos += _probar_hardware(cat, hw)

    requisitos.append(_probar_disco(cat, [r for r in requisitos if r.estado == FALTA]))
    requisitos.append(_probar_audio(cat))
    return Preflight(requisitos=requisitos)


_VEREDICTOS = {
    "perfect": ("✅", "Sí, con comodidad"),
    "good": ("✅", "Sí"),
    "marginal": ("⚠️", "Sí, pero justo"),
    "too_tight": ("⚠️", "Muy justo: puede fallar si tenés otras aplicaciones abiertas"),
    "no_fit": ("❌", "No: no entra en la memoria de tu equipo"),
    "desconocido": ("❔", "No puedo saberlo con este nombre"),
}
_DONDE = {
    "gpu": "en la placa de video (lo más rápido)",
    "cpu_offload": "parte en la placa y parte en el procesador",
    "cpu_only": "en el procesador (más lento, pero anda)",
}


def evaluar_modelo_en_la_pc(nombre: str, hw: Any = None) -> Dict[str, Any]:
    """¿ESTE modelo va a funcionar en MI equipo? Respuesta fácil de leer, sin consola.

    Pedido del usuario: *"solo dejar las herramientas que le permitan al usuario determinar si el modelo
    elegido funcionará en su PC o no, y que pueda usar esas herramientas de una forma fácil"*.

    · Si el modelo **está instalado**, se usa el chequeo del proyecto (`hardware_fit`), que compara su
      tamaño real contra la memoria de la placa y del equipo.
    · Si **no está instalado**, se estima por el tamaño que sugiere el nombre (`:7b` ≈ 4,5 GB en 4 bits) y
      se aclara que es una estimación: sirve para decidir ANTES de descargar varios GB.
    """
    nombre = (nombre or "").strip()
    salida: Dict[str, Any] = {"modelo": nombre, "instalado": False, "nivel": "desconocido"}
    if not nombre:
        salida.update(veredicto="Escribí el nombre del modelo (por ejemplo `qwen3:8b`).")
        return salida

    if hw is None:
        try:
            from src.core.hardware_fit import detect_hardware
            hw = detect_hardware()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[evaluar] no se pudo detectar el hardware: {e}")
            hw = None
    ram_gb = float(getattr(hw, "ram_gb", 0) or 0)
    presupuesto = float(getattr(hw, "effective_budget_gb", 0) or 0)
    en_placa_ok = bool(getattr(hw, "is_gpu_available", False))

    try:
        from src.core.hardware_fit import check_model_fits
        r = check_model_fits(nombre, hw)
        icono, texto = _VEREDICTOS.get(r.fit_level, ("❔", r.fit_level))
        salida.update(instalado=True, nivel=r.fit_level, memoria_gb=round(r.required_gb, 2),
                      donde=_DONDE.get(r.run_mode, r.run_mode),
                      velocidad_tps=round(r.speed_tps, 1) if r.speed_tps else None,
                      veredicto=f"{icono} {texto}",
                      detalle=(f"Necesita unos {r.required_gb:.1f} GB y tu equipo tiene {ram_gb:.1f} GB"
                               + (f" ({r.notes})" if r.notes else "")))
        return salida
    except Exception as e:  # noqa: BLE001 — no está instalado (o falló la consulta): se estima
        logger.debug(f"[evaluar] {nombre} no está instalado o falló el chequeo: {e}")

    # Estimación por el nombre: `:7b` = 7 mil millones de parámetros ≈ 4,5 GB en 4 bits (+ margen)
    import re
    m = re.search(r"[:_-](\d+(?:\.\d+)?)b\b", nombre.lower())
    if not m:
        salida.update(veredicto="❔ No puedo estimarlo con ese nombre",
                      detalle="Probá con el nombre completo, por ejemplo `qwen3:8b` o `llama3:70b`.")
        return salida
    parametros = float(m.group(1))
    necesario = round(parametros * 0.65 + 0.8, 1)          # 4 bits + margen de contexto
    entra_placa = en_placa_ok and necesario <= presupuesto
    entra_equipo = necesario <= ram_gb - 1.5
    if entra_placa:
        nivel, icono, texto = "good", "✅", "Sí, y entra en tu placa de video"
        donde = "en la placa de video (lo más rápido)"
    elif entra_equipo:
        nivel, icono, texto = "marginal", "⚠️", "Sí, pero en el procesador (más lento)"
        donde = "en el procesador"
    else:
        nivel, icono, texto = "no_fit", "❌", "No: no entra en la memoria de tu equipo"
        donde = "—"
    salida.update(nivel=nivel, memoria_gb=necesario, donde=donde,
                  veredicto=f"{icono} {texto}",
                  detalle=(f"Estimado: ~{necesario} GB ({parametros:g} mil millones de parámetros en 4 "
                           f"bits). Tu equipo tiene {ram_gb:.1f} GB de memoria"
                           + (f" y {presupuesto:.1f} GB utilizables de placa" if en_placa_ok else "")),
                  aviso="Es una ESTIMACIÓN (el modelo no está descargado): el tamaño real puede variar.")
    return salida


def informe(pre: Preflight) -> str:
    """Informe legible (lo que ve el usuario)."""
    lineas = ["", "AXIOMA — ¿qué hace falta para funcionar?", "=" * 58]
    for r in pre.requisitos:
        lineas.append(f"{_ICONO.get(r.estado, '?')} {r.titulo}: {r.detalle}")
        if r.remedio and r.estado != OK:
            lineas.append(f"      → {r.remedio}")
    lineas.append("-" * 58)
    lineas.append(pre.veredicto())
    if not pre.puede_responder():
        lineas.append("Volvé a preguntar cuando lo tengas: `python -m src.core.preflight`")
    lineas.append("")
    return "\n".join(lineas)


def main(argv: Optional[List[str]] = None) -> int:
    """Línea de órdenes.

    Opciones:
        --json       salida para la interfaz o un instalador
        --instalar   baja lo que falta (por defecto sólo los modelos obligatorios)
        --todo       con --instalar, incluye también los opcionales (respaldo, visión, voz)

    Devuelve 0 si puede responder y 4 si le falta algo (`ISSUE-146`); con `--instalar`,
    0 sólo si después de bajar todo puede responder.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    pre = revisar()
    if "--json" in args and "--instalar" not in args:
        print(json.dumps(pre.como_dict(), ensure_ascii=False, indent=2))
        return 0 if pre.puede_responder() else 4
    print(informe(pre))

    if "--instalar" in args:
        pendientes = descargas_pendientes(pre, incluir_opcionales="--todo" in args)
        if not pendientes:
            print("No hay nada que descargar.\n")
            return 0 if pre.puede_responder() else 4
        print(f"Descargando {len(pendientes)} modelo(s)...")

        def _avisar(evento: Dict[str, Any]) -> None:
            porcentaje = evento.get("porcentaje")
            avance = f" {porcentaje}%" if porcentaje is not None else ""
            print(f"  · {evento['modelo']}: {evento['estado']}{avance}", flush=True)

        resultados = instalar_faltantes(pre, _cliente_para_instalar(), on_progress=_avisar,
                                        incluir_opcionales="--todo" in args)
        for d in resultados:
            icono = {"instalado": "✅", "fallido": "❌", "cancelado": "⏹️ "}.get(d.estado, "?")
            extra = f" — {d.detalle}" if d.detalle else ""
            print(f"{icono} {d.modelo}: {d.estado} ({d.segundos}s){extra}")
        fallidos = [d for d in resultados if d.estado != "instalado"]
        if fallidos:
            return 4
        pre = revisar()          # se vuelve a mirar con lo que ahora hay
        print(informe(pre))
    return 0 if pre.puede_responder() else 4


def _cliente_para_instalar() -> Any:
    """Cliente de modelos para descargar (se separa para poder probar sin red)."""
    from src.llm.client import OllamaClient
    return OllamaClient()


if __name__ == "__main__":
    sys.exit(main())
