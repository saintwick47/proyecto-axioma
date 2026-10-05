#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.3.6 — Hardware Fit
# Ruta: src/core/hardware_fit.py
# Autor: SaintWick
# Versión: 0.2.1
# Propósito: Detecta hardware del sistema (GPU VRAM, RAM, CPU) y
#            evalúa si los modelos Ollama instalados son compatibles.
#            Permite cambiar el modelo activo editando solo .env —
#            cero modificaciones en otros archivos del proyecto.
# ✅ v0.2.1: FIX AUDITORÍA — force_refresh existía pero nadie lo llamaba.
#   get_hardware() ahora hace un chequeo barato (_gpu_marker_present, sin
#   subprocess) en cada llamada además del TTL de 24h, y se agregó
#   refresh() como entrypoint explícito.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import re
import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

_VRAM_KV_OVERHEAD: float = 0.12
_GPU_SAFE_MARGIN: float = 0.88
_RAM_SAFE_MARGIN: float = 0.72
_IGPU_USABLE_RATIO: float = 0.45
_CACHE_TTL: float = 86_400.0

GpuBackend = Literal["nvidia", "amd", "amd_igpu", "apple", "cpu"]
FitLevel   = Literal["perfect", "good", "marginal", "no_fit", "too_tight"]
RunMode    = Literal["gpu", "cpu_offload", "cpu_only"]


# ═══════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class HardwareProfile:
    gpu_vram_gb: float
    ram_gb: float
    cpu_threads: int
    gpu_name: str
    backend: GpuBackend
    detected_at: float = field(default_factory=time.time)

    @property
    def effective_budget_gb(self) -> float:
        if self.backend in ("nvidia", "amd") and self.gpu_vram_gb > 0:
            return self.gpu_vram_gb * _GPU_SAFE_MARGIN
        if self.backend == "amd_igpu" and self.gpu_vram_gb > 0:
            return self.gpu_vram_gb * _IGPU_USABLE_RATIO
        if self.backend == "apple" and self.gpu_vram_gb > 0:
            return self.gpu_vram_gb * _GPU_SAFE_MARGIN
        return self.ram_gb * _RAM_SAFE_MARGIN

    @property
    def is_gpu_available(self) -> bool:
        return self.backend in ("nvidia", "amd", "apple") and self.gpu_vram_gb > 0


@dataclass(frozen=True)
class ModelFitResult:
    model_name: str
    size_gb: float
    required_gb: float
    fit_level: FitLevel
    run_mode: RunMode
    notes: str
    speed_tps: float = 0.0
    score: float = 0.0


# ═══════════════════════════════════════════════════════════════
# DETECCIÓN DE HARDWARE — PRIVADO
# ═══════════════════════════════════════════════════════════════

def _run(cmd: List[str], timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _detect_nvidia() -> Optional[Tuple[float, str]]:
    out = _run([
        "nvidia-smi",
        "--query-gpu=memory.total,name",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return None
    parts = out.split(",", 1)
    if len(parts) < 2:
        return None
    try:
        return float(parts[0].strip()) / 1024.0, parts[1].strip()
    except ValueError:
        return None


def _detect_amd_rocm() -> Optional[Tuple[float, str]]:
    out = _run(["rocm-smi", "--showmeminfo", "vram", "--csv"])
    if not out:
        return None
    for line in out.splitlines():
        if "Total" not in line:
            continue
        for part in line.split(","):
            stripped = part.strip()
            if stripped.isdigit():
                mb = int(stripped)
                if mb > 512:
                    return mb / 1024.0, "AMD Radeon (ROCm)"
    return None


def _detect_amd_sysfs() -> Optional[Tuple[float, str, GpuBackend]]:
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return None
    best_vram = 0.0
    gpu_name = "AMD GPU"
    for vram_path in sorted(drm.glob("card*/device/mem_info_vram_total")):
        try:
            vram_gb = int(vram_path.read_text().strip()) / (1024 ** 3)
            if vram_gb > best_vram:
                best_vram = vram_gb
                name_path = vram_path.parent / "product_name"
                if name_path.exists():
                    gpu_name = name_path.read_text().strip()
        except (ValueError, OSError):
            continue
    if best_vram <= 0.0:
        return None
    backend: GpuBackend = "amd_igpu" if best_vram < 4.0 else "amd"
    return best_vram, gpu_name, backend


def _detect_apple() -> Optional[Tuple[float, str]]:
    out = _run(["sysctl", "-n", "hw.memsize"])
    if not out:
        return None
    try:
        return int(out) / (1024 ** 3), "Apple Silicon (Unified Memory)"
    except ValueError:
        return None


def _detect_ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except ImportError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 hardware_fit.py:166] excepción degradada (intencional): {_d2e}")
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1]) / (1024 ** 2)
                    except ValueError as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 hardware_fit.py:176] excepción degradada (intencional): {_d2e}")
    return 8.0


