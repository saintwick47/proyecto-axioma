#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Ruta: /ruta/a/axioma/tests/test_audio_diagnostic.py
# Autor: SaintWick
"""
DIAGNÓSTICO DE AUDIO PARA AXIOMA v2
Ejecuta pruebas completas del pipeline de voz y genera un informe.
Uso: python test_audio_diagnostic.py
"""

import os
import sys
import time
import json
import asyncio
import subprocess
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

# Intentar importar módulos de AXIOMA
try:
    _PROJECT_ROOT = Path(__file__).resolve().parent
    if not (_PROJECT_ROOT / "tools").exists():
        _PROJECT_ROOT = _PROJECT_ROOT.parent
    sys.path.insert(0, str(_PROJECT_ROOT))
    from src.multimodal.stt_engine import STTEngine, STTConfig
    from src.multimodal.vad_filter import VADFilter, VADConfig, AudioChunk
    from src.multimodal.tts_engine import TTSEngine
    from src.core.dispatcher import Dispatcher
    from src.multimodal.voice_pipeline import VoicePipeline
    from config.settings import get_settings
    AXIOMA_AVAILABLE = True
except ImportError as e:
    AXIOMA_AVAILABLE = False
    AXIOMA_IMPORT_ERROR = str(e)

# Intentar importar dependencias de audio
try:
    import sounddevice as sd
    import numpy as np
    import soundfile as sf
    AUDIO_DEPS = True
except ImportError as e:
    AUDIO_DEPS = False
    AUDIO_DEPS_ERROR = str(e)

# Intentar importar librosa para resampleo
try:
    import librosa
    HAS_LIBROSA = True
except ImportError:
    HAS_LIBROSA = False

