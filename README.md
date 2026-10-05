# AXIOMA

**Asistente de inteligencia artificial local para tu PC.** Corre en tu propio equipo, con memoria entre
conversaciones, agentes especializados (código, investigación, análisis) y voz. Tus conversaciones y tus
claves **no salen de tu máquina**: los modelos se ejecutan en tu equipo con [Ollama](https://ollama.com).

> **Instalación guiada**: no hace falta saber programar. Hay un instalador que se abre con doble clic, y
> AXIOMA mismo te dice qué le falta la primera vez y lo instala con un clic.

---

## ¿Qué necesitás?

| | Mínimo | Recomendado |
|---|---|---|
| **Memoria (RAM)** | 9 GB (funciona con restricciones) | **16 GB** (bien) · **24-32 GB** (ideal) |
| **Disco libre** | ~15 GB para los modelos | 25 GB |
| **Sistema** | Linux, macOS o Windows con Docker | — |
| **Docker** | [Docker Desktop](https://www.docker.com/products/docker-desktop) | — |

Con menos de 16 GB **igual funciona**: AXIOMA te avisa de la restricción concreta (por ejemplo, que conviene
no tener otras aplicaciones pesadas abiertas mientras conversás). Con una placa de video con memoria
suficiente, el modelo más pesado se aloja en la placa y AXIOMA trabaja **en paralelo** en lugar de un modelo
por vez.

**Lo que NO necesitás**: ni tarjeta de video dedicada, ni cuentas de pago, ni internet después de descargar
los modelos.

---

## Instalación (una sola vez)

**1. Instalá Docker** desde [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop)
y abrilo una vez para que arranque. Es el programa que ejecuta AXIOMA.

**2. Descargá este proyecto**: botón verde **Code → Download ZIP**, y descomprimilo donde quieras.

**3. Ejecutá el instalador**:
- **Linux**: doble clic en `instalar/instalar_axioma.sh`.
- **Desde una consola** (cualquier sistema): `./instalar/instalar_axioma.sh`

El instalador comprueba Docker, **arma AXIOMA** (la primera vez tarda unos minutos), deja un **acceso directo
en tu menú de aplicaciones** y lo enciende.

**4. Usalo**: cada vez que quieras, doble clic en el acceso directo (o en `instalar/iniciar_axioma.sh`):
se enciende y **se abre solo en tu navegador**.

---

## El primer arranque, paso a paso

1. **Creá tu usuario.** No hay ningún usuario predefinido: **el tuyo lo creás vos** la primera vez. Cada
   usuario tiene su memoria separada.
2. **Mirá qué falta.** Se abre sola la pantalla **"Configurar AXIOMA"**, que revisa tu equipo y te dice punto
   por punto qué está bien, qué falta y **por qué** hace falta.
3. **Descargá los modelos** con un clic: el botón **⬇️ Descargar los que faltan** baja los obligatorios y te
   muestra el avance real; podés **detenerlo** cuando quieras.
4. **Cargá tus claves** (opcional). La búsqueda web y las noticias necesitan una clave **tuya y gratuita**
   (por ejemplo de [tavily.com](https://tavily.com)). Sin claves AXIOMA funciona igual: sólo pierde esas
   funciones. Se cargan desde la misma pantalla y quedan guardadas **sólo en tu equipo**.
5. **Listo**: ya podés conversar.

---

## Cómo se usa

| Quiero… | Qué hago |
|---|---|
| Conversar o pedir código | Escribo en el chat y envío |
| Ver si falta algo | Botón **🧩** del encabezado |
| Apagar AXIOMA | Doble clic en el lanzador otra vez, o `./instalar/iniciar_axioma.sh --detener` |
| Ver qué está pasando | `docker compose logs -f axioma` |
| Cambiar de usuario | *Avanzado* → 👤 |

**Funciones avanzadas** (detrás del botón *Avanzado*): Mi Hard (qué modelos entran en tu equipo), Estado,
Archivos, Exportar investigación, Sesiones y Configuración.

**Voz (opcional)**: un servicio aparte escucha la frase de activación *"hey rafael"*:
```bash
docker compose --profile voz up -d voz     # encender
docker compose --profile voz stop voz      # apagar
```

---

## Tus datos y tu privacidad

- **Los modelos corren en tu equipo**: nada de lo que conversás se envía a servicios externos.
- **Tus claves son tuyas**, quedan en un archivo de tu equipo con permisos **sólo para tu usuario** y no
  viajan dentro de la imagen.
- **Tu memoria** (conversaciones y recuerdos) vive en la carpeta `data/`, no dentro de la aplicación. Si
  borrás esa carpeta, empezás de cero.
- Las únicas salidas a internet son: bajar los modelos, y las funciones que vos pidas (búsqueda web,
  noticias, voz con internet).

---

## Problemas frecuentes

| Síntoma | Qué hacer |
|---|---|
| "Docker no responde" | Abrí Docker Desktop (Linux: `sudo systemctl start docker`) y reintentá |
| "permiso denegado" con Docker | `sudo usermod -aG docker $USER` y volvé a entrar en tu sesión |
| El puerto 8080 está ocupado | `AXIOMA_PUERTO=8090 ./instalar/iniciar_axioma.sh` |
| Dice que falta memoria | Con menos de 16 GB funciona con restricciones: AXIOMA te dice cuáles |
| Un modelo no baja | Reintentá desde la pantalla de configuración |
| La voz no escucha | Mirá `MANUAL_USUARIO.md`, sección de voz (permisos de audio del contenedor) |

---

## Documentación

| Documento | Para qué |
|---|---|
| **`MANUAL_USUARIO.md`** | **Empezá por acá si es tu primera vez**: paso a paso con lo que vas a ver |
| `COMANDOS_AXIOMA.txt` | Todos los comandos disponibles |
| `MANUAL_AXIOMA.md` y `README_TECNICO.md` | Manuales técnicos, para quien quiera entender o modificar el proyecto |
| `CHANGELOG.md` | Qué cambió en cada versión |

---

## Licencia

MIT — podés usarlo, modificarlo y compartirlo citando la autoría. Ver [`LICENSE`](LICENSE).

**Estado**: en desarrollo activo. Los problemas y las sugerencias son bienvenidos en la pestaña *Issues*.
