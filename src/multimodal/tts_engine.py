#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — TTS Engine (Text-to-Speech)
# Ruta: src/multimodal/tts_engine.py
# Autor: SaintWick
# Versión: 0.7.0 (Semana 3 — C3 + [A2])
# ✅ [A2]: TTSConfig.chunk_size_ms + _abort_event + speak_chunked() + abort()
# Cambios:
#   - Cadena fallback: edge-tts → pyttsx3 (offline) → print()
#   - Cache local de audios generados (MD5 → .mp3)
#   - speak() intenta cache antes de red
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import tempfile

import numpy as np  # ✅ plan_rafael: usado por _piper_synthesize (chunks int16)
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

try:
    import edge_tts
    _EDGE_TTS_AVAILABLE = True
except ImportError:
    edge_tts = None
    _EDGE_TTS_AVAILABLE = False

try:
    import sounddevice as sd
    import soundfile as sf
    _PLAYBACK_AVAILABLE = True
except ImportError:
    sd = None
    sf = None
    _PLAYBACK_AVAILABLE = False

# ✅ Semana 3 C3: pyttsx3 como fallback offline
try:
    import pyttsx3
    _PYTTSX3_AVAILABLE = True
except ImportError:
    pyttsx3 = None
    _PYTTSX3_AVAILABLE = False

try:
    from piper import PiperVoice
    _PIPER_AVAILABLE = True
except ImportError:
    PiperVoice = None
    _PIPER_AVAILABLE = False

# ✅ Semana 3 C3: directorio de cache local
try:
    from config.paths import Paths
    _TTS_CACHE_DIR: Optional[Path] = Paths.CACHE / "tts"
    _TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    _TTS_CACHE_DIR = None


# ── Configuración ─────────────────────────────────────────────────────────────

@dataclass
class TTSConfig:
    voice: str = "es-ES-AlvaroNeural"
    fallback_voice: str = "es-ES-ElviraNeural"
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"
    play_audio: bool = True
    timeout_seconds: float = 10.0
    chunk_size_ms: int = 200
    piper_model: str = ""


# ── Pre-procesador ────────────────────────────────────────────────────────────

