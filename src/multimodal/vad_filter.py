#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# vad_filter.py - Filtro de Actividad de Voz (Voice Activity Detection)
# Ruta: /ruta/a/axioma/src/multimodal/vad_filter.py
# Autor: SaintWick
# Versión: 0.7.1
#
# Filtra audio del micrófono usando webrtcvad (Google WebRTC VAD) o
# Silero VAD. Detecta inicio/fin de habla y retorna chunks completos
# de audio listos para STT. Soporta captura incremental para
# transcripción en tiempo real y monitor de background para barge-in.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import collections
import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, Generator, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

try:
    import webrtcvad
    _VAD_AVAILABLE = True
except ImportError:
    _VAD_AVAILABLE = False
    webrtcvad = None  # type: ignore[assignment]

try:
    import sounddevice as sd
    _SD_AVAILABLE = True
except ImportError:
    _SD_AVAILABLE = False
    sd = None  # type: ignore[assignment]


# ============================================================================
# CONFIGURACIÓN
# ============================================================================

# webrtcvad solo acepta estos sample rates
_VALID_RATES = {8000, 16000, 32000, 48000}

# webrtcvad solo acepta frames de 10ms, 20ms o 30ms
_FRAME_MS = 30          # ms por frame — 30ms balance entre latencia y precisión
_SAMPLE_RATE = 16000    # Hz — requerido por faster-whisper
_CHANNELS = 1
_DTYPE = np.int16

# Número de frames de silencio para detectar fin de habla
_SILENCE_FRAMES_END = 20     # 20 × 30ms = 600ms de silencio → fin de utterance
# Frames de voz mínimos para considerar inicio de habla real (anti falsos positivos)
_VOICE_FRAMES_MIN = 3        # 3 × 30ms = 90ms de voz mínima

# Tamaño del ring buffer para padding antes del inicio de voz
_RING_BUFFER_FRAMES = 10     # 10 × 30ms = 300ms de pre-roll


@dataclass
class VADConfig:
    """Configuración del VAD."""
    sample_rate: int = _SAMPLE_RATE
    frame_ms: int = _FRAME_MS
    aggressiveness: int = 2
    silence_frames_end: int = _SILENCE_FRAMES_END
    voice_frames_min: int = _VOICE_FRAMES_MIN
    ring_buffer_frames: int = _RING_BUFFER_FRAMES
    max_utterance_seconds: float = 30.0
    backend: str = "webrtcvad"
    # ✅ plan_rafael (2026-08-31): partials menos frecuentes → menos
    # re-transcripciones del buffer en streaming STT (1.2s en vez de 0.7s).
    min_yield_interval: float = 1.2
    # ✅ plan_rafael (2026-08-31): AGC suave en software — normaliza ganancia
    # del chunk y clampea a [-1,1] antes de Whisper (protección aunque ALSA
    # se desconfigure y el mic vuelva a saturar).
    agc_gain: float = 1.5
    # ✅ FIX v0.7.1: dispositivo de entrada (audio.input_device en
    # multimodal.yaml, ej. "pulse"). None = default del sistema, mismo
    # comportamiento que antes de este fix.
    device: Optional[str] = None


@dataclass
class AudioChunk:
    """Chunk de audio que contiene una utterance completa."""
    audio: np.ndarray            # int16, mono, 16kHz
    duration_seconds: float
    voice_ratio: float           # fracción de frames con voz detectada
    sample_rate: int = _SAMPLE_RATE
    # ✅ plan_rafael (2026-08-31): AGC suave aplicado en to_float32().
    agc_gain: float = 1.5

    @property
    def is_valid(self) -> bool:
        """True si el chunk tiene suficiente voz para transcribir."""
        return self.voice_ratio >= 0.15 and self.duration_seconds >= 0.3

    def to_float32(self) -> np.ndarray:
        """Convierte a float32 normalizado — formato requerido por faster-whisper.

        ✅ plan_rafael (2026-08-31): AGC suave + clamp [-1,1] — protege a
        Whisper de micrófonos saturados aunque ALSA se desconfigure."""
        audio = self.audio.astype(np.float32) / 32768.0 * self.agc_gain
        return np.clip(audio, -1.0, 1.0)


