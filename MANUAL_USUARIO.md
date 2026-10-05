# Manual de uso — para quien nunca usó AXIOMA

Este manual está pensado para alguien que **no sabe programar**: no hace falta abrir una consola en ningún
momento. Vamos paso a paso y con lo que vas a ver en pantalla.

---

## 1. ¿Qué es AXIOMA?

Un asistente de inteligencia artificial que **funciona en tu propia computadora**. Le escribís como si fuera
un chat y te responde; además puede escribir y revisar código, buscar información y analizar datos.

Tres cosas importantes:

- **Funciona sin internet** (después de bajar los modelos).
- **Nada de lo que le contás sale de tu computadora.**
- **No se paga** y no hace falta ninguna cuenta.

---

## 2. Antes de empezar: dos comprobaciones

| Comprobación | Cómo saberlo | Si no lo cumplís |
|---|---|---|
| **Memoria (RAM)**: 9 GB mínimo, 16 GB recomendado | En Windows: Configuración → Sistema → Acerca de. En Linux: `free -h` | Con menos de 9 GB AXIOMA te lo dirá: no puede cargar el modelo de chat |
| **Espacio en disco**: unos 15 GB libres | En Windows: Explorador → Este equipo. En Linux: `df -h` | AXIOMA te dirá cuánto falta antes de descargar |

Y necesitás **Docker**, que es el programa que hace funcionar AXIOMA sin que tengas que instalar nada raro.

---

## 3. Paso 1: instalar Docker (10 minutos)

1. Entrá a **[docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop)**.
2. Descargá la versión para tu sistema (Windows, macOS o Linux) e instalala, aceptando lo que propone.
3. **Abrí Docker Desktop** (aparece como un icono con forma de ballena). La primera vez tarda un poco.
4. Esperá a que diga que está **en ejecución**. Dejalo abierto: cuando termines de usar AXIOMA podés
   cerrarlo.

> Si Docker no arranca o te pide reiniciar, hacelo: es normal la primera vez.

---

## 4. Paso 2: descargar AXIOMA

1. En esta página (el repositorio), tocá el botón verde **Code**.
2. Elegí **Download ZIP**.
3. Descomprimí el archivo donde quieras (por ejemplo, en el Escritorio). Se va a crear una carpeta llamada
   `proyecto-axioma`.

---

## 5. Paso 3: instalarlo (5 minutos, una sola vez)

Dentro de la carpeta `proyecto-axioma` hay una carpeta **`instalar`**. Ahí está el instalador.

- **Linux**: hacé **doble clic** en `instalar_axioma.sh` y elegí **Ejecutar**.
- **Windows o macOS**: por ahora hay que abrir una consola dentro de esa carpeta y escribir
  `./instalar/instalar_axioma.sh` (la consola es una ventana de texto; el comando se copia y se pega, y
  Enter).

Vas a ver cómo avanza:

```
▶ AXIOMA — instalación (una sola vez)
  ✅ Docker responde (Docker version 29…)
▶ Armando AXIOMA (la primera vez tarda unos minutos)
  ✅ AXIOMA armado
▶ Creando el acceso directo en tu menú de aplicaciones
  ✅ Acceso creado (aparece como «AXIOMA» en tu menú)
▶ Encendiendo AXIOMA
  ✅ AXIOMA está en línea: http://127.0.0.1:8080/
```

Y **se te abre solo el navegador**. Desde ahora, **para usar AXIOMA alcanza con hacer doble clic en el icono
«AXIOMA»** de tu menú de aplicaciones: se enciende y se abre solo.

---

## 6. Paso 4: la primera vez que entra

### 6.1 Creá tu usuario

AXIOMA **no trae ningún usuario**: el tuyo lo creás vos. Vas a ver una pantalla que dice
**"AXIOMA — Seleccioná tu usuario"** con un botón **➕ Usuario nuevo**.

Tocá ese botón y escribí un nombre (por ejemplo, tu nombre). Listo: **ese usuario es tuyo y tiene su propia
memoria**. Si otra persona usa AXIOMA en la misma computadora, que cree su usuario: cada uno tiene sus
conversaciones separadas.

### 6.2 AXIOMA te dice qué le falta