class _TTSPreprocessor:
    _PATH_RE       = re.compile(r'(?<!\w)(/[\w./\-_]+(?:/[\w./\-_]*)*)')
    _URL_RE        = re.compile(r'https?://\S+')
    _CODE_INLINE_RE = re.compile(r'`([^`]+)`')
    _CODE_BLOCK_RE  = re.compile(r'```[\s\S]*?```')
    _MARKDOWN_RE    = re.compile(r'[*_]{1,3}([^*_]+)[*_]{1,3}')
    _LINENUM_RE     = re.compile(r'\bL(\d+)\b')

    @classmethod
    def process(cls, text: str) -> str:
        text = cls._CODE_BLOCK_RE.sub('...código omitido...', text)
        text = cls._URL_RE.sub('la dirección web', text)
        text = cls._PATH_RE.sub(cls._path_to_speech, text)
        text = cls._CODE_INLINE_RE.sub(r'\1', text)
        text = cls._MARKDOWN_RE.sub(r'\1', text)
        text = cls._LINENUM_RE.sub(r'línea \1', text)
        text = re.sub(r'[#\[\]{}|\\<>]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @staticmethod
    def _path_to_speech(match: re.Match) -> str:
        parts = [p for p in match.group(1).split('/') if p]
        if not parts:
            return 'la raíz'
        if len(parts) == 1:
            return f"el archivo {parts[0]}"
        return f"{'barra '.join(parts[:-1])} barra {parts[-1]}"


# ── TTSEngine ─────────────────────────────────────────────────────────────────

class TTSEngine:
    """
    Motor TTS con cadena de fallback:
      1. Cache local (sin red)
      2. edge-tts (Microsoft cloud, requiere internet)
      3. pyttsx3 (offline, voz sintética)
      4. print() (último recurso)
    """

    def __init__(self, config: Optional[TTSConfig] = None) -> None:
        self._config = config or TTSConfig()
        self._preprocessor = _TTSPreprocessor()
        self._stats = {
            "total_speaks": 0,
            "cache_hits": 0,
            "piper_used": 0,
            "edge_tts_used": 0,
            "pyttsx3_used": 0,
            "fallback_print": 0,
            "errors": 0,
            "avg_latency_ms": 0.0,
        }
        # ✅ [A2]: event de abort para barge-in
        self._abort_event = asyncio.Event()
        self._abort_event.set()  # iniciar en "no abort"

        self._piper = None
        if _PIPER_AVAILABLE and self._config.piper_model:
            try:
                self._piper = PiperVoice.load(self._config.piper_model)
                logger.info(f"[TTS] Piper TTS cargado: {self._config.piper_model}")
            except Exception as e:
                logger.warning(f"[TTS] Piper init error (desactivado): {e}")

        if not _EDGE_TTS_AVAILABLE:
            logger.warning("[TTS] edge-tts no disponible")
        if not _PYTTSX3_AVAILABLE:
            logger.warning("[TTS] pyttsx3 no disponible — solo print() offline")
        if not _PLAYBACK_AVAILABLE:
            logger.warning("[TTS] sounddevice/soundfile no disponible")
        logger.info(
            f"[TTS] Init — piper={self._piper is not None} "
            f"edge={_EDGE_TTS_AVAILABLE} "
            f"pyttsx3={_PYTTSX3_AVAILABLE} cache={_TTS_CACHE_DIR is not None}"
        )

    @property
    def is_available(self) -> bool:
        return _EDGE_TTS_AVAILABLE or _PYTTSX3_AVAILABLE

    async def speak(self, text: str) -> bool:
        """
        Sintetiza y reproduce texto.
        Cadena: cache → edge-tts → pyttsx3 → print().
        """
        if not text.strip():
            return True

        t0 = time.perf_counter()
        processed = self._preprocessor.process(text)

        # ── 1. Cache local ────────────────────────────────────────────────────
        if _TTS_CACHE_DIR and self._config.play_audio and _PLAYBACK_AVAILABLE:
            cache_key = hashlib.md5(processed.encode()).hexdigest()
            cache_file = _TTS_CACHE_DIR / f"{cache_key}.mp3"
            if cache_file.exists():
                try:
                    await self._play_file(str(cache_file))
                    self._stats["cache_hits"] += 1
                    self._update_stats(time.perf_counter() - t0, "cache")
                    logger.debug(f"[TTS] Cache hit: {cache_key}")
                    return True
                except Exception as e:
                    # R3: caché corrupto/no reproducible → se sigue con la cadena de TTS.
                    log_degraded(logger, e, f"TTSEngine.speak (caché {cache_key})",
                                 contract_level=logging.DEBUG)
        else:
            cache_file = None
            cache_key = None

        # ── 2. Piper TTS (offline, alta calidad — activar con: pip install piper-tts) ─
        if _PIPER_AVAILABLE and self._piper is not None:
            try:
                loop = asyncio.get_running_loop()
                audio_bytes = await loop.run_in_executor(
                    None, self._piper_synthesize, processed
                )
                if audio_bytes:
                    if self._config.play_audio and _PLAYBACK_AVAILABLE:
                        await self._play_buffer(io.BytesIO(audio_bytes))
                    self._stats["piper_used"] += 1
                    self._update_stats(time.perf_counter() - t0, "piper")
                    logger.debug(f"[TTS] Piper OK — '{processed[:40]}'")
                    return True
            except Exception as e:
                logger.warning(f"[TTS] Piper error ({e}) → edge-tts")

        # ── 3. edge-tts (requiere internet) ───────────────────────────────────
        if _EDGE_TTS_AVAILABLE:
            try:
                communicate = edge_tts.Communicate(
                    text=processed,
                    voice=self._config.voice,
                    rate=self._config.rate,
                    volume=self._config.volume,
                    pitch=self._config.pitch,
                )
                audio_buffer = io.BytesIO()
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_buffer.write(chunk["data"])
                audio_buffer.seek(0)

                # Guardar en cache para próximas veces
                if _TTS_CACHE_DIR and cache_file:
                    try:
                        cache_file.write_bytes(audio_buffer.read())
                        audio_buffer.seek(0)
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 tts_engine.py:235] excepción degradada (intencional): {_d2e}")

                if self._config.play_audio and _PLAYBACK_AVAILABLE:
                    await self._play_buffer(audio_buffer)
                else:
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                        f.write(audio_buffer.read())
                        logger.debug(f"[TTS] Audio guardado: {f.name}")

                self._stats["edge_tts_used"] += 1
                self._update_stats(time.perf_counter() - t0, "edge_tts")
                logger.debug(f"[TTS] edge-tts OK — '{processed[:40]}'")
                return True

            except asyncio.TimeoutError:
                logger.warning("[TTS] edge-tts timeout → pyttsx3")
            except Exception as e:
                logger.warning(f"[TTS] edge-tts error ({e}) → pyttsx3")
                self._stats["errors"] += 1

        # ── 4. pyttsx3 (offline) ─────────────────────────────────────────────
        if _PYTTSX3_AVAILABLE:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._pyttsx3_speak, processed)
                self._stats["pyttsx3_used"] += 1
                self._update_stats(time.perf_counter() - t0, "pyttsx3")
                logger.debug(f"[TTS] pyttsx3 OK — '{processed[:40]}'")
                return True
            except Exception as e:
                logger.warning(f"[TTS] pyttsx3 error ({e}) → print()")

        # ── 5. print() último recurso ─────────────────────────────────────────
        return self._fallback_print(text, t0)

    def _pyttsx3_speak(self, text: str) -> None:
        engine = pyttsx3.init()
        engine.setProperty("rate", 150)
        engine.say(text)
        engine.runAndWait()

    def _piper_synthesize(self, text: str) -> Optional[bytes]:
        """
        Sintetiza texto con Piper TTS. Retorna bytes WAV o None si falla.

        ✅ plan_rafael (2026-08-31): reescrito para piper-tts ≥1.3 — la API
        vieja (synthesize(text, wav_file)) desapareció: ahora synthesize()
        devuelve AudioChunk (audio_int16_array) y el sample_rate sale del
        config del modelo. El código anterior fallaba con "channels not
        specified" en piper 1.4.2.
        """
        if not _PIPER_AVAILABLE or self._piper is None:
            return None
        try:
            import wave
            from piper.config import SynthesisConfig
            chunks = list(self._piper.synthesize(text, syn_config=SynthesisConfig()))
            if not chunks:
                return None
            audio = np.concatenate([c.audio_int16_array for c in chunks])
            sample_rate = int(getattr(self._piper.config, "sample_rate", 22050))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(audio.tobytes())
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error(f"[TTS] Piper synthesis error: {e}")
            return None

    async def _play_file(self, path: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._play_file_sync, path)

    def _play_file_sync(self, path: str) -> None:
        try:
            data, samplerate = sf.read(path, dtype="float32")
            sd.play(data, samplerate)
            sd.wait()
        except Exception as e:
            logger.error(f"[TTS] Error reproduciendo archivo: {e}")

    async def _play_buffer(self, audio_buffer: io.BytesIO) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._play_buffer_sync, audio_buffer)

    def _play_buffer_sync(self, audio_buffer: io.BytesIO) -> None:
        try:
            data, samplerate = sf.read(audio_buffer, dtype="float32")
            sd.play(data, samplerate)
            sd.wait()
        except Exception as e:
            logger.error(f"[TTS] Error reproduciendo buffer: {e}")

    def _fallback_print(self, text: str, t0: float) -> bool:
        print(f"🔊 AXIOMA: {text}")
        self._stats["fallback_print"] += 1
        self._update_stats(time.perf_counter() - t0, "print")
        return False

    async def speak_chunked(self, text: str, barge_in_event: asyncio.Event) -> bool:
        """
        [A2] TTS en chunks con detección de barge-in.
        [Fase 4.1] Chunking por puntuación (oraciones completas) en lugar de
        conteo de palabras — más natural, el barge-in interrumpe entre oraciones
        y no corta palabras a la mitad.
        Retorna True si completó, False si fue interrumpido.
        """
        if not text.strip():
            return True

        preprocessed = self._preprocessor.process(text)
        chunks = self._split_by_punctuation(preprocessed)

        for chunk in chunks:
            if barge_in_event.is_set():
                logger.info("[TTS] speak_chunked: barge-in detectado — abortando")
                return False
            if not chunk.strip():
                continue
            try:
                await self.speak(chunk)
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"[TTS] speak_chunked chunk error: {e}")

        return True

    @staticmethod
    def _split_by_punctuation(text: str) -> List[str]:
        """
        [Fase 4.1] Divide texto en oraciones completas usando puntuación.
        Fallback a chunks por palabras si la oración supera ~15 palabras.
        """
        import re as _re
        # Dividir en oraciones por ". ", "! ", "? ", ", " — mantener el separador
        sentences = _re.split(r'(?<=[.!?,])\s+', text.strip())
        result = []
        for sentence in sentences:
            words = sentence.split()
            if len(words) <= 15:
                if sentence.strip():
                    result.append(sentence.strip())
            else:
                # Oración larga: partir en sub-chunks de 15 palabras
                for i in range(0, len(words), 15):
                    sub = " ".join(words[i:i + 15])
                    if sub.strip():
                        result.append(sub)
        return result if result else [text]

    def abort(self) -> None:
        """[A2] Aborta el TTS en curso — thread-safe via Event."""
        self._abort_event.set()
        logger.debug("[TTS] abort() llamado")

    def speak_sync(self, text: str) -> bool:
        try:
            try:
                loop = asyncio.get_running_loop()
                # Loop activo — correr en thread separado para no bloquear
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, self.speak(text))
                    return future.result(timeout=self._config.timeout_seconds + 2)
            except RuntimeError:
                # No hay loop activo — seguro usar asyncio.run()
                return asyncio.run(self.speak(text))
        except Exception as e:
            logger.error(f"[TTS] speak_sync error: {e}")
            print(f"🔊 AXIOMA: {text}")
            return False

    async def speak_confirmation_request(self) -> None:
        await self.speak("¿Podés repetir? No entendí bien.")

    async def speak_error(self, brief: str) -> None:
        await self.speak(f"Hubo un problema: {brief}")

    def _update_stats(self, elapsed: float, channel: str) -> None:
        latency_ms = elapsed * 1000
        n = self._stats["total_speaks"]
        self._stats["total_speaks"] += 1
        self._stats["avg_latency_ms"] = (
            self._stats["avg_latency_ms"] * n + latency_ms
        ) / (n + 1)

    def get_stats(self) -> dict:
        return dict(self._stats)

    @staticmethod
    def preprocess_text(text: str) -> str:
        return _TTSPreprocessor.process(text)
