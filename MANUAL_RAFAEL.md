# 🎙️ MANUAL DE RAFAEL — Asistente de voz (modo Jarvis)

**Rafael** es el asistente por voz de AXIOMA, estilo Jarvis: escuchás la
wake word, le hablás y te responde por voz (TTS offline con Piper).

---

## 1. Arrancar y cerrar

| Acción | Comando |
|---|---|
| **Arrancar** (background, sobrevive al logout) | `systemctl --user start rafael.service` |
| **Arrancar** siguiendo los logs | `python Rafael.py` |
| **Cerrar** (apaga todo: voz + widget + TTS) | Botón **✕** (arriba a la derecha del orb) o **clic derecho → Cerrar** |
| **Cerrar** desde terminal | `systemctl --user stop rafael.service` |
| **Métricas de los turnos** | `python Rafael.py --stats` |

> ⚠️ Rafael **no** se auto-arranca al iniciar sesión (está `disabled`).
> Cerrar el widget (✕, Alt+F4 o el ✕ del administrador de ventanas) apaga
> Rafael **completo**.

## 2. La interfaz (widget flotante)

Al arrancar, aparece un **orb circular** flotante (frameless, siempre encima):

- **Click izquierdo** → abre el panel de chat (podés escribirle).
- **Botón ✕ rojo** (esquina superior derecha) → cerrar Rafael.
- **Clic derecho** sobre el orb → menú **Cerrar**.
- **Arrastrar** con el mouse → moverlo por la pantalla (guarda la posición).

## 3. Cómo hablarle (wake word)

Decile **"rafael dime ..."** (o "rafael, ¿...?", "rafael decime ..."):

```
"rafael dime, ¿qué tiempo hace en Buenos Aires?"
"rafael, ¿a cuánto está el dólar hoy?"
```

- La puerta de audio (micrófono calibrado a 56%) detecta que hablás y el
  reconocimiento de voz confirma que dijiste **"rafael"** antes de responder.
- Si la frase no empieza con "rafael", se ignora (no responde a charla ajena).
- Tras la wake word escuchás "Dime" y luego la respuesta por voz.

## 4. Comandos disponibles

| Pedido | Ejemplo | Respuesta |
|---|---|---|
| Clima | "¿qué tiempo hace en Buenos Aires?" | Condiciones actuales (Open-Meteo) |
| Dólar | "¿a cuánto está el dólar hoy?" | Cotizaciones oficial/blue/CCL/etc. (dolarapi) |
| Hora | "¿qué hora es?" | Hora del sistema |
| Noticias | "buscá noticias de tecnología" | Noticias recientes |
| Búsqueda web | "buscá información sobre X" | Resumen con fuentes |
| **Buscar archivo** | "buscá el archivo settings" | **Ruta exacta** del archivo/carpeta |
| **Citar un bloque** | "citá un bloque del archivo .env" | Bloque del archivo (rápido) |
| **Leer y resumir** | "leé el archivo README y resumilo" | Resumen del contenido (LLM, ~1 min en CPU) |
| Mostrar contenido | "mostrame el archivo main" | Contenido del archivo |
| Analizar documento | "analizá el documento X" | Análisis con el modelo |

> La búsqueda de archivos arranca en la carpeta del proyecto AXIOMA y salta
> directorios pesados (venv, .git, backups, caches).

## 5. Escritura por chat (widget)

Click en el orb → panel → escribí el comando (sin wake word) → Enter.
Rafael responde por texto en el panel.

## 6. Configuración

Todo está en `config/multimodal.yaml` y `.env`:

- `jarvis_wake_word` (`.env`) → la wake word (default: `rafael`).
- `jarvis_voice_confidence_threshold` (`.env`, default 0.5) → confianza mínima
  del reconocimiento para procesar un comando.
- `jarvis_proactive_enabled` (`.env`, default `false`) → si se activa, Rafael
  habla solo (clima/noticias en horarios). **OFF por default**: solo responde
  cuando le hablás.
- `multimodal.yaml` → micrófono, STT (modelo, beam_size), TTS (Piper offline),
  VAD (agresividad, AGC).

## 7. Problemas comunes

| Problema | Solución |
|---|---|
| No escucha / no responde | Verificá el micrófono por defecto (pavucontrol); el diagnóstico te ayuda: `python tests/test_audio_diagnostic.py` (leé la frase que muestra con la cuenta regresiva) |
| Micro saturado (ruido/clipping) | La ganancia se fija a 56% al iniciar sesión (`mic-gain.service`); ajustala con `amixer -c 2 sset Capture 35` |
| Tarda mucho en responder | Es CPU puro (sin GPU): el resumen de archivos puede tomar ~1 min; la búsqueda y la cita son instantáneas |
| Se cerró y no sabés por qué | Mirá `logs/rafael.log` (turnos + errores) |

---
*Rafael — AXIOMA v0.6.8 · CHANGELOG v0.6.8j–q*