La primera vez se abre sola la pantalla **🧩 Configurar AXIOMA**. Arriba te saluda y abajo te muestra, punto
por punto, el estado de tu equipo:

```
✅ Intérprete de Python: 3.12.x con dependencias
✅ Motor de modelos (Ollama): responde
⚠️  Memoria RAM: 14,7 GB: AXIOMA funciona, con restricciones (16 GB es el nivel recomendado)
❌ Modelo de chat: no está descargado: qwen3:8b (4,87 GB en disco)
      → `ollama pull qwen3:8b` — sin esto AXIOMA no puede responder
```

Cada renglón te dice **qué significa** y **qué se pierde** si falta. Los ✅ están resueltos; los ⚠️ son
avisos (podés usarlo igual) y los ❌ son cosas que hay que resolver.

### 6.3 Descargá los modelos (10-20 minutos, según tu internet)

En esa misma pantalla, tocá **⬇️ Descargar los que faltan**. Vas a ver el avance real y un botón
**⏹️ DETENER** por si querés parar (podés continuar más tarde: lo que ya se bajó no se pierde).

Son varios GB: es normal que tarde. Cuando termina, el renglón pasa a ✅ y podés cerrar la pantalla.

### 6.4 Cargá tus claves (opcional)

Algunas funciones —**buscar en internet** y **noticias**— necesitan una clave **tuya y gratuita** que te da
el sitio del servicio (por ejemplo [tavily.com](https://tavily.com): te registrás y copiás la clave).

- **Si no cargás ninguna, AXIOMA funciona igual**: sólo no podrá buscar en internet.
- Se cargan en la misma pantalla (sección **🔑 Tus claves**): pegás la clave y tocás **Guardar claves**.
- Quedan guardadas **sólo en tu computadora** y **nunca se muestran** en pantalla.

---

## 7. Paso 5: a usarlo

Escribí en el chat y enviá. Algunas cosas para probar:

| Escribí algo así | Qué hace |
|---|---|
| `Hola, ¿qué podés hacer?` | Conversación |
| `Escribime una función en Python que ordene una lista` | Código |
| `Investigué sobre paneles solares y hacé un resumen con fuentes` | Investigación (necesita internet) |
| `Analizá este archivo` (adjuntándolo) | Análisis |

Mientras responde, podés **detenerlo** con el botón de parar. Arriba a la derecha está el botón **Avanzado**
con las funciones extra (Mi Hard, Estado, Archivos, Exportar, Sesiones, Configuración).

### La voz (opcional)

AXIOMA puede escuchar la frase **"hey rafael"** y responderte hablando. Necesita permisos de audio y se
enciende aparte:

```bash
docker compose --profile voz up -d voz     # encender la voz
docker compose --profile voz stop voz      # apagarla
```

---

## 8. Apagarlo y volver a usarlo

- **Apagar**: doble clic otra vez en el lanzador (o `./instalar/iniciar_axioma.sh --detener`).
- **Volver a usar**: doble clic en el icono «AXIOMA». Los modelos y tus conversaciones quedan guardados, no
  se bajan de nuevo.

---

## 9. Si algo no funciona

| Lo que ves | Qué hacer |
|---|---|
| "No tenés Docker instalado" | Instalá Docker Desktop (paso 1) y volvé a intentar |
| "Docker está instalado pero no responde" | Abrí Docker Desktop y esperá a que diga que está en ejecución |
| "No encontré Ollama… levanto el que viene con AXIOMA" | Es normal: AXIOMA se encarga solo |
| El navegador no se abre | Entrá a mano a **http://127.0.0.1:8080** |
| "El puerto 8080 está ocupado" | Cerrá el programa que lo usa, o usá otro puerto: `AXIOMA_PUERTO=8090 ./instalar/iniciar_axioma.sh` |
| Dicen que falta memoria | Cerrá otras aplicaciones pesadas; con 16 GB o más anda cómodo |
| La descarga de un modelo se cortó | Volvé a la pantalla 🧩 y tocá otra vez "Descargar los que faltan" |

**Tus cosas están a salvo**: las conversaciones y las claves quedan en la carpeta `data/` de AXIOMA, en tu
computadora. Si querés hacer una copia de seguridad, copiá esa carpeta.