def _detect_cpu_threads() -> int:
    try:
        import psutil
        return psutil.cpu_count(logical=True) or os.cpu_count() or 4
    except ImportError:
        return os.cpu_count() or 4


# ═══════════════════════════════════════════════════════════════
# DETECCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════

def detect_hardware() -> HardwareProfile:
    """
    Detecta hardware del sistema. Orden de prioridad:
    NVIDIA → AMD ROCm → AMD sysfs (incluye iGPU) → Apple Silicon → CPU-only.

    Para Ryzen 5 8500G (Radeon 740M iGPU):
      - Sin ROCm: reporta como cpu (Ollama usa RAM completa como presupuesto)
      - Con ROCm: detectado como amd_igpu; budget = vram_compartida * 0.45
    """
    ram_gb = _detect_ram_gb()
    cpu_threads = _detect_cpu_threads()

    nvidia = _detect_nvidia()
    if nvidia:
        vram_gb, name = nvidia
        return HardwareProfile(gpu_vram_gb=vram_gb, ram_gb=ram_gb,
                               cpu_threads=cpu_threads, gpu_name=name, backend="nvidia")

    rocm = _detect_amd_rocm()
    if rocm:
        vram_gb, name = rocm
        return HardwareProfile(gpu_vram_gb=vram_gb, ram_gb=ram_gb,
                               cpu_threads=cpu_threads, gpu_name=name, backend="amd")

    sysfs = _detect_amd_sysfs()
    if sysfs:
        vram_gb, name, backend = sysfs
        return HardwareProfile(gpu_vram_gb=vram_gb, ram_gb=ram_gb,
                               cpu_threads=cpu_threads, gpu_name=name, backend=backend)

    apple = _detect_apple()
    if apple:
        vram_gb, name = apple
        return HardwareProfile(gpu_vram_gb=vram_gb, ram_gb=ram_gb,
                               cpu_threads=cpu_threads, gpu_name=name, backend="apple")

    return HardwareProfile(gpu_vram_gb=0.0, ram_gb=ram_gb, cpu_threads=cpu_threads,
                           gpu_name="", backend="cpu")



# ═══════════════════════════════════════════════════════════════
# SCORING: BANDWIDTH + SPEED + QUALITY
# ═══════════════════════════════════════════════════════════════

# GB/s por familia GPU — keys ordenadas por longitud descendente para match correcto
_GPU_BANDWIDTH: Dict[str, float] = {
    # RTX 50xx
    "5090": 1792.0, "5080": 960.0, "5070 ti": 896.0, "5070": 672.0,
    "5060 ti": 448.0, "5060": 256.0,
    # RTX 40xx
    "4090": 1008.0, "4080 super": 736.0, "4080": 717.0,
    "4070 ti super": 672.0, "4070 ti": 504.0, "4070 super": 504.0,
    "4070": 504.0, "4060 ti": 288.0, "4060": 272.0,
    # RTX 30xx
    "3090 ti": 1008.0, "3090": 936.0, "3080 ti": 912.0, "3080": 760.0,
    "3070 ti": 608.0, "3070": 448.0, "3060 ti": 448.0, "3060": 360.0,
    # RTX 20xx
    "2080 ti": 616.0, "2080 super": 496.0, "2080": 448.0,
    "2070 super": 448.0, "2070": 448.0, "2060 super": 448.0, "2060": 336.0,
    # AMD RDNA4
    "9070 xt": 624.0, "9070": 488.0, "9060 xt": 322.0, "9060": 322.0,
    # AMD RDNA3
    "7900 xtx": 960.0, "7900 xt": 800.0, "7900 gre": 576.0,
    "7800 xt": 624.0, "7700 xt": 432.0, "7600": 288.0,
    # AMD RDNA2
    "6950 xt": 576.0, "6900 xt": 512.0, "6800 xt": 512.0,
    "6800": 512.0, "6700 xt": 384.0, "6600 xt": 256.0, "6600": 224.0,
    # AMD iGPU (LPDDR5 shared — Ryzen 8000/9000 series)
    "radeon 890m": 76.8, "radeon 880m": 76.8,
    "radeon 780m": 51.2, "radeon 760m": 51.2,
    "radeon 740m": 51.2, "radeon 720m": 38.4,
    # Apple Silicon (unified memory)
    "m4 pro": 273.0, "m4 max": 410.0, "m4": 120.0,
    "m3 pro": 150.0, "m3 max": 300.0, "m3": 100.0,
    "m2 pro": 200.0, "m2 max": 400.0, "m2 ultra": 800.0, "m2": 100.0,
    "m1 pro": 200.0, "m1 max": 400.0, "m1 ultra": 800.0, "m1": 68.0,
}
_BW_KEYS_SORTED: List[str] = sorted(_GPU_BANDWIDTH.keys(), key=len, reverse=True)