class AudioDiagnostic:
    """Diagnóstico completo del pipeline de audio de AXIOMA."""

    def __init__(self):
        self.results: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "system": {},
            "hardware": {},
            "config": {},
            "vad": {},
            "stt": {},
            "pipeline": {},
            "recommendations": []
        }
        self.temp_dir = tempfile.mkdtemp(prefix="axioma_audio_diag_")
        self.test_audio_file = None
        # Ruta para guardar el informe final dentro del proyecto
        self.report_dir = Path(__file__).resolve().parent.parent / "reports"
        self.report_dir.mkdir(exist_ok=True)
        self.report_file = self.report_dir / "audio_diagnostic_report.json"

    def run_all(self) -> Dict[str, Any]:
        """Ejecuta todas las pruebas y genera el informe."""
        self._check_system()
        self._check_hardware()
        self._check_config()
        self._test_vad()
        self._test_stt()
        # La prueba del pipeline es asíncrona
        asyncio.run(self._test_pipeline_async())
        self._generate_recommendations()
        return self.results

    def _check_system(self):
        sys_info = {
            "platform": sys.platform,
            "python_version": sys.version,
            "audiodevices_installed": AUDIO_DEPS,
            "axioma_available": AXIOMA_AVAILABLE,
        }
        if not AUDIO_DEPS:
            sys_info["audio_error"] = AUDIO_DEPS_ERROR
        if not AXIOMA_AVAILABLE:
            sys_info["axioma_error"] = AXIOMA_IMPORT_ERROR
        self.results["system"] = sys_info

    def _check_hardware(self):
        hw = {}
        try:
            if AUDIO_DEPS:
                devices = sd.query_devices()
                input_devices = [d for d in devices if d['max_input_channels'] > 0]
                hw["input_devices"] = [
                    {"name": d['name'], "index": i, "channels": d['max_input_channels']}
                    for i, d in enumerate(devices) if d['max_input_channels'] > 0
                ]
                hw["default_input"] = sd.default.device[0]
                if sd.default.device[0] is not None:
                    hw["default_input_name"] = devices[sd.default.device[0]]['name']
                else:
                    hw["default_input_name"] = None
            else:
                hw["error"] = "sounddevice no disponible"

            if sys.platform.startswith('linux'):
                try:
                    result = subprocess.run(
                        ["arecord", "-l"],
                        capture_output=True, text=True, timeout=5
                    )
                    hw["arecord_output"] = result.stdout
                    hw["arecord_error"] = result.stderr if result.stderr else None
                except Exception as e:
                    hw["arecord_error"] = str(e)

            if AUDIO_DEPS:
                try:
                    duration = 3
                    fs = 16000
                    # ✅ plan_rafael (2026-08-31): mostrar frase a leer +
                    # cuenta regresiva para que la grabación capture voz real.
                    _phrase = "¿Cuál es el clima de hoy en Buenos Aires?"
                    print()
                    print("🎤 Leé esta frase en voz alta cuando arranque la grabación:")
                    print(f"   «{_phrase}»")
                    for i in (3, 2, 1):
                        print(f"   ⏳ Grabando en {i}...", flush=True)
                        time.sleep(1)
                    print("   🔴 ¡AHORA! (grabando 3s)...", flush=True)
                    recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='float32')
                    sd.wait()
                    print("   ✅ Grabación lista")
                    rms = np.sqrt(np.mean(recording**2))
                    hw["test_recording_rms"] = float(rms)
                    hw["test_recording_peak"] = float(np.max(np.abs(recording)))
                    hw["test_recording_valid"] = rms > 0.001
                    test_file = os.path.join(self.temp_dir, "test_recording.wav")
                    sf.write(test_file, recording, fs)
                    self.test_audio_file = test_file
                except Exception as e:
                    hw["test_recording_error"] = str(e)
        except Exception as e:
            hw["error"] = str(e)
        self.results["hardware"] = hw

    def _check_config(self):
        cfg = {}
        if AXIOMA_AVAILABLE:
            try:
                from config.settings import get_settings
                settings = get_settings()
                import yaml
                multimodal_path = Path("config/multimodal.yaml")
                if multimodal_path.exists():
                    with open(multimodal_path, 'r') as f:
                        multimodal_cfg = yaml.safe_load(f)
                        cfg["multimodal_yaml"] = multimodal_cfg
                else:
                    cfg["multimodal_yaml"] = None

                # ✅ plan_rafael (2026-08-31): mostrar la config REAL del
                # pipeline (loader multimodal.yaml). Antes leía settings
                # inexistentes (stt_confidence_threshold → mostraba 0.6 viejo).
                from config.multimodal_loader import build_stt_config, build_vad_config
                _stt_cfg = build_stt_config()
                _vad_cfg = build_vad_config()
                cfg["stt_model"] = _stt_cfg.model_size
                cfg["stt_beam_size"] = _stt_cfg.beam_size
                cfg["vad_aggressiveness"] = _vad_cfg.aggressiveness
                cfg["vad_sample_rate"] = _vad_cfg.sample_rate
                cfg["stt_confidence_threshold"] = _stt_cfg.confidence_threshold
                cfg["vad_backend"] = _vad_cfg.backend
                cfg["stt_no_speech_threshold"] = _stt_cfg.no_speech_threshold
            except Exception as e:
                cfg["error"] = str(e)
        else:
            cfg["error"] = "AXIOMA no disponible"
        self.results["config"] = cfg

    def _test_vad(self):
        vad_result = {}
        if not AXIOMA_AVAILABLE:
            vad_result["error"] = "AXIOMA no disponible"
            self.results["vad"] = vad_result
            return

        try:
            config = VADConfig()
            vad = VADFilter(config)
            vad_result["available"] = True

            if self.test_audio_file and os.path.exists(self.test_audio_file):
                audio, sr = sf.read(self.test_audio_file)
                if sr != 16000 and HAS_LIBROSA:
                    audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
                elif sr != 16000:
                    vad_result["resample_warning"] = "No se pudo resamplear, puede afectar la detección"

                audio_int16 = (audio * 32767).astype(np.int16)
                frame_len = int(16000 * 0.03)
                frames = [audio_int16[i:i+frame_len].tobytes()
                         for i in range(0, len(audio_int16), frame_len)]

                speech_frames = 0
                total_frames = len(frames)
                for f in frames:
                    if len(f) == frame_len * 2:
                        if vad.is_speech(f):
                            speech_frames += 1
                vad_result["speech_ratio"] = speech_frames / total_frames if total_frames else 0
                vad_result["total_frames"] = total_frames
                vad_result["speech_frames"] = speech_frames
            else:
                # Prueba en tiempo real breve
                if AUDIO_DEPS:
                    try:
                        duration = 3
                        fs = 16000
                        recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='float32')
                        sd.wait()
                        audio_int16 = (recording * 32767).astype(np.int16).flatten()
                        frame_len = int(fs * 0.03)
                        frames = [audio_int16[i:i+frame_len].tobytes()
                                 for i in range(0, len(audio_int16), frame_len)]
                        speech_frames = 0
                        total_frames = len(frames)
                        for f in frames:
                            if len(f) == frame_len * 2:
                                if vad.is_speech(f):
                                    speech_frames += 1
                        vad_result["realtime_ratio"] = speech_frames / total_frames if total_frames else 0
                    except Exception as e:
                        vad_result["realtime_error"] = str(e)
        except Exception as e:
            vad_result["error"] = str(e)
        self.results["vad"] = vad_result

    def _test_stt(self):
        stt_result = {}
        if not AXIOMA_AVAILABLE:
            stt_result["error"] = "AXIOMA no disponible"
            self.results["stt"] = stt_result
            return

        if not self.test_audio_file or not os.path.exists(self.test_audio_file):
            stt_result["error"] = "No hay archivo de prueba"
            self.results["stt"] = stt_result
            return

        try:
            config = STTConfig()
            engine = STTEngine(config)
            audio, sr = sf.read(self.test_audio_file)
            if sr != 16000 and HAS_LIBROSA:
                audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
            start = time.time()
            result = engine.transcribe(audio)
            elapsed = time.time() - start
            stt_result["transcription"] = result.text
            stt_result["confidence"] = result.confidence
            stt_result["language"] = result.language
            stt_result["duration_seconds"] = result.duration_seconds
            stt_result["latency_ms"] = result.latency_ms
            stt_result["elapsed"] = elapsed
            stt_result["success"] = bool(result.text.strip())
            if not stt_result["success"]:
                stt_result["warning"] = "La transcripción está vacía o tiene confianza baja"
        except Exception as e:
            stt_result["error"] = str(e)
        self.results["stt"] = stt_result

    async def _test_pipeline_async(self):
        """Prueba asíncrona del pipeline."""
        pl_result = {}
        if not AXIOMA_AVAILABLE:
            pl_result["error"] = "AXIOMA no disponible"
            self.results["pipeline"] = pl_result
            return

        if not self.test_audio_file:
            pl_result["error"] = "No hay archivo de prueba"
            self.results["pipeline"] = pl_result
            return

        try:
            dispatcher = Dispatcher()
            tts = TTSEngine()
            stt = STTEngine()
            vad = VADFilter(VADConfig())
            pipeline = VoicePipeline(dispatcher, tts, stt, vad)

            audio, sr = sf.read(self.test_audio_file)
            if sr != 16000 and HAS_LIBROSA:
                audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)

            # ✅ plan_rafael (2026-08-31): el pipeline espera un AudioChunk
            # (tiene .to_float32()), no un ndarray crudo — antes fallaba con
            # "'numpy.ndarray' object has no attribute 'to_float32'". El
            # voice_ratio se calcula con el VAD real frame a frame.
            audio = np.asarray(audio, dtype=np.float32)
            audio16 = np.clip(audio, -1.0, 1.0)
            audio16 = (audio16 * 32767).astype(np.int16)
            frame_bytes = vad._config.frame_ms * vad._config.sample_rate // 1000 * 2
            frames = [
                audio16[i:i + frame_bytes // 2].tobytes()
                for i in range(0, len(audio16) - frame_bytes // 2, frame_bytes // 2)
            ]
            voice_count = sum(1 for f in frames if vad.is_speech(f)) if frames else 0
            chunk = AudioChunk(
                audio=audio16,
                duration_seconds=len(audio16) / 16000,
                voice_ratio=voice_count / len(frames) if frames else 0.0,
            )

            start = time.time()
            result = await pipeline._process_utterance_from_chunk(chunk)
            elapsed = time.time() - start

            pl_result["simulated"] = True
            pl_result["elapsed"] = elapsed
            pl_result["result"] = str(result) if result else "None"
            if result and hasattr(result, 'success'):
                pl_result["success"] = result.success
                pl_result["reason"] = getattr(result, 'reason', None)
            else:
                pl_result["success"] = False
                pl_result["reason"] = "No se obtuvo resultado"
        except Exception as e:
            pl_result["error"] = str(e)
        self.results["pipeline"] = pl_result

    def _generate_recommendations(self):
        recs = []
        hw = self.results.get("hardware", {})
        vad = self.results.get("vad", {})
        stt = self.results.get("stt", {})
        config = self.results.get("config", {})
        pl = self.results.get("pipeline", {})

        # Hardware
        if hw.get("test_recording_valid") is False:
            recs.append("❌ La grabación de prueba falló. Verifica que el micrófono esté conectado y tenga permisos.")
        elif hw.get("test_recording_rms", 0) < 0.001:
            recs.append("⚠️ El nivel de audio grabado es muy bajo. Aumenta la ganancia del micrófono en el sistema.")

        if not hw.get("input_devices"):
            recs.append("❌ No se detectaron dispositivos de entrada de audio. Revisa los drivers y conexiones.")

        # Configuración
        if config.get("multimodal_yaml"):
            vad_agg = config.get("vad_aggressiveness", 2)
            if vad_agg < 2:
                recs.append(f"ℹ️ La agresividad del VAD es {vad_agg}. Puedes aumentarla a 3 para mejor detección en entornos ruidosos.")
            elif vad_agg > 3:
                recs.append("⚠️ La agresividad del VAD es alta (>{3}), puede causar falsos negativos. Reduce a 2 o 3.")

        # VAD
        ratio = vad.get("speech_ratio")
        if ratio is not None:
            if ratio < 0.01:
                recs.append("❌ El VAD no detectó voz en el audio de prueba. Ajusta la ganancia o revisa el umbral.")
            elif ratio > 0.9:
                recs.append("ℹ️ El VAD detecta voz en casi todo el audio. Puedes ajustar la agresividad para evitar falsos positivos.")

        # STT
        if stt.get("success") is False:
            recs.append("❌ El STT no transcribió el audio. Verifica que el modelo esté instalado y que el audio sea claro.")
        elif stt.get("confidence", 0) < 0.5:
            recs.append("⚠️ La confianza del STT es baja (<0.5). Intenta hablar más claro o reduce el ruido de fondo.")

        # Pipeline
        if pl.get("success") is False:
            recs.append("❌ El pipeline completo falló. Revisa los logs para más detalles.")
            if pl.get("error"):
                recs.append(f"   Error: {pl['error']}")

        # Recomendación general
        if not recs:
            recs.append("✅ Todo parece funcionar correctamente. Si aún no escucha, verifica que el dispositivo de entrada por defecto sea el correcto.")

        self.results["recommendations"] = recs

    def print_report(self):
        print("\n" + "="*80)
        print("          DIAGNÓSTICO DE AUDIO DE AXIOMA")
        print("="*80)
        print(f"Fecha: {self.results['timestamp']}")
        print()

        sys_info = self.results.get("system", {})
        print("🔹 SISTEMA")
        print(f"  Plataforma: {sys_info.get('platform', 'N/A')}")
        print(f"  Python: {sys_info.get('python_version', 'N/A')}")
        print(f"  Dependencias de audio instaladas: {'✅' if sys_info.get('audiodevices_installed') else '❌'}")
        print(f"  Módulos de AXIOMA disponibles: {'✅' if sys_info.get('axioma_available') else '❌'}")
        if not sys_info.get('audiodevices_installed'):
            print(f"    Error: {sys_info.get('audio_error', 'Desconocido')}")
        if not sys_info.get('axioma_available'):
            print(f"    Error: {sys_info.get('axioma_error', 'Desconocido')}")
        print()

        hw = self.results.get("hardware", {})
        print("🔹 HARDWARE")
        if hw.get("input_devices"):
            print("  Dispositivos de entrada:")
            for dev in hw["input_devices"]:
                print(f"    - {dev['name']} (canales: {dev['channels']})")
            default = hw.get("default_input")
            print(f"  Dispositivo por defecto: {default} - {hw.get('default_input_name', 'N/A')}")
        else:
            print("  ❌ No se detectaron dispositivos de entrada.")

        rms = hw.get("test_recording_rms")
        if rms is not None:
            print(f"  Nivel RMS de grabación de prueba: {rms:.4f} {'✅' if rms > 0.001 else '❌'}")
        if hw.get("test_recording_error"):
            print(f"  Error en grabación: {hw['test_recording_error']}")
        print()

        cfg = self.results.get("config", {})
        print("🔹 CONFIGURACIÓN")
        if cfg.get("error"):
            print(f"  Error: {cfg['error']}")
        else:
            print(f"  Modelo STT: {cfg.get('stt_model', 'N/A')}")
            print(f"  Agresividad VAD: {cfg.get('vad_aggressiveness', 'N/A')}")
            print(f"  Frecuencia muestreo: {cfg.get('vad_sample_rate', 'N/A')} Hz")
            print(f"  Umbral confianza STT: {cfg.get('stt_confidence_threshold', 'N/A')}")
        print()

        vad = self.results.get("vad", {})
        print("🔹 VAD")
        if vad.get("error"):
            print(f"  ❌ Error: {vad['error']}")
        else:
            ratio = vad.get("speech_ratio")
            if ratio is not None:
                print(f"  Proporción de voz en audio de prueba: {ratio:.2%}")
            if vad.get("realtime_ratio") is not None:
                print(f"  Proporción de voz en tiempo real: {vad['realtime_ratio']:.2%}")
        print()

        stt = self.results.get("stt", {})
        print("🔹 STT")
        if stt.get("error"):
            print(f"  ❌ Error: {stt['error']}")
        else:
            print(f"  Transcripción: '{stt.get('transcription', 'N/A')}'")
            print(f"  Confianza: {stt.get('confidence', 0):.2f}")
            print(f"  Idioma: {stt.get('language', 'N/A')}")
            print(f"  Latencia: {stt.get('latency_ms', 0):.1f} ms")
            print(f"  Éxito: {'✅' if stt.get('success') else '❌'}")
            if stt.get("warning"):
                print(f"  ⚠️ {stt['warning']}")
        print()

        pl = self.results.get("pipeline", {})
        print("🔹 PIPELINE COMPLETO")
        if pl.get("error"):
            print(f"  ❌ Error: {pl['error']}")
        else:
            print(f"  Simulación ejecutada: {'✅' if pl.get('simulated') else '❌'}")
            print(f"  Éxito: {'✅' if pl.get('success') else '❌'}")
            print(f"  Razón: {pl.get('reason', 'N/A')}")
            print(f"  Tiempo: {pl.get('elapsed', 0):.2f} s")
        print()

        recs = self.results.get("recommendations", [])
        print("🔹 RECOMENDACIONES")
        for rec in recs:
            print(f"  {rec}")

        # Guardar JSON en el directorio reports del proyecto
        with open(self.report_file, "w") as f:
            json.dump(self.results, f, indent=2, default=str)
        print("\n" + "="*80)
        print(f"✅ Informe guardado en: {self.report_file}")
        print("="*80 + "\n")


def main():
    print("Iniciando diagnóstico de audio de AXIOMA...")
    diag = AudioDiagnostic()
    diag.run_all()
    diag.print_report()

def barrer_volumen(niveles=(40, 55, 70), segundos=3.0, fs=16000):
    """Mide RMS/pico/VAD/STT a distintos volúmenes del micrófono (✅ ISSUE-128).

    Motivo MEDIDO: con el micro al 31 % el VAD estricto no oía la voz (turnos vacíos) y
    al 85 % el audio saturaba (**RMS 0,73 · pico 2,04**) ⇒ el VAD veía "voz" en el 100 %
    de los cuadros ⇒ **el turno nunca se cerraba** (el log muestra buffers que crecen de
    4,9 s a 29,3 s sin despachar nada). El punto medio hay que medirlo, no adivinarlo.

    Uso:  venv/bin/python tests/test_audio_diagnostic.py --volumen
    Leé la MISMA frase en cada nivel; al final imprime una tabla para elegir.
    """
    import subprocess
    import numpy as np
    import soundfile as sf
    import sounddevice as sd

    frase = "¿Cuál es el clima de hoy?"
    print("\n🎚️  BARRIDO DE VOLUMEN DEL MICRÓFONO")
    print(f"   Leé SIEMPRE la misma frase en voz normal: «{frase}»\n")
    filas = []
    for nivel in niveles:
        subprocess.run(["pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"{nivel}%"],
                       capture_output=True)
        print(f"   ── volumen {nivel}% ──")
        for i in (3, 2, 1):
            print(f"      ⏳ {i}...", flush=True)
            time.sleep(1)
        print("      🔴 ¡LEÉ AHORA! (3 s)", flush=True)
        grab = sd.rec(int(segundos * fs), samplerate=fs, channels=1, dtype="float32")
        sd.wait()
        audio = grab.flatten()
        rms = float(np.sqrt(np.mean(audio ** 2)))
        pico = float(np.max(np.abs(audio)))
        # VAD: cuántos cuadros se consideran voz (ideal: ni 0 % ni 100 %).
        # ⚠️ `VADFilter.is_speech()` recibe BYTES int16 (no float): pasarle floats
        # devolvía "todo es voz" (medido: 99 % incluso con silencio) — la métrica
        # engañaba. Se convierte a int16 y se cuadra en ventanas de `frame_ms`.
        try:
            from src.multimodal.vad_filter import VADFilter
            from config.multimodal_loader import build_vad_config
            cfg = build_vad_config()
            v = VADFilter(cfg)
            muestras = max(1, int(cfg.sample_rate * cfg.frame_ms / 1000))
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            cuadros = [pcm[i:i + muestras] for i in range(0, len(pcm) - muestras, muestras)]
            voz = sum(1 for c in cuadros if v.is_speech(c.tobytes()) is True)
            ratio = voz / max(1, len(cuadros))
        except Exception:
            ratio = float("nan")
        # STT sobre la MISMA grabación
        texto, conf = "", 0.0
        try:
            from src.multimodal.stt_engine import STTEngine, STTConfig
            r = STTEngine(STTConfig(model_size="small")).transcribe(audio)
            texto, conf = (r.text or "").strip(), float(r.confidence)
        except Exception as e:
            texto = f"(error STT: {type(e).__name__})"
        wav = f"/tmp/barrido_{nivel}.wav"
        sf.write(wav, audio, fs)
        # La métrica que importa: ¿el STT entendió la frase ESPERADA?
        esperado = "clima" in texto.lower()
        estado = "✅" if (0.01 < rms < 0.5 and pico < 1.0 and esperado) else "⚠️"
        filas.append((nivel, rms, pico, ratio, conf, texto, wav, estado))
        print(f"      {estado} RMS={rms:.3f} pico={pico:.2f} voz={ratio:.0%} "
              f"conf={conf:.2f} · «{texto[:60]}»")

    print("\n   📊 RESUMEN (buscá RMS 0.01-0.15, pico <1.0, voz entre 20 % y 80 % y conf alta):")
    print("      vol   RMS    pico   voz    conf   transcripción")
    for nivel, rms, pico, ratio, conf, texto, _, _ in filas:
        print(f"      {nivel:3d}%  {rms:5.3f}  {pico:5.2f}  {ratio:4.0%}  {conf:5.2f}   {texto[:50]}")
    mejor = max(filas, key=lambda f: (f[4], -f[2]))
    print(f"\n   👉 Sugerido: {mejor[0]}%  (mejor confianza {mejor[4]:.2f})")
    print(f"      Fijalo con: pactl set-source-volume @DEFAULT_SOURCE@ {mejor[0]}%\n")
    return filas


if __name__ == "__main__":
    import sys as _sys
    if "--volumen" in _sys.argv:
        barrer_volumen()
        raise SystemExit(0)
    main()
