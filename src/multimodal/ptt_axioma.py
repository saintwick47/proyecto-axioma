#!/usr/bin/env python3
import logging
# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════
# ptt_axioma.py - Push-to-Talk (Empujar para Hablar)
# Ruta: src/multimodal/ptt_axioma.py
# Autor: SaintWick
# Versión: 1.0.0
#
# Permite hablar manteniendo presionada una tecla (Bloq Mayús/Scroll Lock),
# bypaseando el VAD y la Wake Word. Al soltar, envía la consulta al
# Dispatcher y reproduce la respuesta por TTS.
# Uso: python -m src.multimodal.ptt_axioma
# ═══════════════════════════════════════════════════════════════

import asyncio
import sys
import time
import threading
from pathlib import Path

# Añadir la raíz del proyecto al path para poder importar src.*
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
import sounddevice as sd

# Importar componentes de AXIOMA
from src.core.dispatcher import Dispatcher
from src.multimodal.stt_engine import STTEngine, STTConfig
from src.multimodal.tts_engine import TTSEngine
from src.domain.entities import Message, MessageRole

# --- CONFIGURACIÓN ---
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = 'float32'
PTT_KEY = 'scroll_lock'  # Puedes cambiarlo a 'ctrl_r' (Ctrl derecho), 'shift_r', etc.

# --- VARIABLES GLOBALES ---
_recording = False
_audio_buffer = []


def audio_callback(indata, frames, time_info, status):
    """Callback de sounddevice: acumula audio en el buffer."""
    global _audio_buffer
    if _recording:
        _audio_buffer.append(indata.copy())


def on_press(key):
    """Detecta cuando se presiona la tecla PTT."""
    global _recording, _audio_buffer
    try:
        if key == getattr(keyboard.Key, PTT_KEY.upper()):
            if not _recording:
                _recording = True
                _audio_buffer = []
                print("\n🎤 Grabando... (suelta la tecla para enviar)")
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 ptt_axioma.py:61] excepción degradada (intencional): {_d2e}")


def on_release(key):
    """Detecta cuando se suelta la tecla PTT -> procesa el audio."""
    global _recording
    try:
        if key == getattr(keyboard.Key, PTT_KEY.upper()):
            if _recording:
                _recording = False
                print("⏹️  Procesando...")
                # Disparar el procesamiento en un hilo separado
                threading.Thread(target=process_audio, daemon=True).start()
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 ptt_axioma.py:75] excepción degradada (intencional): {_d2e}")


def process_audio():
    """Procesa el audio grabado (STT -> Dispatcher -> TTS)."""
    global _audio_buffer
    if not _audio_buffer:
        return

    # Concatenar buffers
    audio_data = np.concatenate(_audio_buffer, axis=0)
    _audio_buffer = []

    if len(audio_data) < SAMPLE_RATE * 0.3:  # Menos de 300ms -> ignorar
        print("⏳ Audio demasiado corto, ignorado.")
        return

    # Aplanar a 1D
    audio_flat = audio_data.flatten()

    # ---- STT ----
    stt = STTEngine(STTConfig())
    result = stt.transcribe(audio_flat)

    if not result.is_actionable:
        print(f"❌ No se entendió (confianza: {result.confidence:.2f}). Intenta de nuevo.")
        return

    print(f"📝 Texto: '{result.text}' (confianza: {result.confidence:.2f})")

    # ---- Dispatcher ----
    try:
        # Ejecutar el dispatcher de forma síncrona dentro del hilo
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        response = loop.run_until_complete(dispatcher.process(
            Message(content=result.text, role=MessageRole.USER)
        ))
        loop.close()
    except Exception as e:
        print(f"❌ Error en Dispatcher: {e}")
        return

    if response and response.content:
        print(f"🤖 Respuesta: {response.content[:100]}...")

        # ---- TTS ----
        tts = TTSEngine()
        # Ejecutar TTS de forma síncrona
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(tts.speak(response.content))
        loop.close()
    else:
        print("❌ Respuesta vacía o con error.")


def main():
    global dispatcher
    print("🚀 Iniciando AXIOMA Push-to-Talk...")
    print(f"🎤 Mantén presionada la tecla '{PTT_KEY.upper()}' para hablar.")
    print("   Suelta la tecla para procesar la consulta.")
    print("   Presiona Ctrl+C para salir.\n")

    # Inicializar Dispatcher (una sola vez)
    dispatcher = Dispatcher()

    # Iniciar stream de audio (siempre abierto, sin bloqueo)
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        callback=audio_callback,
        blocksize=int(SAMPLE_RATE * 0.03),  # 30ms
    )
    stream.start()

    # Escuchar teclado
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            print("\n👋 Saliendo...")
        finally:
            stream.stop()
            stream.close()


if __name__ == "__main__":
    try:
        from pynput import keyboard
    except ImportError:
        print("❌ Instala pynput: pip install pynput")
        sys.exit(1)

    # Verificar que sounddevice está instalado
    try:
        import sounddevice
    except ImportError:
        print("❌ Instala sounddevice: pip install sounddevice")
        sys.exit(1)

    main()