# Fallback por backend (GB/s) — CPU DDR4/DDR5 dual-channel
_CPU_BW: float = 55.0
_IGPU_BW: float = 51.2   # LPDDR5 Ryzen 8500G — overridden por tabla si hay match

# Bytes por parámetro en Q4_K_M (aproximación)
_Q4_BPP: float = 0.5
# Eficiencia de memoria bandwidth (lectura secuencial de pesos)
_BW_EFFICIENCY: float = 0.55
# Target tok/s para scoring (uso interactivo)
_SPEED_TARGET: float = 30.0


def _lookup_bandwidth(gpu_name: str) -> Optional[float]:
    """Retorna bandwidth (GB/s) para un gpu_name, o None si no hay match."""
    if not gpu_name:
        return None
    gn = gpu_name.lower()
    for key in _BW_KEYS_SORTED:
        if key in gn:
            return _GPU_BANDWIDTH[key]
    return None


def _estimate_speed(
    size_gb: float,
    run_mode: RunMode,
    gpu_name: str,
    backend: GpuBackend,
) -> float:
    """
    Estima tok/s basado en bandwidth de memoria y tamaño del modelo.
    Modelo: tps ≈ (bw / model_gb) * efficiency
    Para cpu_offload usa blend harmónico GPU/CPU.
    """
    if size_gb <= 0.0:
        return 0.0
    # ✅ FIX: size_gb YA es el tamaño real en disco de un modelo Q4 —
    # multiplicar por _Q4_BPP acá descontaba la cuantización dos veces
    # (subestimaba el footprint real de memoria/bandwidth a la mitad).
    model_gb = size_gb

    if run_mode == "gpu":
        bw = _lookup_bandwidth(gpu_name) or _IGPU_BW
        return (bw / model_gb) * _BW_EFFICIENCY if model_gb > 0 else 0.0

    if run_mode == "cpu_offload":
        bw = _lookup_bandwidth(gpu_name) or _IGPU_BW
        # 50% offload aproximado — blend harmónico
        eff_bw = 1.0 / (0.5 / _CPU_BW + 0.5 / bw)
        return (eff_bw / model_gb) * _BW_EFFICIENCY

    # cpu_only
    return (_CPU_BW / model_gb) * _BW_EFFICIENCY


def _architecture_bonus(model_name: str, known_architecture: Optional[str] = None) -> int:
    """Bonus por recencia de arquitectura (0-9).

    Por default matchea contra el NOMBRE del modelo, pero un nombre custom
    (ej. "axioma-code") no delata su arquitectura real aunque sea un
    fine-tune/rename de una conocida. Si el caller ya sabe la arquitectura
    real (ej. vía `ollama show`), puede pasarla en known_architecture y
    ese valor tiene prioridad sobre el nombre.
    """
    n = (known_architecture or model_name).lower()
    if re.search(r"qwen3[\.\-_]?[5-9]", n):
        return 9
    if "qwen3" in n:
        return 6
    if "qwen2.5" in n or "qwen2_5" in n:
        return 3
    if re.search(r"llama[-_]?3\.?[1-9]", n):
        return 3
    if re.search(r"llama[-_]?3", n):
        return 2
    if "gemma3" in n or "gemma-3" in n:
        return 3
    if "deepseek" in n:
        return 4
    if "mistral" in n or "mixtral" in n:
        return 2
    return 0