# ============================================================================
# VAD FILTER
# ============================================================================

class VADFilter:
    """
    Detector de actividad de voz basado en webrtcvad.

    Responsabilidades:
        1. Capturar audio del micrófono frame por frame
        2. Clasificar cada frame: voz / silencio
        3. Acumular frames hasta detectar fin de utterance
        4. Retornar AudioChunk completo listo para STT

    Criterio de aceptación: ≤ 5% falsos positivos.
    Logrado con aggressiveness=2 + voice_frames_min=3.

    Uso:
        vad = VADFilter()
        for chunk in vad._listen_legacy():
            # chunk es AudioChunk con utterance completa
            transcript = stt.transcribe(chunk)
    """

    def __init__(self, config: Optional[VADConfig] = None) -> None:
        self._config = config or VADConfig()
        self._vad = None
        self._silero_model = None
        self._silero_buf = None   # acumulador de 512 muestras (ISSUE-129)
        self._frame_samples = int(
            self._config.sample_rate * self._config.frame_ms / 1000
        )
        self._frame_bytes = self._frame_samples * 2

        # ── Monitor de background (barge-in) ─────────────────────────────
        # Stream persistente y liviano, separado de listen()/listen_once().
        # Se arranca/para explícitamente vía start_background_monitor() /
        # stop_background_monitor() — pensado para correr SOLO mientras el
        # TTS está hablando, nunca en paralelo con listen()/listen_once()
        # (dos sd.InputStream concurrentes sobre el mismo device no es
        # comportamiento garantizado por PortAudio).
        self._monitor_stream: Optional["sd.InputStream"] = None
        self._monitor_lock = threading.Lock()
        self._monitor_buffer: bytes = b""
        self._monitor_voice_run: int = 0
        self._monitor_speech_detected: bool = False
        self._monitor_min_voice_frames: int = 2  # 2×30ms=60ms — barge-in prioriza latencia baja

        if not _SD_AVAILABLE:
            logger.warning("[VAD] sounddevice no disponible — captura desactivada")
            return

        if self._config.sample_rate not in _VALID_RATES:
            raise ValueError(
                f"[VAD] sample_rate inválido: {self._config.sample_rate}. "
                f"Válidos: {_VALID_RATES}"
            )

        if self._config.backend == "silero":
            try:
                import torch
                self._silero_model, _utils = torch.hub.load(
                    repo_or_dir="snakers4/silero-vad",
                    model="silero_vad",
                    trust_repo=True,
                    verbose=False,
                )
                self._silero_model.eval()
                logger.info(
                    f"[VAD] Silero VAD cargado via torch.hub — "
                    f"rate={self._config.sample_rate}Hz "
                    f"frame={self._config.frame_ms}ms"
                )
                return
            except Exception as e:
                logger.warning(
                    f"[VAD] Silero no disponible ({e}) — fallback a webrtcvad"
                )
                self._config.backend = "webrtcvad"

        if not _VAD_AVAILABLE:
            logger.warning("[VAD] webrtcvad no disponible — VAD desactivado")
            return

        self._vad = webrtcvad.Vad(self._config.aggressiveness)
        logger.info(
            f"[VAD] Inicializado — rate={self._config.sample_rate}Hz "
            f"frame={self._config.frame_ms}ms "
            f"aggressiveness={self._config.aggressiveness} "
            f"device={self._config.device!r}"
        )

    @property
    def is_available(self) -> bool:
        if self._config.backend == "silero" and self._silero_model is not None:
            return _SD_AVAILABLE
        return self._vad is not None and _SD_AVAILABLE

    def is_speech(self, frame: bytes) -> bool:
        """
        True si el frame contiene voz humana.
        frame: bytes PCM int16 mono de exactamente frame_bytes longitud.
        Despacha al backend configurado (silero o webrtcvad).
        """
        if self._config.backend == "silero" and self._silero_model is not None:
            try:
                return self._silero_is_speech(frame)
            except Exception:
                return self._webrtcvad_is_speech(frame)
        return self._webrtcvad_is_speech(frame)

    def _silero_is_speech(self, frame: bytes) -> bool:
        """Clasifica con silero respetando SU tamano de ventana (ISSUE-129).

        Bug MEDIDO (2026-10-01): se le pasaban los cuadros del pipeline (480 muestras por
        `frame_ms: 30`) y silero exige EXACTAMENTE 512 muestras a 16 kHz => devolvia "voz"
        en el 100 % de los cuadros con cualquier nivel de micro (comparado sobre las
        grabaciones reales del usuario: webrtcvad 36/71/51 % de voz vs silero 100/100/100 %)
        => la utterance NUNCA se cerraba (buffers de 4,9 s a 29,3 s) y Rafael no respondia.

        Solucion: se ACUMULAN las muestras y se evaluan ventanas exactas de 512; si el
        cuadro trae varias, alcanza con que UNA tenga voz. El modelo es recurrente, asi
        que se alimenta en orden.
        """
        import numpy as _np
        muestras = _np.frombuffer(frame, dtype=_np.int16).astype(_np.float32) / 32768.0
        if self._silero_buf is None:
            self._silero_buf = _np.zeros(0, dtype=_np.float32)
        self._silero_buf = _np.concatenate([self._silero_buf, muestras])
        ventana = 512 if self._config.sample_rate == 16000 else 256
        hablado = False
        while len(self._silero_buf) >= ventana:
            trozo = self._silero_buf[:ventana]
            self._silero_buf = self._silero_buf[ventana:]
            import torch
            with torch.no_grad():
                prob = self._silero_model(
                    torch.from_numpy(trozo), self._config.sample_rate).item()
            hablado = hablado or prob >= 0.5
        return hablado

    def _webrtcvad_is_speech(self, frame: bytes) -> bool:
        """Clasificación voz/silencio via webrtcvad. Fallback si no hay VAD: True."""
        if self._vad is None:
            return True
        try:
            return self._vad.is_speech(frame, self._config.sample_rate)
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════════════════
    # Monitor de background — chequeo de voz sin capturar una utterance
    # ══════════════════════════════════════════════════════════════════════
    # [FIX barge-in] voice_pipeline.py._barge_in_monitor() ya llamaba a
    # self._vad.is_speech_detected() protegido con hasattr() — el método
    # nunca existió en esta clase, así que hasattr() daba False siempre y
    # el barge-in quedaba muerto en silencio (sin excepción, sin log).
    # Estos 3 métodos son la implementación real que faltaba.

    def start_background_monitor(self) -> None:
        """
        Arranca un sd.InputStream propio y liviano para detectar voz entrante
        sin capturar una utterance completa. Idempotente — no hace nada si
        ya está corriendo. Pensado para arrancar justo cuando el TTS empieza
        a hablar y pararse cuando termina (ver stop_background_monitor).
        """
        if not _SD_AVAILABLE or self._vad is None and self._config.backend != "silero":
            return
        with self._monitor_lock:
            if self._monitor_stream is not None:
                return  # ya corriendo
            self._monitor_buffer = b""
            self._monitor_voice_run = 0
            self._monitor_speech_detected = False

        def _monitor_callback(indata: np.ndarray, frames: int, time_info, status) -> None:
            if status:
                logger.debug(f"[VAD] monitor sounddevice status: {status}")
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            with self._monitor_lock:
                self._monitor_buffer += pcm
                while len(self._monitor_buffer) >= self._frame_bytes:
                    frame = self._monitor_buffer[: self._frame_bytes]
                    self._monitor_buffer = self._monitor_buffer[self._frame_bytes:]
                    is_voice = self.is_speech(frame)
                    if is_voice:
                        self._monitor_voice_run += 1
                        if self._monitor_voice_run >= self._monitor_min_voice_frames:
                            self._monitor_speech_detected = True
                    else:
                        self._monitor_voice_run = 0

        try:
            stream = sd.InputStream(
                samplerate=self._config.sample_rate,
                channels=_CHANNELS,
                dtype="float32",
                blocksize=self._frame_samples,
                device=self._config.device,
                callback=_monitor_callback,
            )
            stream.start()
            with self._monitor_lock:
                self._monitor_stream = stream
            logger.debug("[VAD] Monitor de background iniciado")
        except Exception as e:
            logger.warning(f"[VAD] No se pudo iniciar monitor de background: {e}")
            with self._monitor_lock:
                self._monitor_stream = None

    def stop_background_monitor(self) -> None:
        """Para y cierra el stream del monitor de background. Idempotente."""
        with self._monitor_lock:
            stream = self._monitor_stream
            self._monitor_stream = None
            self._monitor_buffer = b""
            self._monitor_voice_run = 0
            self._monitor_speech_detected = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
                logger.debug("[VAD] Monitor de background detenido")
            except Exception as e:
                logger.warning(f"[VAD] Error deteniendo monitor de background: {e}")

    def is_speech_detected(self) -> bool:
        """
        True si el monitor de background detectó voz reciente.
        No bloqueante — solo lee el flag actualizado por el callback del
        stream. Retorna False si el monitor no está corriendo.
        """
        with self._monitor_lock:
            return self._monitor_speech_detected

    def _listen_legacy(self) -> Generator[AudioChunk, None, None]:
        """
        [LEGACY — sin callers; el pipeline usa listen_once()]
        Genera AudioChunks — una utterance completa por yield.
        Bloqueante. Usar en thread separado o con asyncio.run_in_executor().

        Pipeline interno:
            frame (30ms) → VAD → ring buffer → acumulación → fin silencio → yield
        """
        if not self.is_available:
            logger.error("[VAD] No disponible para captura")
            return

        ring_buffer: collections.deque = collections.deque(
            maxlen=self._config.ring_buffer_frames
        )
        triggered = False
        voiced_frames: List[bytes] = []
        voice_frame_count = 0
        silence_frame_count = 0
        utterance_start = 0.0

        logger.info("[VAD] Escuchando micrófono...")

        # Buffer de captura: acumular hasta tener un frame completo
        capture_buffer = b""

        def _audio_callback(indata: np.ndarray, frames: int, time_info, status) -> None:
            nonlocal capture_buffer
            if status:
                logger.debug(f"[VAD] sounddevice status: {status}")
            # Convertir float32 → int16
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            capture_buffer += pcm

        # Calcular blocksize en samples que da ~frame_ms ms
        blocksize = self._frame_samples

        with sd.InputStream(
            samplerate=self._config.sample_rate,
            channels=_CHANNELS,
            dtype="float32",
            blocksize=blocksize,
            device=self._config.device,
            callback=_audio_callback,
        ):
            while True:
                # Esperar hasta tener un frame completo
                while len(capture_buffer) < self._frame_bytes:
                    time.sleep(0.005)

                frame = capture_buffer[: self._frame_bytes]
                capture_buffer = capture_buffer[self._frame_bytes:]

                is_voice = self.is_speech(frame)

                if not triggered:
                    ring_buffer.append((frame, is_voice))
                    voice_count = sum(1 for _, v in ring_buffer if v)

                    if voice_count >= self._config.voice_frames_min:
                        # Inicio de utterance confirmado
                        triggered = True
                        utterance_start = time.perf_counter()
                        voiced_frames = [f for f, _ in ring_buffer]
                        voice_frame_count = voice_count
                        silence_frame_count = 0
                        ring_buffer.clear()
                        logger.debug("[VAD] Inicio de utterance detectado")
                else:
                    voiced_frames.append(frame)
                    if is_voice:
                        voice_frame_count += 1
                        silence_frame_count = 0
                    else:
                        silence_frame_count += 1

                    elapsed = time.perf_counter() - utterance_start

                    # Fin de utterance: silencio suficiente O timeout
                    end_by_silence = silence_frame_count >= self._config.silence_frames_end
                    end_by_timeout = elapsed >= self._config.max_utterance_seconds

                    if end_by_silence or end_by_timeout:
                        # Construir chunk
                        audio_bytes = b"".join(voiced_frames)
                        audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                        total_frames = len(voiced_frames)
                        voice_ratio = voice_frame_count / max(total_frames, 1)
                        duration = total_frames * self._config.frame_ms / 1000

                        chunk = AudioChunk(
                            audio=audio_array,
                            duration_seconds=duration,
                            voice_ratio=voice_ratio,
                            sample_rate=self._config.sample_rate,
                        )

                        if end_by_timeout:
                            logger.warning(f"[VAD] Utterance cortada por timeout ({elapsed:.1f}s)")
                        else:
                            logger.debug(
                                f"[VAD] Utterance completa — "
                                f"dur={duration:.1f}s voice_ratio={voice_ratio:.0%}"
                            )

                        yield chunk

                        # Reset estado
                        triggered = False
                        voiced_frames = []
                        voice_frame_count = 0
                        silence_frame_count = 0
                        ring_buffer.clear()

    async def listen_incremental(
        self,
        timeout: float = 10.0,
        min_yield_interval: float = 1.2,  # ✅ plan_rafael: 1.2s (antes 0.7s)
        max_duration: Optional[float] = None,
    ) -> "AsyncGenerator[Tuple[np.ndarray, bool], None]":
        """
        ✅ v0.7.0: Captura incremental — yields (audio_so_far, is_final) mientras
        el usuario habla, en vez de esperar a que termine para devolver un solo
        AudioChunk completo (eso es lo que hace listen_once()).

        audio_so_far es el buffer COMPLETO acumulado desde el inicio de la
        utterance en cada yield (float32, formato faster-whisper) — no un delta.
        No hay reset entre yields; cada uno reemplaza al anterior con más audio.

        Misma máquina de estados que _listen_with_deadline() (ring_buffer para
        pre-roll, voice_frames_min para confirmar inicio, silence_frames_end
        para fin) pero corriendo sobre un sd.InputStream con callback (mismo
        patrón que start_background_monitor) en vez del loop bloqueante con
        time.sleep — así se puede yieldear periódicamente sin bloquear el
        event loop.

        Args:
            timeout: segundos máximos esperando que ARRANQUE el habla. Si no
                     hay voz en ese lapso, retorna sin yieldear nada (igual
                     que listen_once() devolviendo None).
            min_yield_interval: segundos mínimos de habla nueva entre yields
                     no-finales. Controla el costo de CPU — cada yield implica
                     una re-transcripción completa en transcribe_partial()
                     (no hay streaming incremental nativo en Whisper), así que
                     subir este valor baja el costo a cambio de partials menos
                     frecuentes.
            max_duration: tope de duración de la utterance. None = usa
                     self._config.max_utterance_seconds.

        No usar en paralelo con listen()/listen_once()/start_background_monitor()
        — abre su propio sd.InputStream, y dos streams de input concurrentes
        sobre el mismo device no es comportamiento garantizado por PortAudio.
        """
        if not self.is_available:
            return

        effective_max_duration = max_duration or self._config.max_utterance_seconds

        ring_buffer: collections.deque = collections.deque(
            maxlen=self._config.ring_buffer_frames
        )
        triggered = False
        voiced_frames: List[bytes] = []
        voice_frame_count = 0
        silence_frame_count = 0
        utterance_start = 0.0
        last_yield_at = 0.0

        buf_lock = threading.Lock()
        capture_buffer = b""

        def _audio_callback(indata: np.ndarray, frames: int, time_info, status) -> None:
            nonlocal capture_buffer
            if status:
                logger.debug(f"[VAD] listen_incremental sounddevice status: {status}")
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            with buf_lock:
                capture_buffer += pcm

        try:
            stream = sd.InputStream(
                samplerate=self._config.sample_rate,
                channels=_CHANNELS,
                dtype="float32",
                blocksize=self._frame_samples,
                device=self._config.device,
                callback=_audio_callback,
            )
            stream.start()
        except Exception as e:
            logger.warning(f"[VAD] listen_incremental: no se pudo abrir InputStream: {e}")
            return

        wait_start = time.perf_counter()

        try:
            while True:
                await asyncio.sleep(0.02)

                # Sin voz confirmada todavía — chequear timeout de espera
                if not triggered and (time.perf_counter() - wait_start) > timeout:
                    logger.debug("[VAD] listen_incremental: timeout esperando inicio de habla")
                    return

                should_yield_final = False
                final_elapsed = 0.0
                final_by_timeout = False

                # ✅ FIX starvation (confirmado con log real: duración creciendo
                # de a ~0.03s = 1 frame por ciclo, 0 utterances completadas en
                # 6s de prueba): drenar TODOS los frames de 30ms disponibles
                # ANTES de decidir si corresponde yieldear. Antes, el chequeo de
                # min_yield_interval estaba adentro de este loop — como la
                # transcripción tarda más que min_yield_interval (~0.9s vs
                # 0.7s), al reanudar el generador ya había pasado tiempo de
                # sobra y se yieldeaba tras procesar apenas 1 frame nuevo,
                # dejando el resto del backlog de capture_buffer sin drenar.
                # silence_frame_count/voice_frame_count avanzaban a ritmo de
                # audio real solo por ese 1 frame/ciclo — ~30x más lento que
                # el habla real — así que end_by_silence casi nunca se
                # alcanzaba en una ventana de prueba corta.
                while True:
                    with buf_lock:
                        if len(capture_buffer) < self._frame_bytes:
                            break
                        frame = capture_buffer[: self._frame_bytes]
                        capture_buffer = capture_buffer[self._frame_bytes:]

                    is_voice = self.is_speech(frame)

                    if not triggered:
                        ring_buffer.append((frame, is_voice))
                        voice_count = sum(1 for _, v in ring_buffer if v)

                        if voice_count >= self._config.voice_frames_min:
                            triggered = True
                            utterance_start = time.perf_counter()
                            last_yield_at = utterance_start
                            voiced_frames = [f for f, _ in ring_buffer]
                            voice_frame_count = voice_count
                            silence_frame_count = 0
                            ring_buffer.clear()
                            logger.debug("[VAD] listen_incremental: inicio de utterance detectado")
                    else:
                        voiced_frames.append(frame)
                        if is_voice:
                            voice_frame_count += 1
                            silence_frame_count = 0
                        else:
                            silence_frame_count += 1

                        elapsed = time.perf_counter() - utterance_start
                        end_by_silence = silence_frame_count >= self._config.silence_frames_end
                        end_by_timeout = elapsed >= effective_max_duration

                        if end_by_silence or end_by_timeout:
                            should_yield_final = True
                            final_elapsed = elapsed
                            final_by_timeout = end_by_timeout
                            break  # utterance terminada — cortar el drenado acá

                if should_yield_final:
                    audio_so_far = self._frames_to_float32(voiced_frames, gain=self._config.agc_gain)
                    if final_by_timeout:
                        logger.warning(
                            f"[VAD] listen_incremental: utterance cortada por "
                            f"timeout ({final_elapsed:.1f}s)"
                        )
                    else:
                        logger.debug(
                            f"[VAD] listen_incremental: utterance completa — "
                            f"dur={final_elapsed:.1f}s"
                        )
                    yield audio_so_far, True
                    return

                # Yield parcial — recién acá, con el backlog ya drenado por
                # completo, solo si pasó min_yield_interval desde el último
                if triggered:
                    now = time.perf_counter()
                    if now - last_yield_at >= min_yield_interval:
                        last_yield_at = now
                        yield self._frames_to_float32(voiced_frames, gain=self._config.agc_gain), False
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 vad_filter.py:614] excepción degradada (intencional): {_d2e}")

    @classmethod
    def _frames_to_float32(cls, frames: List[bytes], gain: float = 1.5) -> np.ndarray:
        """Concatena frames int16 y convierte a float32 normalizado (formato faster-whisper).

        ✅ plan_rafael (2026-08-31): AGC suave (gain) + clamp [-1,1]."""
        audio_bytes = b"".join(frames)
        audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
        audio = audio_array.astype(np.float32) / 32768.0 * gain
        return np.clip(audio, -1.0, 1.0)

    def listen_once(self, timeout: float = 10.0) -> Optional[AudioChunk]:
        """
        Captura una sola utterance y retorna. Útil para tests y modo PTT.
        Retorna None si no hay voz en el timeout dado.

        [FIX 1.5] El timeout ahora se evalúa dentro del loop interno de captura
        de frames — previene bloqueo infinito si el micrófono está desenchufado
        o VAD nunca detecta voz.
        """
        if not self.is_available:
            return None
        deadline = time.perf_counter() + timeout
        start = time.perf_counter()
        for chunk in self._listen_with_deadline(deadline):
            if chunk.is_valid:
                return chunk
            if time.perf_counter() - start > timeout:
                return None
        return None

    def _listen_with_deadline(self, deadline: float) -> "Generator[AudioChunk, None, None]":
        """
        Versión de listen() con deadline verificado en el loop interno de frames.
        Aborta limpiamente cuando time.perf_counter() > deadline.
        """
        if not self.is_available:
            return

        ring_buffer: collections.deque = collections.deque(
            maxlen=self._config.ring_buffer_frames
        )
        triggered = False
        voiced_frames: List[bytes] = []
        voice_frame_count = 0
        silence_frame_count = 0
        utterance_start = 0.0

        capture_buffer = b""

        def _audio_callback(indata: np.ndarray, frames: int, time_info, status) -> None:
            nonlocal capture_buffer
            if status:
                logger.debug(f"[VAD] sounddevice status: {status}")
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            capture_buffer += pcm

        blocksize = self._frame_samples

        with sd.InputStream(
            samplerate=self._config.sample_rate,
            channels=_CHANNELS,
            dtype="float32",
            blocksize=blocksize,
            device=self._config.device,
            callback=_audio_callback,
        ):
            while True:
                # [FIX 1.5] Verificar deadline en el loop de espera de frames
                while len(capture_buffer) < self._frame_bytes:
                    if time.perf_counter() > deadline:
                        logger.debug("[VAD] Deadline alcanzado en loop de captura — abortando")
                        return
                    time.sleep(0.005)

                frame = capture_buffer[: self._frame_bytes]
                capture_buffer = capture_buffer[self._frame_bytes:]

                is_voice = self.is_speech(frame)

                if not triggered:
                    ring_buffer.append((frame, is_voice))
                    voice_count = sum(1 for _, v in ring_buffer if v)

                    if voice_count >= self._config.voice_frames_min:
                        triggered = True
                        utterance_start = time.perf_counter()
                        voiced_frames = [f for f, _ in ring_buffer]
                        voice_frame_count = voice_count
                        silence_frame_count = 0
                        ring_buffer.clear()
                        logger.debug("[VAD] Inicio de utterance detectado")
                else:
                    voiced_frames.append(frame)
                    if is_voice:
                        voice_frame_count += 1
                        silence_frame_count = 0
                    else:
                        silence_frame_count += 1

                    elapsed = time.perf_counter() - utterance_start
                    end_by_silence = silence_frame_count >= self._config.silence_frames_end
                    end_by_timeout = elapsed >= self._config.max_utterance_seconds

                    if end_by_silence or end_by_timeout:
                        audio_bytes = b"".join(voiced_frames)
                        audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                        total_frames = len(voiced_frames)
                        voice_ratio = voice_frame_count / max(total_frames, 1)
                        duration = total_frames * self._config.frame_ms / 1000

                        chunk = AudioChunk(
                            audio=audio_array,
                            duration_seconds=duration,
                            voice_ratio=voice_ratio,
                            sample_rate=self._config.sample_rate,
                        )

                        if end_by_timeout:
                            logger.warning(f"[VAD] Utterance cortada por timeout ({elapsed:.1f}s)")
                        else:
                            logger.debug(
                                f"[VAD] Utterance completa — "
                                f"dur={duration:.1f}s voice_ratio={voice_ratio:.0%}"
                            )

                        yield chunk
                        return  # _listen_with_deadline yield UNA utterance y termina
