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


---

## La voz con contenedor (Docker)

Rafael también puede correr **dentro del contenedor**. Es un servicio aparte, detrás de un perfil, que no
arranca salvo que lo pidas:

```bash
docker compose --profile voz up -d voz      # encender la voz
docker compose --profile voz stop voz       # apagarla
docker compose logs -f voz                  # ver cómo va
```

**Si lo corrés a mano, pasale los datos de tu equipo** (el lanzador ya los calcula solo): el grupo
`audio` no es el mismo en todas las distribuciones (Debian/Ubuntu 29 · Fedora 63 · Arch/CachyOS 996) y el
socket del servidor de sonido lleva tu UID:

```bash
AXIOMA_UID=$(id -u) AXIOMA_AUDIO_GID=$(getent group audio | cut -d: -f3) \
  docker compose --profile voz up -d voz
```

**Qué necesita** (todo ya configurado en `docker-compose.yml`; los tres valores los calcula el lanzador,
así que no hay que tocar nada a mano):

| Pieza | Por qué |
|---|---|
| `devices: [/dev/snd]` y `group_add: [<gid de audio>]` | el micrófono y los parlantes del equipo |
| El socket del servidor de sonido (`/run/user/1000/pulse`) y `PULSE_SERVER` | la vía que **funciona** para capturar: abrir la placa directo da error de frecuencia |
| `AXIOMA_RAFAEL_DIRECTO=1` | el demonio corre **sin systemd** (dentro del contenedor no existe `systemctl`) |
| `AXIOMA_INPUT_DEVICE` | si el nombre del dispositivo del equipo no existe adentro, se usa el del sistema |
| `./data/registros_voz:/app/logs` | sus registros van aparte, así no ensucian el informe de salud del proyecto |

**Medido**: con esa configuración el demonio arranca dentro del contenedor y **queda escuchando** la frase de
activación (`[WakeWord] Escuchando wake word`), sin errores en el lazo de escucha.

**Nota**: el widget de escritorio (que corre en tu equipo) se comunica con el demonio por archivos en `/tmp`;
si querés verlo desde el contenedor, hay que montar `/tmp` (está comentado en el compose, con la advertencia).

**Windows**: el servicio `voz` **no está disponible** ahí. Todo lo que necesita (`/dev/snd`, el socket de
PulseAudio, los grupos de audio del equipo) es de Linux, y el contenedor de Windows corre sobre WSL 2 con un
kernel que no expone esos dispositivos. El chat y los modelos funcionan igual; el lanzador de Windows lo
avisa al instalar.