def _quality_score(model_name: str, size_gb: float, known_architecture: Optional[str] = None) -> float:
    """
    Calidad estimada 0-100 basada en tamaño del modelo y arquitectura.
    Usa size_gb como proxy de params, con densidad real de Q4_K_M.
    """
    # ✅ FIX: dividía por 2.0 (densidad FP16, ~2GB/B) pese a que el comentario
    # decía "Q4" — todo el stack de AXIOMA es Q4 (~0.5GB/B, ver _Q4_BPP),
    # eso subestimaba params (y por ende calidad) de modelos chicos ~4x.
    params_b = size_gb / _Q4_BPP  # densidad real Q4_K_M
    if params_b < 1.5:
        base = 35.0
    elif params_b < 3.5:
        base = 50.0
    elif params_b < 8.0:
        base = 65.0
    elif params_b < 14.0:
        base = 75.0
    elif params_b < 30.0:
        base = 83.0
    elif params_b < 60.0:
        base = 89.0
    else:
        base = 94.0
    return min(100.0, base + _architecture_bonus(model_name, known_architecture))


def _speed_score(tps: float) -> float:
    """0-100 relativo al target interactivo (_SPEED_TARGET tok/s)."""
    return min(100.0, (tps / _SPEED_TARGET) * 100.0) if _SPEED_TARGET > 0 else 0.0


def _fit_score(required: float, budget: float) -> float:
    """0-100 basado en headroom de memoria."""
    if required > budget or budget <= 0:
        return 0.0
    ratio = required / budget
    if ratio <= 0.5:
        return 60.0 + (ratio / 0.5) * 40.0
    if ratio <= 0.80:
        return 100.0
    if ratio <= 0.90:
        return 70.0
    return 50.0


def _composite(quality: float, speed: float, fit: float) -> float:
    """Composite score: quality×0.50 + speed×0.35 + fit×0.15"""
    return round(quality * 0.50 + speed * 0.35 + fit * 0.15, 2)


# ═══════════════════════════════════════════════════════════════
# EVALUACIÓN DE FIT
# ═══════════════════════════════════════════════════════════════

def evaluate_model_fit(
    model_name: str,
    size_gb: float,
    hw: HardwareProfile,
    known_architecture: Optional[str] = None,
) -> ModelFitResult:
    """
    Evalúa si un modelo cabe en el hardware disponible.

    Args:
        known_architecture: arquitectura real si se conoce (ej. "deepseek2"
            vía `ollama show`). Opcional — sin esto, el bonus de arquitectura
            se estima por el NOMBRE del modelo, que puede no delatarla
            (nombres custom/renombrados).

    Umbrales:
        perfect  : required ≤ budget * 0.80
        good     : required ≤ budget
        marginal : required ≤ budget * 1.15  (offload parcial posible)
                   O required ≤ ram_gb * 0.90 (solo CPU RAM)
        no_fit   : no cabe en ningún modo razonable
    """
    required_gb = size_gb * (1.0 + _VRAM_KV_OVERHEAD)
    budget = hw.effective_budget_gb

    if required_gb <= budget * 0.80:
        fit: FitLevel = "perfect"
        mode: RunMode = "gpu" if hw.is_gpu_available else "cpu_only"
        notes = f"Cabe con margen ({required_gb:.1f}/{budget:.1f} GB)"
    elif required_gb <= budget:
        fit = "good"
        mode = "gpu" if hw.is_gpu_available else "cpu_only"
        notes = f"Cabe ajustado ({required_gb:.1f}/{budget:.1f} GB)"
    elif required_gb <= budget * 1.15:
        fit = "marginal"
        mode = "cpu_offload" if hw.is_gpu_available else "cpu_only"
        notes = f"Offload parcial posible ({required_gb:.1f}/{budget:.1f} GB)"
    elif required_gb <= hw.ram_gb * 0.90:
        fit = "marginal"
        mode = "cpu_only"
        notes = f"Solo CPU RAM ({required_gb:.1f}/{hw.ram_gb:.1f} GB) — velocidad reducida"
    else:
        fit = "no_fit"
        mode = "cpu_only"
        notes = f"No cabe: requiere {required_gb:.1f} GB, disponible {budget:.1f} GB"

    tps = _estimate_speed(size_gb, mode, hw.gpu_name, hw.backend)
    q   = _quality_score(model_name, size_gb, known_architecture)
    s   = _speed_score(tps)
    f_  = _fit_score(required_gb, hw.effective_budget_gb)
    composite = _composite(q, s, f_)

    return ModelFitResult(
        model_name=model_name,
        size_gb=size_gb,
        required_gb=required_gb,
        fit_level=fit,
        run_mode=mode,
        notes=notes,
        speed_tps=round(tps, 1),
        score=composite,
    )


