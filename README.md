# AXIOMA

**Asistente de inteligencia artificial local para tu PC.** Corre en tu propio equipo, con memoria entre
conversaciones, agentes especializados (código, investigación, análisis) y voz. Tus conversaciones y tus
claves **no salen de tu máquina**: los modelos se ejecutan en tu equipo con [Ollama](https://ollama.com).

> **Instalación guiada**: no hace falta saber programar. El instalador se abre con doble clic, y AXIOMA
> mismo te dice qué le falta la primera vez y lo instala con un clic.

---

## ¿Qué necesitás?

| | Mínimo | Recomendado |
|---|---|---|
| **Memoria (RAM)** | 9 GB (funciona con restricciones) | **16 GB** (bien) · **24-32 GB** (ideal) |
| **Disco libre** | ~15 GB para los modelos | 25 GB |
| **Sistema** | Linux, macOS o Windows con Docker | — |
| **Docker** | [Docker Desktop](https://www.docker.com/products/docker-desktop) | — |

Con menos de 16 GB **igual funciona**: AXIOMA te avisa de la restricción concreta. Con una placa de video
con memoria suficiente, el modelo más pesado se aloja en la placa y AXIOMA trabaja **en paralelo** en vez de
un modelo por vez. **No necesitás** placa dedicada, ni cuentas de pago, ni internet después de bajar los
modelos.

---

## Instalación (una sola vez)

1. **Instalá Docker** desde [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop)
   y abrilo una vez para que arranque.
2. **Descargá este proyecto**: botón verde **Code → Download ZIP** y descomprimilo.
3. **Ejecutá el instalador**: doble clic en `instalar/instalar_axioma.sh` (en Linux) o
   `./instalar/instalar_axioma.sh` desde una consola. Comprueba Docker, arma AXIOMA, deja un **acceso directo
   en tu menú** y lo enciende.
4. **Usalo**: cada vez, doble clic en el acceso directo: se enciende y **se abre solo en tu navegador**.

## El primer arranque

1. **Creá tu usuario** (no hay ninguno predefinido: el tuyo lo creás vos; cada uno tiene su memoria aparte).
2. Se abre sola la pantalla **🧩 Configurar AXIOMA**, que te dice punto por punto qué está bien, qué falta y
   **por qué**.
3. **Descargá los modelos** con un clic (con avance real y botón **DETENER**).
4. **Cargá tus claves** (opcional): la búsqueda web y las noticias necesitan una clave **tuya y gratuita**
   (por ejemplo de [tavily.com](https://tavily.com)); sin claves todo lo demás funciona igual. Se guardan
   **sólo en tu equipo** y nunca se muestran.

---

## Cómo se usa

| Quiero… | Qué hago |
|---|---|
| Conversar o pedir código | Escribo en el chat y envío |
| Ver si falta algo | Botón **🧩** del encabezado |
| Apagar AXIOMA | Doble clic en el lanzador otra vez |
| Ver qué pasa | `docker compose logs -f axioma` |
| Cambiar de usuario | *Avanzado* → 👤 |

**Voz (opcional)**: un servicio aparte escucha la frase *"hey rafael"*:
`docker compose --profile voz up -d voz` (encender) · `docker compose --profile voz stop voz` (apagar).

---

## Tus datos y tu privacidad

- **Los modelos corren en tu equipo**: nada de lo que conversás se envía a servicios externos.
- **Tus claves** quedan en un archivo de tu equipo con permisos **sólo para tu usuario**.
- **Tu memoria** vive en la carpeta `data/`: si la borrás, empezás de cero.
- Las únicas salidas a internet son bajar los modelos y las funciones que vos pidas.

## Problemas frecuentes

| Síntoma | Qué hacer |
|---|---|
| "Docker no responde" | Abrí Docker Desktop (Linux: `sudo systemctl start docker`) |
| "permiso denegado" con Docker | `sudo usermod -aG docker $USER` y volvé a entrar |
| Puerto 8080 ocupado | `AXIOMA_PUERTO=8090 ./instalar/iniciar_axioma.sh` |
| Un modelo no baja | Reintentá desde la pantalla 🧩 (lo bajado no se pierde) |

## Documentación

| Documento | Para qué |
|---|---|
| **`MANUAL_USUARIO.md`** | **Empezá por acá si es tu primera vez**: paso a paso con lo que vas a ver |
| `COMANDOS_AXIOMA.txt` | Todos los comandos (contenedor y desarrollo) |
| `MANUAL_AXIOMA.md`, `MANUAL_RAFAEL.md`, `README_TECNICO.md` | Manuales técnicos |
| `CHANGELOG.md` | Qué cambió en cada versión |

## Licencia

MIT — podés usarlo, modificarlo y compartirlo citando la autoría. Ver [`LICENSE`](LICENSE).