# ═══════════════════════════════════════════════════════════════
# INTEGRACIÓN OLLAMA
# ═══════════════════════════════════════════════════════════════

def _fetch_ollama_models() -> List[Dict]:
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{settings.ollama_host}/api/tags")
            resp.raise_for_status()
            return resp.json().get("models", [])
    except Exception as exc:
        logger.warning(f"[HardwareFit] Ollama no disponible: {exc}")
        return []


def rank_ollama_models(hw: Optional[HardwareProfile] = None) -> List[ModelFitResult]:
    """
    Evalúa TODOS los modelos Ollama instalados y los ordena:
    perfect → good → marginal → no_fit.
    """
    if hw is None:
        hw = detect_hardware()
    results = [
        evaluate_model_fit(m["name"], m.get("size", 0) / (1024 ** 3), hw)
        for m in _fetch_ollama_models()
        if m.get("name")
    ]
    # Ordenar por composite score desc; dentro de no_fit, por required_gb asc
    results.sort(key=lambda r: (0 if r.fit_level != "no_fit" else 1, -r.score, r.required_gb))
    return results


def decidir_modo_de_trabajo(hw: Optional[HardwareProfile] = None,
                            catalogo: Optional[Any] = None,
                            margen_gb: float = 1.5) -> str:
    """¿Se puede trabajar EN PARALELO (los dos modelos a la vez) o hay que ir de a uno?

    ✅ Pedido del usuario (2026-10-05): *"si la pc cuenta con gpu, pueda usarla y alojar el modelo más
    pesado allí y trabajar de forma paralela en lugar de forma simple (un modelo a la vez)"*.

    Devuelve `"paralelo"` cuando se cumplen las dos cosas:
      · hay placa utilizable y **el modelo más pesado entra en su VRAM** (con el margen del proyecto,
        `effective_budget_gb`), y
      · el otro modelo obligatorio **entra en la memoria del equipo** dejando el margen.

    Si no, `"simple"` (un modelo a la vez: es lo que pasa hoy en este equipo, donde la placa integrada
    no alcanza y la memoria no permite los dos juntos — medido en `ISSUE-141`: 12,47 GB de modelos
    contra 11,05 GB disponibles).
    """
    if hw is None:
        hw = detect_hardware()
    if catalogo is None:
        from config.model_catalog import cargar_catalogo
        catalogo = cargar_catalogo()
    if not hw.is_gpu_available:
        return "simple"
    pesos = sorted((m.ram_gb for m in catalogo.obligatorios()), reverse=True)
    if not pesos:
        return "simple"
    mas_pesado = pesos[0]
    otros = pesos[1]
    entra_en_placa = mas_pesado <= hw.effective_budget_gb
    el_otro_entra_en_memoria = (otros + margen_gb + 0.2) <= hw.ram_gb
    return "paralelo" if (entra_en_placa and el_otro_entra_en_memoria) else "simple"


def check_model_fits(model_name: str, hw: Optional[HardwareProfile] = None) -> ModelFitResult:
    """
    Verifica compatibilidad de un modelo específico instalado en Ollama.
    Raise ValueError si el modelo no está instalado.
    """
    if hw is None:
        hw = detect_hardware()
    for m in _fetch_ollama_models():
        if m.get("name") == model_name:
            return evaluate_model_fit(model_name, m.get("size", 0) / (1024 ** 3), hw)
    raise ValueError(f"Modelo '{model_name}' no encontrado en Ollama")


# ═══════════════════════════════════════════════════════════════
# SINGLETON CON CACHÉ (24 h)
# ═══════════════════════════════════════════════════════════════

class HardwareFitManager:
    """
    Singleton con caché de 24 h del HardwareProfile.
    Expone la API de evaluación sin re-detectar hardware en cada llamada.

    Uso:
        hw_mgr = HardwareFitManager.instance()
        hw_mgr.rank_models()                       # todos los modelos rankeados
        hw_mgr.check_model("qwen3:8b")           # un modelo específico
        hw_mgr.is_current_config_compatible()      # valida settings actuales
    """

    _instance: Optional["HardwareFitManager"] = None
    _hw_cache: Optional[HardwareProfile] = None
    _cache_ts: float = 0.0

    @classmethod
    def instance(cls) -> "HardwareFitManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_hardware(self, force_refresh: bool = False) -> HardwareProfile:
        """
        ✅ FIX AUDITORÍA: antes force_refresh existía pero ningún archivo
        lo llamaba nunca con True — la caché de 24h nunca se invalidaba
        ante un cambio real de hardware (ej. GPU externa desconectada).
        Ahora, además del TTL, cada llamada hace un chequeo barato (sin
        subprocess) de que el backend cacheado sigue siendo plausible —
        si no, fuerza una re-detección real sin esperar el TTL.
        """
        now = time.time()
        stale = (
            force_refresh
            or self._hw_cache is None
            or (now - self._cache_ts) > _CACHE_TTL
            or (self._hw_cache is not None and not self._gpu_marker_present(self._hw_cache))
        )
        if stale:
            self._hw_cache = detect_hardware()
            self._cache_ts = now
            logger.info(
                f"[HardwareFit] backend={self._hw_cache.backend} "
                f"vram={self._hw_cache.gpu_vram_gb:.1f}GB "
                f"ram={self._hw_cache.ram_gb:.1f}GB "
                f"budget={self._hw_cache.effective_budget_gb:.1f}GB "
                f"threads={self._hw_cache.cpu_threads}"
            )
        return self._hw_cache

    @staticmethod
    def _gpu_marker_present(hw: HardwareProfile) -> bool:
        """
        ✅ FIX AUDITORÍA: chequeo barato (shutil.which / os.path.exists, sin
        spawnear subprocess) de que el backend cacheado sigue siendo
        plausible. No reemplaza la detección real (detect_hardware()) —
        solo decide si vale la pena forzarla antes de que venza el TTL de
        24h. Falsos negativos posibles (ej. nvidia-smi removido pero GPU
        sigue ahí) solo causan una re-detección de más, no un problema.
        """
        if hw.backend == "nvidia":
            return shutil.which("nvidia-smi") is not None
        if hw.backend == "amd":
            return shutil.which("rocm-smi") is not None or Path("/sys/class/drm").exists()
        if hw.backend == "amd_igpu":
            return Path("/sys/class/drm").exists()
        return True  # apple / cpu: sin marcador barato confiable que chequear

    def refresh(self) -> HardwareProfile:
        """
        ✅ FIX AUDITORÍA: entrypoint explícito para forzar re-detección de
        hardware — antes force_refresh=True existía en get_hardware() pero
        no había ningún punto de entrada obvio para invocarlo (ej. desde un
        comando manual/admin cuando se sabe que el hardware cambió).
        """
        return self.get_hardware(force_refresh=True)

    def rank_models(self) -> List[ModelFitResult]:
        return rank_ollama_models(self.get_hardware())

    def check_model(self, model_name: str) -> ModelFitResult:
        return check_model_fits(model_name, self.get_hardware())

    def is_current_config_compatible(self) -> Dict[str, ModelFitResult]:
        """
        Verifica si los modelos configurados en settings caben en el hardware.
        Retorna solo los que están instalados en Ollama.

        Ejemplo de uso en startup o antes de un swap:
            hw_mgr = HardwareFitManager.instance()
            report = hw_mgr.is_current_config_compatible()
            for key, result in report.items():
                if result.fit_level == "no_fit":
                    logger.warning(f"{key}: {result.notes}")
        """
        hw = self.get_hardware()
        installed = {m.get("name", "") for m in _fetch_ollama_models()}
        targets: Dict[str, str] = {
            "llm_default_model": settings.llm_default_model,
            "llm_code_model":    settings.llm_code_model,
            "llm_vision_model":  settings.llm_vision_model,
        }
        if settings.llm_fallback_1:
            targets["llm_fallback_1"] = settings.llm_fallback_1
        return {
            key: check_model_fits(name, hw)
            for key, name in targets.items()
            if name and name in installed
        }
