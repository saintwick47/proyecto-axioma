# DECISIONES — AXIOMA

Registro de decisiones del proyecto: **qué se decidió, por qué, qué se midió y qué error evitó**.

- Vive en el repositorio **privado** (`tools/preparar_publicacion.sh` no lo copia al público): habla de
  herramientas y del equipo del autor, no del producto.
- Se **agrega**, no se reescribe: si una decisión cambia, se anota la nueva con la fecha y se deja la
  anterior. La historia es lo que enseña.
- Las mismas decisiones, contadas para el usuario, van al `CHANGELOG.md` (ese sí se publica).

---

## 2026-10 · AXIOMA en contenedores (instalable en cualquier PC)

### D-01 · Contenedores con Docker, no Podman
**Por qué**: quien instala no tiene que saber nada de Python ni de entornos virtuales; y en Windows y
macOS hay una sola cosa que instalar (Docker Desktop). **Descartado**: Podman (menos rodado en Windows).
**Cómo se comprueba**: `tools/verificar_imagen.sh` (compila, mide y corre la suite adentro).

### D-02 · Ollama: soportar los dos caminos y elegir solo
**Decisión**: si el equipo ya tiene Ollama andando se usa **ese** (no gasta disco ni choca con lo que el
usuario ya usa); si no, el lanzador levanta el que viene con AXIOMA (perfil `ollama-local`, modelos en un
volumen). El usuario no elige nada.
**Por qué**: instalar un segundo Ollama sin preguntar sería pisar lo que ya funciona; no ofrecer ninguno
dejaría a alguien sin modelos.

### D-03 · Los modelos NO van dentro de la imagen
**Medido**: imagen de dos etapas, torch desde el índice CPU, **5,58 GB**; los modelos viven en volúmenes y
se piden desde la pantalla 🧩. La imagen también trae `docs/ cache/` y un `/app/.env` idéntico a
`.env.example` (claves vacías), y `/app/data` y `/app/logs` vacíos.
**Por qué**: una imagen con modelos adentro se re-descarga en cada actualización y no se puede compartir.

### D-04 · Ningún usuario cocido en el código
**Decisión**: la identidad por defecto queda **vacía** (`axioma_user_id = ""`) y el rol por defecto es
`user`; la primera vez AXIOMA **pide crear tu usuario**. Antes traía el usuario del autor.
**Por qué**: cada persona tiene su memoria aparte; lo del autor era una fuga de identidad en el público.
**Cuidado al tocarlo**: el `.env` del autor (no versionado) sigue con su usuario y rol: en su máquina no
cambia nada.

### D-05 · Memoria por niveles, sin «modo humilde»
**Medido**: menos de 9 GB no alcanza para el modelo de chat · 9-16 GB anda con restricciones · 16 GB anda
**bien** · 24-32 GB es lo ideal. El mínimo del producto es 16 GB.
**Por qué**: prometer un modo humilde que igual no deja conversar es engañar al usuario. Preferimos decir
la verdad en la pantalla 🧩 y en el preflight.

### D-06 · Trabajo en paralelo sólo con placa de video usable
**Decisión**: `decidir_modo_de_trabajo()` devuelve `"paralelo"` sólo si la placa es usable **y** el modelo
más pesado entra en su memoria **y** el otro entra en RAM con margen; en ese caso no se descarga el
modelo anterior al cambiar de tarea.
**Medido acá**: la iGPU de 2 GB **no** es usable → este equipo trabaja de a un modelo por vez. Forzar
`num_gpu` sin placa real está pendiente de una medición en un equipo con placa.

### D-07 · Voz dentro del contenedor: puente ALSA + socket de Pulse
**Medido**: `/dev/snd` solo no alcanza — abrir la placa directo da `Invalid sample rate [-9997]`. Con
`libasound2-plugins` + `libpulse0` + el socket `/run/user/1000/pulse` y `AXIOMA_INPUT_DEVICE=pulse` el
demonio **arranca y queda escuchando** (`[WakeWord] Escuchando wake word`). `AXIOMA_RAFAEL_DIRECTO=1`
porque adentro no existe `systemctl`. Los registros de la voz van a `./data/registros_voz` para no
ensuciar el informe de salud del proyecto.
**Error evitado**: los errores de la voz bloqueaban la compuerta de calidad al escribirse en `logs/`.

### D-08 · Qué se publica y qué no
**Decisión**: el repositorio público (`saintwick47/proyecto-axioma`) lleva **el producto y los manuales**.
Las herramientas técnicas del autor quedan en el privado.
**Analizado una por una** (2026-10-06): se comprobó si el **producto** (`src/`, `main.py`), la **suite**,
el **CI** o los **instaladores** las usan. Se sacaron 11 que nadie usa:
`analyze_tests_imports`, `corpus_benchmark`, `judge_grounding_calibration`, `measure_task_config`,
`model_tester`, `network_scanner`, `retrieval_eval`, `run_diagnostic_suite`, `structure_detector`,
`verify_axioma`, `vision_real_probe`. Y después el **documentador** (`tools/documentador/`, 1,1 MB, 19
archivos): es para documentar el proyecto del autor, no lo necesita quien instala.
**Cómo se comprobó que no hacía falta**: se corrió la **suite completa en la copia pública** (20/20, como
la corre el CI) y el CI quedó verde después de sacarlas.
**Lo único que quedó dependiendo del documentador**: una prueba de portabilidad que importa
`axioma_doc_base.py` para probar que la raíz del proyecto sale del archivo y no del directorio de trabajo.
Ahora **se saltea** cuando la carpeta no está (en el público), y sigue corriendo en el privado.

### D-09 · Windows: perfil `puente`, puerto fijo y sin voz
**Decisión**: en Windows se usa el servicio `axioma-puente` (perfil `puente`): publica el **8080** y llega
al Ollama del equipo por `host.docker.internal`, porque **`network_mode: host` no existe ahí**. La **voz no
está disponible** (el contenedor de Windows corre sobre WSL 2, que no expone `/dev/snd` ni el socket de
Pulse). El instalador deja acceso directo en el menú Inicio y en el Escritorio.
**Error medido y corregido**: `docker compose --profile puente up -d` levanta **además** el servicio
`axioma` (no tiene perfil, arranca siempre) → los dos pelean por el 8080 y `axioma` queda en **bucle de
reinicios**. Por eso el guion ahora **nombra el servicio**: `up -d axioma-puente` (y `ollama` si hace
falta). Hay una prueba que lo exige.
**Detalles que sí importan en Windows**: los `.ps1` van en UTF-8 **con BOM** (PowerShell 5.1 lee ANSI si
no y los acentos salen mal), **sin emoji** (la consola CP850 los imprime «?»: se usan `[OK] [!] [X]`), y
las llamadas a Docker van protegidas con `try/catch` porque en **PowerShell 7.4+** un comando que falla
lanza excepción y, con `$ErrorActionPreference = "Stop"`, el guion se cortaría.

### D-10 · Los instaladores de Windows NO están probados en Windows
**Estado**: escritos y cubiertos por 25 pruebas de **estructura** (BOM, sintaxis balanceada, perfiles y
servicios que existen, sin emoji, sin rutas del autor, protección 7.4+), y probado en Linux lo único
probable acá: el servicio `axioma-puente` respondió **HTTP 200** en `127.0.0.1:8080`.
**Lo que falta**: ejecutarlos en un Windows real. En este equipo no hay Windows ni PowerShell (`command -v
pwsh` vacío). **No se afirma que funcionen en Windows hasta probarlo.**

### D-11 · Las herramientas locales nunca se suben
**Decisión**: `tools/commit.py`, `tools/push.py` y `tools/publicar_cambios.py` **no se suben a ningún
repositorio** (pedido explícito del usuario).
**Cómo se garantiza** (tres barreras, para que no dependa de acordarse):
1. `BLOCKED_FILES` en `tools/commit.py` (bloqueo por nombre y por ruta);
2. `.git/info/exclude` (lista de ignorados **local**, no versionada: no aparece en ningún repo);
3. exclusiones de `tools/preparar_publicacion.sh`.
**Comprobado**: `git ls-files | grep publicar_cambios` da **0** en el privado y **0** en el público.

### D-12 · `requirements-ci.txt` es un subconjunto exacto, con invariante probado
**Medido (2026-10-06)**: `ruff` estaba **sólo** en el archivo del CI y `psutil` (que el producto usa en 5
módulos para medir memoria) **sólo** en el completo → el CI corría por el camino de respaldo y no probaba
el real. Se corrigió: `ruff` se declaró en los dos, `psutil` se agregó al del CI, y el encabezado ahora
dice **qué omite el CI y por qué** (`torch`, `torchaudio`, `sentence-transformers` se instalan aparte con
el índice CPU; `pyatspi` necesita las libs de accesibilidad del escritorio; `openwakeword` es opcional con
respaldo).
**Prueba que lo fija**: `test_requirements_ci_es_requirements_menos_lo_documentado` (verificada con tres
mutaciones: sacar un paquete del CI, agregar uno sólo al CI, cambiar una versión sólo en el CI).

### D-13 · La publicación se prepara con un guion, no a mano
**Decisión**: `tools/preparar_publicacion.sh` arma la copia pública (rsync con exclusiones), saca los
rastros del autor, sanea la documentación y **verifica** que no quede nada personal. Conserva `.git` de la
copia publicada (borrarlo la dejaba sin poder subirse: medido) y convierte `README_PUBLICO.md` en
`README.md` y el `README.md` técnico en `README_TECNICO.md`.
**Secciones privadas**: van entre marcas `SOLO PRIVADO: inicio` / `SOLO PRIVADO: fin` y se saca el bloque
**entero**. Antes se sacaban líneas sueltas por nombre y quedó en el público un **fragmento colgado** (el
encabezado y una lista del 1 al 7 sin el punto 5). Ahora, si una marca queda sin cerrar, el guion **corta**,
y al final verifica que no queden ni marcas ni menciones a herramientas que no se publican.
**Atajo para el uso diario**: `tools/publicar_cambios.py` hace el flujo completo (ver D-14).

### D-14 · Una sola orden para publicar: `tools/publicar_cambios.py`
**Por qué**: publicar a mano son 7 pasos y es fácil olvidarse de compilar o de mirar el CI.
**Qué hace**: (1) mira qué cambió de verdad y compara **funciones y clases** con el commit anterior; (2) si
no hubo cambios reales, se detiene; (3) **compila los contenedores antes de commitear** y comprueba que la
imagen levante; (4) documenta el cambio en `data/registros_publicacion/`; (5) commitea y sube lo privado
(`commit.py` + `push.py`); (6) arma y sube la copia pública; (7) espera al CI y avisa **VERDE o ROJO**
(rojo = sale con error).
**Es local** (D-11) y está documentado en `COMANDOS_AXIOMA.txt`, en su sección privada.

### D-15 · macOS: se queda como está, documentado
**Decisión**: en macOS el instalador se corre desde una consola (`./instalar/instalar_axioma.sh`), igual
que hoy. **Por qué**: no hay un lanzador de doble clic estándar comparable al `.desktop` de Linux o al
`.lnk` de Windows; lo más parecido es un `.command` (que además abre una ventana de Terminal y arrastra
permisos de ejecución que hay que fijar a mano). No hay ninguna Mac a mano para probarlo, y prometer un
instalador de macOS **sin haberlo probado** sería el mismo error que evitamos con Windows (D-10).
**Cuándo se revisa**: cuando haya una Mac para medir (o si alguien lo pide).

### D-16 · Cómo se recomienda un modelo por hardware (hecho el 2026-10-06)
**Objetivo**: que alguien que no sabe nada de modelos reciba una respuesta clara: «con tu equipo, usá
estos; este otro no te va a andar, y por esto».
**Lo que ya existe** (no hay que rehacerlo): el catálogo por rol con RAM y disco medidos
(`config/model_catalog.yaml`), los niveles de memoria (9 / 16 / 24-32 GB), `preflight --instalar` y la
comprobación **«¿Este modelo va a andar en mi PC?»** de la pantalla 🧩, que además sabe estimar un modelo
**que no está en el catálogo** (por el tamaño del nombre, con su aviso).
**Lo que falta**: un flujo `preflight --recomendar` que, con el hardware medido, diga para cada rol qué
modelo **conviene** y por qué (entra con margen / entra justo / no entra), y que se pueda consultar por un
modelo puntual que no esté en el catálogo.
**Cómo se probó**: con **hardware simulado** (32 GB, 16 GB, por debajo del mínimo), cuatro pruebas en
`tests/test_axioma_27_portabilidad.py`, cada una verificada con una mutación (olvidar un rol, volver a la
estimación por encima del mínimo medido, dejar el texto crudo del catálogo, y quitar la orden).
**Detalle que salió de la medición**: la cuenta por nombre de parámetros puede decir «entra justo» en un
equipo que el proyecto ya midió como insuficiente; manda el **mínimo medido** (9 GB) — es la decisión D-05.

**Defecto de documentación que apareció al construirla**: el texto del modelo de respaldo estaba escrito
como concatenación de Python (`para: ("…" "…")`), que en YAML **no existe**: la pantalla mostraba los
paréntesis y las comillas. Corregido con `>-` y con una prueba que revisa **todos** los textos del
catálogo. Lección: el texto que ve el usuario también es código, y hay que mirarlo.`

### D-17 · Cómo se comprueba que los instaladores funcionan (en tres niveles)
**Pregunta del usuario (2026-10-06): «¿cómo pruebo si realmente funcionan los instaladores?».** La
respuesta honesta es que se prueba en tres niveles, y cada nivel cubre algo que el anterior no:

**1. Automático en cada push** (nadie tiene que acordarse de nada):
- **Los guiones, ejecutados de verdad**: trabajos `instaladores-linux` (Ubuntu, en modo ensayo) e
  `instaladores-windows` (Windows, con PowerShell 5.1 y 7). No leen el archivo: lo **corren**.
- **Estructura** (rápido, en cualquier equipo): `tests/test_axioma_27_portabilidad.py` — BOM, sintaxis
  balanceada, perfiles y servicios que existen, sin emoji, sin rutas del autor, protección 7.4+.
- **Windows de verdad**: el trabajo `instaladores-windows` del CI corre
  `tests/probar_instaladores_windows.ps1` en un runner `windows-latest`, **con PowerShell 5.1 y con
  PowerShell 7**: ejecuta los dos guiones (como lo hace el acceso directo), comprueba que arranquen, que
  los acentos se lean y que sin Docker expliquen qué falta **y salgan con 4** en vez de quedarse mudos.
- **Linux de verdad**: `tools/verificar_imagen.sh` compila la imagen, **mide** su tamaño y corre la suite
  completa **dentro** de la imagen (una imagen que no pasa sus pruebas no se publica).

**2. En esta máquina, a mano y sin romper nada** (0 minutos, sin riesgo):
- `./instalar/iniciar_axioma.sh --dry-run` → muestra qué haría (Docker, decisión del Ollama, perfiles).
- `./instalar/instalar_axioma.sh --dry-run` → ídem para la instalación.
- Copia limpia: `rsync` de la carpeta sin `venv/ data/ logs/ .env` a un temporal y correr ahí el
  instalador en ensayo; así se prueba el camino de «máquina recién bajada» sin tocar la instalación que
  ya funciona.
- `python tools/verificar_primer_arranque.py` → abre el navegador y comprueba la guía del primer arranque.

**3. A mano, en un Windows real** (es lo que **no** se puede automatizar: Docker Desktop, los accesos
directos y el navegador). Lista mínima, 10 minutos:
1. Docker Desktop instalado, abierto y con «Engine running» (si falta WSL 2, `wsl --install` y reiniciar).
2. Clic derecho en `instalar\instalar_axioma.ps1` → **Ejecutar con PowerShell**.
3. Tiene que decir **«Docker responde»**, después **«Armando AXIOMA»** (la primera vez tarda minutos) y
   crear **«AXIOMA»** en el menú Inicio **y** en el Escritorio.
4. Al terminar se abre solo el navegador en `http://127.0.0.1:8080`; crear el usuario y seguir la guía.
5. **Doble clic en el acceso directo del Escritorio**: tiene que encender AXIOMA y abrir el navegador
   (esto prueba el `.lnk`, que es lo que más se rompe).
6. Cerrar y volver a hacer doble clic: los modelos **no** se vuelven a bajar.
7. `powershell -NoProfile -ExecutionPolicy Bypass -File .\instalar\iniciar_axioma.ps1 -Detener` → apaga
   todo y avisa que los datos quedan guardados.
8. Si algo falla, mirar `docker compose logs --tail 40 axioma-puente` (el servicio de Windows).
**Sin una PC con Windows**: el runner del CI ya cubre lo automatizable; lo interactivo (3, 4, 5) necesita
una máquina real —una PC prestada, o una máquina virtual con Windows (QEMU/KVM, ~30 GB y una hora de
puesta a punto)—. **Hasta que eso pase, no se afirma que funcionen en Windows** (D-10).

### D-21 · Seguridad de la interfaz: local por defecto, sesiones con clave propia (fase 1 del plan)
**Medido el 2026-10-07** (`ss -ltn`): la interfaz escuchaba en **`0.0.0.0:8080` sin autenticación**, con
`network_mode: host`; y la clave de firma de sesiones era la constante
`axioma_secret_key_change_in_production`, escrita a mano en `app.py` y `server.py`. Con las dos cosas
juntas, cualquiera en la misma red podía abrir AXIOMA y firmarse una sesión propia.
**Decisión**: (a) el servicio con red del equipo arranca con `--host ${AXIOMA_WEB_HOST:-127.0.0.1}` y el
perfil `puente` publica `127.0.0.1:8080:8080` (adentro del contenedor sigue siendo `0.0.0.0`, que es lo
que Docker exige para publicar un puerto); (b) clave de sesión **propia de cada equipo**, generada la
primera vez en `data/.storage_secret` (0600) o tomada de `AXIOMA_STORAGE_SECRET`.
**Lo que NO se hizo a propósito**: un sistema de login. Es una función grande y es **decisión del
usuario** (ver la tabla de decisiones abiertas de esa etapa; el documento que la contenía,
`docs/PLAN_MEJORAS_2026-10-07.md`, **fue eliminado en la Fase 3**; sus decisiones abiertas quedaron
absorbidas en las D-21 a D-26 de este archivo). Mientras no exista,
exponer la interfaz a la red es una decisión explícita y consciente.
**Comprobado**: `ss -ltn` → `127.0.0.1:8080` (ya no `0.0.0.0`) con la app respondiendo 200; el archivo
del secreto con `600` y 64 caracteres. Seis pruebas nuevas en `tests/test_axioma_29_seguridad_web.py`, y
las cinco mutaciones (volver la constante, sacar los permisos, publicar en todas las interfaces, volver a
`0.0.0.0`, ignorar la variable de entorno) **fallan** como corresponde.

### D-22 · Una sola versión de Python: 3.14 (imagen, desarrollo y CI)
**Decisión del usuario (2026-10-07)**: *«py3.12 no es importante, descartalo; corregirlo es una pérdida de
tiempo ya que lo vamos a migrar a py3.14»*.
**Qué se hizo**: `Dockerfile` → `ARG PYTHON_VERSION=3.14`; la matriz del CI queda en `['3.14']` y se quita
el `continue-on-error` (ya no hay segunda versión que comparar); `requirements-ci.txt` y el README dejan
de hablar de dos versiones.
**Por qué**: la imagen era 3.12 y el desarrollo 3.14 → dos entornos distintos, que es donde aparecen las
sorpresas. Y el job de 3.14 corría con `continue-on-error`, o sea que un rojo ahí **no se veía**.
**Evidencia a favor** (antes de migrar): el entorno local (3.14.7) corre la suite completa; el job 3.14 del
CI estaba en verde; y el único fallo de esos días apareció **en 3.12** (una prueba frágil que exigía
Docker: `test_el_acceso_directo_no_abre_la_terminal`, ahora tolerante, porque también tiene que pasar
**dentro** de la imagen, donde no hay Docker).
**Cómo se mide**: `tools/verificar_imagen.sh` (compila en 3.14, **mide el tamaño** y corre la suite
adentro). El plan pedía no crecer más de 200 MB.
**Aviso del usuario que acompañó la decisión**: actualizó `pandas` 3.0.3 → **3.0.5** y `numpy`
2.4.4 → **2.5.3** con `uv`, por un aviso de cuelgues de pandas 3.0.4 en 3.14. Ese aviso no se pudo
verificar acá (no tenemos esa versión), pero la subida **sí** se verificó: los dos archivos de
dependencias declaran las versiones nuevas y la suite completa pasa con ellas.

### D-23 · Banderas del contenedor: una se implementa, la otra se elimina (fase 2 del plan)
**Medido el 2026-10-07**: `AXIOMA_SIN_AUDIO` (declarada para los dos servicios sin audio) y
`AXIOMA_EN_CONTENEDOR` (declarada en el `Dockerfile`) **no las leía ningún módulo**. El comentario del
compose decía «la voz se desactiva sola» y era **falso**: el usuario veía «no se ven dispositivos de
audio» y pensaba que era su equipo.
**Decisión**: **una idea, una bandera**.
- `AXIOMA_SIN_AUDIO` → **se implementa** con un lector único
  (`config/multimodal_loader.audio_deshabilitado`). Efecto real: el preflight **no sondea** dispositivos y
  dice «desactivada a propósito»; la interfaz no ofrece voz (`voice_runtime.voz_posible`); el daemon de
  Rafael avisa en castellano y sale con 4.
- `AXIOMA_EN_CONTENEDOR` → **se elimina** del `Dockerfile`: lo que el contenedor necesita ya se pasa
  explícito (`--no-browser` en el `CMD`, `AXIOMA_RAFAEL_DIRECTO`, `AXIOMA_SIN_AUDIO`, rutas de logs).
  Mantener una bandera que nadie lee es deuda, no documentación.
**Medido**: con la bandera → `⚠️ Audio (voz): desactivada a propósito (AXIOMA_SIN_AUDIO=1)`; sin ella →
`✅ Audio (voz): 15 dispositivos de audio`. Cinco pruebas nuevas en
`tests/test_axioma_30_audio_opcional.py` y **cuatro mutaciones** que fallan como corresponde.

### D-24 · El audio se configura con los datos del equipo, no con números fijos (fase 3 del plan)
**Medido el 2026-10-07**: el compose tenía `group_add: ${AXIOMA_AUDIO_GID:-996}` pero **nadie calculaba**
esa variable, y el socket estaba fijo en `/run/user/1000/pulse`. El grupo `audio` **no es el mismo** en
todas las distribuciones (Debian/Ubuntu **29**, Fedora **63**, Arch/CachyOS **996**) y el socket lleva el
UID: con esos valores fijos, en otra PC la voz no ve los dispositivos y **el motivo no se entiende**.
**Decisión**: el **lanzador** y el **instalador** calculan los tres valores del equipo
(`id -u`, `id -g`, `getent group audio`) y los exportan para `docker compose`; el compose los toma con
`${AXIOMA_...}` y deja los números viejos sólo como último recurso. Los tres valores se **muestran** en el
plan del modo ensayo, así se ven antes de tocar nada.
**Por qué no un archivo compartido**: son dos guiones sueltos que se tienen que poder leer y ejecutar
solos (R14: no crear archivos cuando alcanza con lo que hay); el bloque está duplicado y **dicho** en los
dos, con el comentario que explica por qué.
**Medido**: en este equipo el ensayo dice `UID 1000 · GID 1000 · grupo audio 996`; con un grupo falso de
29 (Debian) el lanzador dice 29. En el compose renderizado, con UID 1001 y grupo 29, sale
`PULSE_SERVER: unix:/run/user/1001/pulse/native` y `group_add: "29"`. Tres mutaciones fallan como
corresponde (volver al valor fijo, volver al UID 1000, dejar de calcular el UID).

---

## Cómo se retoma el trabajo (si pasaron días)

El estado del proyecto se comprueba y se publica con estas cuatro órdenes, en este orden:

```bash
venv/bin/python tools/run_all_tests.py --timeout 150                 # suite completa
venv/bin/python axioma_auditor.py --format json -o logs/audit_summary.json
venv/bin/python tools/quality_gate.py --no-require-health --json logs/gate.json
venv/bin/python tools/publicar_cambios.py    # compila los contenedores, commitea, publica y espera el CI
```

El plan de trabajo de esa etapa (histórico) vivía en `docs/PLAN_MEJORAS_2026-10-07.md` («PUNTO DE
CONTINUACIÓN»); ese documento **fue eliminado en la Fase 3** (cumplido, absorbido en las D-21 a D-26) y
las decisiones y los errores propios quedan en este archivo. Lo primero que quedó pendiente es
la **fase 5** (permisos de `data/` y `logs/` con el UID del equipo) y después la **fase 6** (fakes de
Ollama en el CI).

### D-25 · El contenedor corre con el UID del equipo (fase 5 del plan)
**El problema (medido el 2026-10-08)**: el contenedor usaba siempre el usuario `1000` de la imagen. En un
equipo donde tu usuario tiene otro UID, lo que AXIOMA escribe en `data/` y `logs/` queda de un usuario que
**no existe** en tu máquina: no lo podés leer ni borrar. En este equipo no se notaba porque tu UID es
1000 (por eso el archivo de `data/` figura como `saintwick`).
**Decisión**: los tres servicios de la aplicación (`axioma`, `axioma-puente`, `voz`) corren con
`user: "${AXIOMA_UID:-1000}:${AXIOMA_GID:-1000}"`, con los valores que ya calculan el lanzador y el
instalador (fase 3). Al Ollama propio **no** se le cambia el usuario: es una imagen ajena.
**Lo que apareció al hacerlo** (y es la parte interesante): con `docker run --user 1001` el contenedor
**no podía escribir en ninguna carpeta** (`/app/data`, `/app/logs`, `/app/cache`) **ni leer el código**
(`import config.settings` explotaba con `PermissionError` sobre `src/utils/degradation.py`). La causa:
**1622 archivos del proyecto estaban en modo 600** (los escribe así la herramienta del autor) y al
copiarse a la imagen quedaban ilegibles para cualquier otro UID. Arreglado en los dos lados: la imagen
abre las carpetas de datos (`chmod -R 0777 data logs cache`), garantiza que el código sea legible
(`chmod -R a+rX /app`) y no escribe bytecode en el árbol (`PYTHONDONTWRITEBYTECODE=1`); y el árbol de
trabajo quedó en modo legible (de 1622 archivos a 0 en el código).
**Medido**: con UID 1001 la configuración se importa y las tres carpetas aceptan escritura ✅; en el
contenedor real, un archivo escrito en `logs/` figura como `saintwick` ✅. Prueba nueva que **impide** que
un archivo en modo 600 llegue a la imagen (era invisible hasta que se probó con otro UID), más dos
pruebas del compose y del `Dockerfile`; **tres mutaciones** fallan como corresponde.
**Lección**: arreglar la propiedad de los archivos destapó un problema de **lectura** que no se veía con
el UID de siempre. Un guardián temprano (¿es legible?) vale más que el arreglo.

### D-26 · Ollama falso para el CI, sin dependencias nuevas (fase 6 del plan)
**El problema**: en el CI `OLLAMA_HOST` apunta a un puerto muerto (`http://127.0.0.1:1`) para no depender
de la red (regla R5), así que las pruebas que necesitan el servidor **se saltan** (medido:
`test_axioma_02_core.py::restore_default()` y dos de visión) y el camino real del cliente nunca se
ejercita.
**Decisión**: en vez de agregar `respx` (lo que proponía el plan), un **servidor HTTP real de la
biblioteca estándar** que imita a Ollama, en `tests/conftest.py` como *fixture* (`ollama_falso` y
`ollama_falso_que_falla`). Ventajas: **cero dependencias nuevas**, el código del proyecto hace pedidos
HTTP reales (mejor prueba que reemplazar el cliente por un doble) y escucha en un puerto libre que elige
el sistema, así funciona igual en el CI y en un equipo con Ollama de verdad.
**Medido al usarlo**: el cuerpo de la descarga es `{"name": …, "stream": true}` (no `model`), y el
cliente **consulta la salud del servidor configurado** (`OLLAMA_HOST`), no la del `base_url`: en un
equipo sin Ollama la conexión se rechaza antes de llegar al falso. Eso obligó a que la prueba del camino
de error mire **el comportamiento** (el falso contesta 500 y el cliente falla claro) y no *cuántos
pedidos llegaron* —contar pedidos daba un fallo intermitente por el cortacircuitos y por la salud—.
**Pruebas**: cinco en `tests/test_axioma_31_ollama_falso.py` y **tres mutaciones** que fallan como
corresponde (el falso contesta otra cosa, la descarga no termina, el cliente deja de pedir en modo flujo
—esta última es del código del proyecto, no de la prueba—).

---

### D-27 · Voz en Windows: demonio nativo que llama al contenedor (decisión del usuario, 2026-10-08)
**Decisión** (la recomendación que el usuario aprobó): en Windows la voz va en un **demonio nativo**
(`instalar/rafael_windows.ps1`), no dentro del contenedor. Escucha y habla con la voz que **ya trae
Windows** (`System.Speech`) y le manda el texto al AXIOMA del contenedor por HTTP.
**Por qué no dentro del contenedor**: Docker Desktop en Windows corre sobre **WSL 2**, que no expone el
micrófono ni el servidor de sonido (lo mismo ya medido para el servicio `voz`: necesita `/dev/snd` y el
socket de Pulse). La alternativa sería pedirle al usuario que instale **Python** en Windows, justo lo que
el proyecto evita.
**Corrección medida antes de escribir el guion**: la ruta del endpoint **no** es `/api/v1/chat` como decía
la recomendación original. Medido contra el AXIOMA andando: `POST /api/v1/chat` -> **404**, y el
`openapi.json` del servidor muestra las rutas montadas bajo `/api`, así que queda **duplicada**:
**`/api/api/v1/chat`** -> **200** (contesta de verdad: `{"success":true,"content":"Hola."}`). El guion
prueba **las dos** rutas, así seguirá funcionando cuando se arregle el prefijo.
**Defecto abierto, anotado**: el prefijo duplicado es una trampa para quien use la API desde afuera; el
arreglo es montar el router sin duplicar (`/api/v1/...`), pero cambia una dirección pública y se hace con
cuidado (no se tocó en esta pasada).
**Cómo se verifica**: el trabajo `instaladores-windows` del CI **ejecuta el guion de verdad** en un
Windows real (con `-Ensayo`), y hay una prueba de estructura que exige BOM, ausencia de dependencias de
Python, las dos rutas y el modo ensayo. **Medido en el CI**: la primera versión de esa comprobación
miraba la *salida* del guion en vez del *archivo* y hacía fallar el trabajo sin motivo — corregido.
**Límite conocido**: para **dictar** en castellano hace falta el paquete de idioma de Windows; si no está,
el guion avisa y sigue en modo texto.

### D-28 · La dirección pública de la API estaba duplicada (arreglada el 2026-10-08)
**Cómo se confirmó que la duplicación era REAL** (pedido explícito del usuario: «asegurate siempre que sea
real»): no alcanzaba con ver el código. Se midió contra el servidor ANDANDO: `/api/v1/chat` -> **404** y
`/api/api/v1/chat` -> **200**; el propio `openapi.json` del servidor mostraba las rutas del router; y el
código dice las dos mitades de la duplicación: `APIRouter(prefix="/api/v1")` en `routes.py` **y**
`nicegui_app.mount('/api', _fastapi_app)` en `server.py`. Además se buscó **quién llama** a esas rutas:
nadie usaba la duplicada —los textos, el README y el demonio de Windows dicen `/api/v1/...`—, y
`COMANDOS_AXIOMA.txt` documentaba la duplicada (lo que probaba que era un accidente, no un diseño).
**El arreglo**: el prefijo del router pasa a `/v1` (el `/api` lo pone el montaje), con lo que la dirección
pública queda como se quería: **`/api/v1/...`**.
**El defecto de fondo que apareció**: la **auto-comprobación del arranque no lo detectaba** porque
comparaba contra las rutas del *router* (`/api/v1/health` existe siempre) en vez de la dirección pública.
Ahora compara contra `montaje + prefijo` — así esta familia de errores se ve al arrancar.
**Medido después del arreglo**: `/api/v1/chat` contesta de verdad (`{"success":true,"content":"¡Hola! …"}`),
`/api/v1/health` -> 200, `/api/api/v1/chat` -> 404 y la interfaz sigue en 200. Y **desde dentro del
contenedor**: `/api/v1/chat` -> 200 (el demonio de Windows acierta la primera ruta que prueba).
**Pruebas**: una prueba nueva que prohíbe la duplicación y exige que la comprobación del arranque mire la
dirección pública (con **dos mutaciones** que la hacen fallar: volver el prefijo duplicado y volver a
comparar contra el router), y se corrigieron `test_axioma_10`, `test_axioma_22`, `test_axioma_23` y
`test_axioma_28`, que miraban las rutas internas.

**¿Y si la duplicación era a propósito?** El usuario no lo recuerda (2026-10-08). Queda dicho acá cómo
revertir y cómo darse cuenta de que hace falta:

- **Síntomas que justificarían revertir**: algo que ya funcionaba (una integración propia, una herramienta
  tuya, un acceso directo, un script viejo) empieza a dar **404** justo después de este cambio y usaba
  `/api/api/v1/...`.
- **Cómo saber qué está pasando** (10 segundos): mirar en los registros qué dirección pidió el cliente
  (`logs/`, sección de la API) o probar las dos a mano —
  `curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8080/api/v1/chat -d '{"message":"hola"}' -H 'Content-Type: application/json'`
  (200 = la nueva ✅ · 404 = la vieja tenía razón) y lo mismo con `/api/api/v1/chat`.
- **Pasos para revertir** (dos ediciones, 5 minutos):
  1. `src/interfaces/web/routes.py`: volver `router = APIRouter(prefix="/v1", …)` a `prefix="/api/v1"`.
  2. `tests/test_axioma_29_seguridad_web.py`: la prueba
     `test_la_direccion_publica_de_la_api_no_se_duplica` **codifica esta decisión a propósito** (exige
     `/v1` y que la comprobación del arranque mire la dirección pública): al revertir hay que darla vuelta
     —o borrarla— y dejarlo escrito acá como decisión nueva, con el motivo.
  3. `Aviso`: **el demonio de Windows no se toca** (prueba las dos rutas: la nueva primero y la vieja como
     respaldo), y `COMANDOS_AXIOMA.txt` sí: las tres líneas que apuntan a `/api/v1/...`.
- **Comprobar que la reversión quedó bien**: `/api/api/v1/chat` → 200, `/api/v1/chat` → 404, la interfaz
  `/` → 200 y la suite completa en verde.
- **Alternativa menos drástica** (si hay que sostener las dos direcciones): montar el router **dos veces**
  (`/api` y `/`), en vez de cambiar el prefijo. Es más código, pero no rompe a nadie; se decide el día
  que aparezca el problema, no antes.

### D-29 · Guion de prueba en máquina limpia (hecho el 2026-10-08) — y lo que encontró
**Pedido del usuario**: probar la instalación en una copia limpia (sin `venv`, `data/`, `logs/`, `.env`),
con medición y **sin tocar la instalación que ya funciona**.
**Hecho**: `tools/probar_instalacion_limpia.sh`, con tres niveles (`--solo-plan`, por defecto rápido, y
`--con-imagen`/`--encender`). Cómo garantiza que no toca nada: trabaja en una **carpeta temporal** (y la
borra), usa **otra imagen** (`axioma-limpia:prueba`), **otro contenedor** (`axioma-prueba`) y **otro
puerto** (8099), y toma una **foto de la instalación del usuario antes y después** para compararla y
fallar si cambió.
**Medido (ruta rápida, 30 s, verde)**: copia limpia ✅, los dos instaladores explican su plan en ensayo ✅,
el lanzador decide el Ollama y dice a dónde abriría ✅, `docker compose config` válida ✅, y la instalación
del usuario **idéntica** (mismo Id de contenedor, mismo estado `healthy`, mismo `127.0.0.1:8080`, mismo
HTTP 200) ✅.
**Medido (ruta completa, `--encender`)**: encontró **dos brechas reales de la máquina limpia**, todavía
**abiertas** (no se arreglaron en esta pasada):
1. **La suite falla dentro de la imagen armada desde una copia limpia**: 4 pruebas en 3 archivos
   (`test_axioma_27`, `test_axioma_28`, `test_axioma_30`). La copia no tiene `docs/` (está fuera de git a
   propósito) y varias pruebas lo esperan: hay que hacer que se salteen cuando falta, como ya se hizo con
   los archivos de empaquetado.
2. **La copia encendida no respondió en un minuto**: el contenedor arranca, pero el arranque en frío
   necesita bajar el modelo de embeddings (`bge-m3`) porque la caché vive en `data/`/`cache/` del
   proyecto y la copia no los tiene. En una máquina limpia eso significa **internet y minutos de espera**:
   hay que documentarlo en el manual del primer arranque (y, si se puede, avisar en pantalla).
**Lo bueno**: la instalación del usuario **quedó igual** también en la ruta completa ✅, y el propio guion
**cazó sus hallazgos** en vez de decir «todo bien».

### D-30 · Máquina limpia: la suite adentro ya pasa y el arranque en frío, explicado (2026-10-08)
**Lo que confirmó la corrida completa del usuario** (`tools/probar_instalacion_limpia.sh --encender`, 5m 9s):
la imagen de la copia limpia se arma (**5,59 GB**, Python **3.14.8**) y **la suite pasa ADENTRO: 23/23
archivos OK** ✅ — la brecha 1 quedó cerrada de verdad (midiéndola con la compilación real, no con el
proyecto montado: de ese error mío salió la lección de abajo). Y su instalación quedó **idéntica** ✅.
**El único fallo que queda es el arranque en frío, y no es un defecto**: en una copia **sin caché**, el
primer arranque **baja el modelo de la memoria** (`BAAI/bge-m3`, medido: **4,3 GB**) y eso son minutos; el
guion esperaba **60 segundos** y avisaba un fallo que no lo era.
**Arreglado de raíz (D-29) y ahora también en el guion**:
- La caché de HuggingFace vive en `cache/` (volumen del equipo) y `HF_HOME` apunta ahí: se baja **una vez**.
- El guion espera **5 minutos** y va **contando lo que dice el contenedor**: si ve descargas, dice
  «está bajando el modelo de la memoria (4,3 GB), es la primera vez **no es un fallo**»; si no, informa el
  fallo real con las últimas líneas.
- Opción nueva `--con-cache`: monta la caché de modelos del equipo en la copia, para probar **el arranque**
  sin esperar la descarga.
**Bonus medido**: la caché de este equipo ya tiene el modelo, así que el proyecto se sembró con **enlaces
duros** (`cp -al`) → el contenedor lo encuentra sin bajar nada y **sin ocupar disco extra** (mismo sistema
de archivos). Reversible: `rm -rf cache/huggingface`.

### D-31 · El guion de máquina limpia encontró un BLOQUEANTE de instalación nueva (arreglado)
**Lo encontró el guion** (`probar_instalacion_limpia.sh --encender --con-cache`), y **es un defecto que yo
mismo introduje** en la fase 5: al hacer que el contenedor corra con el UID del equipo, en una instalación
**nueva** las carpetas `data/` y `logs/` **no existen**; Docker las crea **como `root`** al montarlas, y el
contenedor (que corre con TU UID) no puede escribir en ellas.
**Medido**: la copia limpia encendía y moría con `PermissionError: [Errno 13] /app/data/memory` — o sea,
**en una máquina nueva AXIOMA no arrancaba** (en este equipo no se notaba porque `data/` y `logs/` ya
existen y son del usuario).
**Arreglo**: los dos guiones (`instalar_axioma.sh` y `iniciar_axioma.sh`) crean `data/`, `logs/` y `cache/`
**antes** de encender, así quedan del usuario y el contenedor puede escribir. Con eso, la copia limpia
arranca y **responde HTTP 200** (medido), con `data/memory` del usuario ✅. Prueba nueva que lo exige.
**Ojo con lo que sigue**: esto corrige el arranque, no la demora de la primera vez (el modelo de la
memoria son 4,3 GB, D-29). Y queda pendiente terminar en el guion la opción `--con-cache` (acepta el
interruptor pero el montaje de la caché todavía no está implementado: mi parche no coincidió dos veces).

### D-32 · Revisión de la SEGUNDA respuesta de Qwen (2026-10-09): qué es cierto y qué no
El usuario pidió analizar si lo que dice esa respuesta **es realmente así**. Se midió cada punto contra el
código (no se tocó nada por creerle a la respuesta).

| # | Lo que dice | Medición | Veredicto |
|---|---|---|---|
| — | «273 archivos Python, 121K LOC, 48 TaskTypes, 28 archivos de test, v0.6.9» | `src/` tiene **203** `.py` (todo el proyecto: 385) · **23** archivos de test · TaskTypes **48** ✅ · la versión del título está **vieja** | **Números imprecisos** (sólo TaskTypes acierta) |
| 1 | `TaskStatus` duplicado, unificar en `src/domain/types.py` | **La observación es cierta, el REMEDIO es un error de análisis** (verificado el 2026-10-09 al ir a implementarlo): son **tres máquinas de estado de cosas distintas**, no una duplicada. `task_board` = tablero de coordinación (`QUEUED/RUNNING/WAITING_MODEL/WAITING_RESULT/DONE/FAILED/**REFUSED_OOM**`), `task_queue` = cola de trabajo (`PENDING/RUNNING/COMPLETED/FAILED/**CANCELLED/TIMEOUT**`), netron = orquestador ajeno (no integrado). Y **cada una la usa un consumidor distinto**: el tablero lo usa `agent_coordinator` (`TaskStatus.REFUSED_OOM` ×3) y la cola la usan `parallel_executor`, `distributed_queue`, `proactive_engine` y `dispatcher_distributed`. Unificarlas exigiría la UNIÓN de los estados y obligaría a cada consumidor a defenderse de estados imposibles: **peor, no mejor** | ❌ **NO implementar** (a lo sumo, desambiguar los nombres: `BoardTaskStatus` / `QueueTaskStatus`) |
| 2 | `--trace-flows` no está implementado | Medido: el flag `--trace-flows` es **del documentador** (`tools/documentador/axioma_doc_main.py`, herramienta **privada** que genera `docs/AXIOMA_COMPLETE_CONTEXT.md`), no del producto. El producto **sí** tiene infraestructura de trazas (`TraceStore`, `SessionEventLog`) | ❌ **No es una brecha del producto** (lo que falta es un flag de una herramienta interna) |
| 3 | Los 4 «flujos críticos» sin rastreo | Las marcas `[❓ SIN RASTREO]` salen de **`docs/AXIOMA_COMPLETE_CONTEXT.md`** (481 ocurrencias), un documento **generado** por el documentador —que ya no se publica— | ❌ **Confunde documentación con código**: son huecos del documento generado, no del producto |
| 4 | El ciclo `FeedbackLoop → PromptOptimizer` no cierra | `PromptManager.record_quality()` existe **y se llama** (`src/agents/coder.py:678`) | ❌ **Desactualizado** (ya se cerró: ISSUE-069) |
| 5 | `InterpreterLoop` sin validación semántica | Hay un `validator` en el constructor y se usa (`interpreter_loop.py:394`), pero para **ejecutar**, no para validar la respuesta contra la intención | ⚠️ **Parcial**: tiene razón en el fondo, mal en el detalle |
| 6 | Falta dedup en el cross-sesión (similitud > 0.92, límite 2) | `_inject_cross_session()` **ya filtra por similitud** (`cross_session_min_similarity`, hoy 0.75) | ⚠️ **Parcial**: el umbral existe; el tope de inyecciones no |
| 7 | Faltan tests de `dispatcher_distributed`, `dispatcher_plan`, `dispatcher_process_guard` | Se puede comprobar: hay 23 archivos de test y ninguno lleva esos nombres | ⚠️ **A verificar antes de creer** (no medí cobertura por módulo) |
| 8 | `HardwareFitManager` sólo avisa (debería rechazar/degradar) | No encontré rechazo explícito en `hardware_fit`/`model_swap`; **sí** existe el nivel `no_fit` y el `MemoryGuard` rechaza cargas (`refused_oom`) | ⚠️ **Dudoso**: hay compuertas de memoria; hay que medir el caso `no_fit` |
| 9 | Verificar que `ProactiveEngine` consume las predicciones | `get_frequent_patterns()` se usa **y** hay un FIX documentado en el código («antes el resultado de `_predict_next_intent()` se ignoraba») | ❌ **Desactualizado** (ya está hecho) |
| 10 | Permisos por tool en `McpToolBridge.connect_server()` | No existe `connect_server` ni `tool_permissions`; **sí** existe `ToolPermission` (usado en `agents/base.py`) | ❌ **La premisa no coincide con el código** |

**Conclusión corregida** (2026-10-09, al ir a implementar y medir a fondo): de 10 puntos, **ninguno es
implementable tal cual**. Los dos que parecían accionables se cayeron al verificar: el 1 porque las clases
son máquinas de estado **distintas con consumidores distintos** (unificar sería peor) y el 2/3 porque el
flag es de una **herramienta interna** y las marcas «sin rastreo» son huecos de un **documento generado**.
Quedan **4 parciales** (hay algo de razón pero el detalle no coincide) y **5 son desactualizados o inventados**
(describe cosas que ya están hechas o que no existen con ese nombre). La causa más probable del desajuste:
la respuesta se apoya en **documentos generados** (el documentador, las marcas `[❓ SIN RASTREO]`) en vez
de leer el código.
**Lo que sí queda como trabajo real, si el usuario quiere**: unificar `TaskStatus` (con prueba de que los
estados migran bien) y revisar el interruptor `--trace-flows` (que hoy no existe).

### D-33 · Desambiguación de las dos máquinas de estado (hecho el 2026-10-09)
**Decisión del usuario** (tras la corrección de D-32): renombrar para que nadie vuelva a proponer
«unificarlas». Se implementó **sólo eso**, y con el alcance **medido**, no con la lista que traía la
respuesta externa:
- `src/core/task_board.py`: `TaskStatus` → **`BoardTaskStatus`** (tablero de coordinación; estados propios:
  `WAITING_MODEL`, `WAITING_RESULT`, `REFUSED_OOM`).
- `src/core/task_queue.py`: `TaskStatus` → **`QueueTaskStatus`** (cola de trabajo genérica; estados propios:
  `COMPLETED`, `CANCELLED`, `TIMEOUT`).
- **La lista de la respuesta estaba equivocada**: decía que había que tocar también `parallel_executor`,
  `distributed_queue`, `proactive_engine` y `dispatcher_distributed`. Medido: **ninguno** de esos importa
  `TaskStatus` (importan `TaskQueue`/`Task`/`TaskPriority`). Los únicos tres lugares eran
  `agent_coordinator.py` y dos archivos de test.
- `tools/netron/netron_orchestrator.py` se deja como está: no se integra ni se publica.
- **No hay datos que migrar**: se comprobó que las clases sólo se comparan por miembro (no se serializan
  como texto), así que renombrar no toca nada guardado.
**Candado**: una prueba que exige que las dos clases existan, sean distintas y que **cada una tenga estados
que la otra no tiene**. Mutación verificada: al simular la «unificación» (darle a la cola los mismos
estados que el tablero) la prueba **falla** ✅. La prueba vive en `tests/test_axioma_27_portabilidad.py`
por el límite de 950 líneas de la regla R13: `test_axioma_08` quedó en 941 líneas (venía de 984).
**Comentario en cada clase** explicando por qué no se unifican, para que el próximo análisis —humano o de
IA— no repita el error.

### D-34 · Windows: no hay máquina disponible (ni del autor ni del usuario), así que se automatiza todo
**Situación (2026-10-09)**: el usuario confirma que **tampoco** tiene Windows. Esperar una máquina no es un
plan, así que la decisión es: **cubrir con automatización todo lo que no necesite una persona** y decir con
precisión qué queda sin probar.
**Lo que ya estaba cubierto** (trabajo `instaladores-windows` del CI, con PowerShell 5.1 y 7): BOM,
sintaxis, acentos, ensayo de los dos instaladores y del demonio de voz, y el rechazo de un interruptor
inexistente.
**Lo que se agrega ahora** (el hueco más grande: que el demonio **encuentre** el endpoint en Windows):
`tests/axioma_falso_http.py` (servidor mínimo de la biblioteca estándar) + un paso del CI que lo levanta y
corre `instalar/rafael_windows.ps1 -Ensayo -Url http://127.0.0.1:8098`: si el demonio no encuentra el
endpoint, el trabajo se pone rojo. Probado localmente que el falso contesta ✅.
**Lo que sigue SIN poder probarse sin una persona** (y así queda dicho en el README público, sin prometer
de más): que los accesos directos **aparezcan** en el menú Inicio y el Escritorio, que el navegador **se
abra solo**, y el **micrófono** de verdad. Todo lo demás (guiones, rutas, perfiles, endpoint, permisos,
imagen) se prueba solo en cada cambio.
**Si algún día aparece un Windows**: `PRUEBA_WINDOWS.md` tiene el recorrido de 5 pasos y qué pegar de
vuelta.

### D-35 · Registro en archivo para los guiones de Windows: intentado DOS veces, revertido, y con las fuentes
**Pregunta del usuario**: «si lo instalo en Windows, ¿los errores quedarán en algún log?». **Medido**: no —
los tres `.ps1` sólo imprimían en pantalla, así que un fallo se iba con la ventana.
**Intento 1** — `Start-Transcript` a `logs\`: el CI (nuestro único Windows) se puso **rojo** en PowerShell
5.1 y **no se pudo ver el detalle** (los registros del runner piden credenciales).
**Búsqueda en internet** (lo que faltaba hacer antes): `Start-Transcript` **falla en hosts que no soportan
transcripción**, por ejemplo cuando la salida está **redirigida o capturada** — que es exactamente como el
CI ejecuta estos guiones. Fuentes: [«This host does not support transcription»](https://stackoverflow.com/questions/5032075)
· [cómo redirigir toda la salida de forma robusta](https://github.com/PowerShell/PowerShell/discussions/25174).
**Intento 2** — que el propio guion escriba el archivo (`Add-Content` desde sus cuatro funciones de
mensaje, sin depender del host): **el CI volvió a ponerse rojo** en el trabajo de Windows. Sin poder ver el
registro del runner, no se puede saber si el fallo era del registro o de otra cosa.
**Decisión**: **revertido**. La regla que me puse vale más que la mejora: *algo que rompe la instalación no
se entrega sin validar*, y el CI es el único Windows que tenemos.
**Lo que queda (verificado)** para capturar un fallo hoy: los registros de AXIOMA (`logs/`), los de la voz
(`logs/rafael.log`, `data/registros_voz/`), los del contenedor (`docker compose logs --tail 100
axioma-puente`), y —a mano— envolver la corrida en la consola del usuario:
`Start-Transcript -Path logs\instalacion_windows.txt` … `Stop-Transcript` (en la consola interactiva sí
funciona).
**Para retomarlo con un Windows a mano**: agregar el registro por archivo, correr
`tests\probar_instaladores_windows.ps1` y ver el error exacto en pantalla; recién entonces publicarlo.

### D-36 · Fase 3: repositorio público limpio (2026-10-10) — verificado línea por línea
**Lo que pidió el usuario**: limpiar el repositorio público comprobando **cada línea** de lo que se afirma que
sobra, en vez de aplicar listas de palabras.
**Lo que resultó cierto, con la medición delante**: (1) `MANUAL_AXIOMA.md` decía «**saintwick** es el usuario
root (admin)» — falso hoy: la identidad arranca **vacía** (`config/settings.py:675`, `default=""`) y el primer
usuario lo crea quien instala, así que el manual contradecía al producto; (2) `requirements.txt` explicaba, con
nombre y apellido, el **entorno virtual compartido** del autor y su otro proyecto; (3) `COMANDOS_AXIOMA.txt`
tenía secciones del entorno del autor (documentador, optimizador de su distro con su hardware, harness) y una
tarea pendiente suya sobre el token; (4) `README_TECNICO.md` enlazaba a `docs/`, que no se publica.
**Lo que resultó FALSO** (el plan externo las daba por personales): «Arch/CachyOS 996» en `docker-compose.yml`
es el **GID real del grupo de audio** (Debian/Ubuntu 29 · Fedora 63 · Arch 996) y «Escritorio» en `ci.yml` es
el de **Windows**; las dos se quedan. Y las menciones históricas del `CHANGELOG` también: un historial que
cuenta lo que pasó no manda a nadie a buscar un archivo que no existe.
**Decisión de diseño**: se abandonó la limpieza por **palabras** en el saneador y se pasó a una **lista curada
de líneas exactas**, más tres reemplazos con texto de reemplazo verificado (bloque del entorno compartido,
distro en los encabezados, menciones al otro proyecto en `.dockerignore`/`Dockerfile`). Un documento pierde
una línea entera o no pierde nada.
**Lo que queda del producto, a propósito**: el nombre del autor y el enlace real del repositorio en la
atribución del README técnico, y las menciones a `deepseek-harness` dentro del **código**, del
`.dockerignore` y del `Dockerfile` (listas de exclusión y comentarios): es un proyecto de código abierto
que el autor usa, no un dato privado. A esas dos últimas las intenté sacar de la copia publicada y el CI
se puso **rojo**: una prueba publicada **exige** que `.dockerignore` excluya `deepseek-harness/`. Revertido
(el detalle, en la lección 28).

### D-37 · Prueba visual de la sección de modelos: encontró un error real (2026-10-10)
**Lo que pidió el usuario**: probar en un navegador de verdad la sección nueva (🎯 elegir modelo por rol),
no sólo con pruebas unitarias. **Cómo se hizo**: se extendió `tools/verificar_primer_arranque.py` (es su
lugar: la sección vive dentro de «Configurar AXIOMA») con una tercera comprobación que contrasta **tres
cosas que tienen que coincidir**: lo que **muestra la pantalla** (leyendo los campos por DOM), lo que
declara **`GET /api/v1/preflight/modelos`** y lo que está **instalado en Ollama**. Deja la captura en
`data/capturas/modelos_en_configurar.png` para mirarla con los ojos.
**Lo que encontró (error mío de la Fase 1)**: la pantalla mostraba **«Programar (código)» VACÍO** aunque el
`.env` tenía `LLM_CODE_MODEL=qwen2.5-coder:7b`. Medido: en `preflight_modelos` el nombre del atributo se
armaba desde el **rol** (`llm_codigo_model`, `llm_codigo_respaldo_model`), que no existen, así que `codigo`
y `codigo_respaldo` salían siempre vacíos. **Alcance verificado**: sólo esa función — el catálogo y el
preflight (`config/model_catalog.py::modelos_efectivos`) usaban los nombres correctos. **Arreglado**:
el nombre sale de la variable (`LLM_CODE_MODEL` → `llm_code_model`), o sea de `VARIABLES_DE_ROL`, y hay una
prueba de regresión con su **mutación comprobada** (con el error viejo, falla).
**Por qué la suite no lo cazó**: la prueba existente miraba `instalados` y `ollama_disponible`, nunca
`configurados`. Una pantalla de configuración hay que probarla diciendo **qué valores muestra**, no sólo
que la lista cargó.
**Verificado después del arreglo, en pantalla** (captura del 2026-10-10): chat → `qwen3:8b` · código →
`qwen2.5-coder:7b` · respaldo → `huihui_ai/qwen2.5-coder-abliterate:7b` · visión → `qwen3-vl:4b`, con Ollama
ofreciendo sus 4 instalados.
**Dos defectos más de la herramienta, arreglados en la misma pasada**: (1) si fallaba a mitad salía
**sin reponer** el marcador de primer arranque, y al usuario le volvía a aparecer la guía (ahora se repone
siempre, con `try/finally`); (2) una dirección caída salía como **traceback** de Playwright en vez del
código 4 con el motivo (ahora dice qué pasó y recuerda encender AXIOMA).

### D-38 · La pantalla de configuración quedaba tapando todo (lo reportó el usuario, 2026-10-10)
**Síntoma (palabras del usuario)**: al abrir «Configurar AXIOMA» se despliegan las opciones de modelos y,
al elegirlas, «se quedan ahí visibles y no vuelven por más que haga clic en el logo, haciendo que tape la
pantalla y no me deje hacer nada».
**Medición (navegador real, no lectura de código)**: los 4 campos de modelo estaban en **y = 815, 859, 903
y 947** de una ventana de 900 y su cadena de ancestros era `nicegui-content < q-page …`, o sea **fuera del
diálogo**. **Causa**: en `configurar_axioma.py` las llamadas `_seccion_chequeo_de_modelo()` y
`_seccion_modelos()` estaban al FINAL de la función, **fuera** del `with ui.dialog()...`: sus elementos se
dibujaban en la **página**, así que al cerrar la pantalla se quedaban a la vista, y **cada clic en el ícono
agregaba otra copia** (por eso terminaba tapando todo). Es un error mío de la Fase 1.
**Arreglo (mínimo)**: mover las dos llamadas **adentro** del `with`, antes del botón Cerrar.
**Verificado después, en el navegador**: los campos están dentro del diálogo; al cerrar quedan **0**
diálogos y **0** campos a la vista, el centro de la pantalla vuelve a ser la página, y los valores siguen
siendo los que AXIOMA usa (chat `qwen3:8b` · código `qwen2.5-coder:7b` · respaldo
`huihui_ai/qwen2.5-coder-abliterate:7b` · visión `qwen3-vl:4b`).
**Prueba de regresión con el árbol sintáctico** (`ast`, no `in texto`): la llamada tiene que estar **dentro
del `with` que crea el diálogo**; con el código viejo la prueba **falla** (mutación comprobada).
**La verificación visual también se hizo más fuerte**: ahora comprueba **dónde** se dibujan los campos y que
al cerrar no quede nada tapando. Antes sólo miraba los **valores** — por eso no lo cazó, aunque los valores
fueran correctos.
**Corrección de algo que dije mal**: `diagnostico_v2.py` **nunca estuvo** en el repositorio público (está en
el `.gitignore` que se copia: `/diagnostico_*.py`). Me confundí de repositorio al afirmarlo y el usuario
autorizó sacarlo por eso. Igual se agregó la exclusión explícita como red de seguridad, y queda dicho acá
para no repetir el error.
**Un detalle del clon público**: `preparar_publicacion.sh` **vacía la carpeta destino** (conservando `.git`)
y la rehace con `rsync`; si el `rsync` falla, el clon queda vacío. Medido hoy al romper ese comando con un
comentario: la herramienta de publicación **corta** en ese punto (`if codigo != 0: return False`), así que
nunca se llegó a commitear ese estado. El comentario se movió fuera del comando y quedó escrito por qué.

### D-39 · Revisión del informe de Sonnet sobre el repositorio público (2026-10-10)
**Cómo se trabajó**: se verificó **cada punto y cada remedio** antes de tocar nada (pedido explícito del
usuario: «asegurate de que lo que se va a implementar sea lo correcto»). Los seis puntos resultaron ciertos;
uno de los remedios propuestos era peligroso y se descartó, y el informe tenía un punto equivocado.
**1) Windows — CIERTO y bloqueante.** Los tres `.ps1` no creaban `.env`. Probado el mecanismo con
`docker run -v /tmp/pb/noexiste.env:/app/x alpine sh -c 'ls -ld /app/x'` → **`drwxr-xr-x`**: Docker crea una
CARPETA y el montaje `./.env:/app/.env` queda roto. **Y el informe no lo vio**: tampoco creaban `data/`,
`logs/` ni `cache/` (la misma familia del PermissionError de Linux, D-31). Arreglado en los dos
instaladores, con el **BOM repuesto** y verificado por el comprobador del CI (código + ensayo) y por la suite.
**2) `task_mappings` — CIERTO, y con una corrección a MI propio informe.** `get_model_for_task` mira
**primero** el mapeo y `_sync_with_settings` sólo se aseguraba de que el modelo existiera: con 6 tareas
mapeadas a nombres fijos (las 4 de código y `general_chat`/`complex_reasoning`), el modelo elegido en la
pantalla **no cambiaba ninguna decisión en ejecución** (sólo el preflight, que era lo que yo había
arreglado). Dije antes que eran «las 13 tareas»: **falso — son 6** (las otras 7 sólo declaran temperatura y
ya seguían al `.env`). Arreglado según lo que decidió el usuario: código → `LLM_CODE_MODEL` con
`LLM_CODE_MODEL_FALLBACK` en la cadena de respaldo, el resto → `LLM_DEFAULT_MODEL`, **conservando la
temperatura de cada tarea**; con el `.env` por defecto los valores coinciden con `models.yaml`, así que
nada cambia para quien no tocó nada (medido: `code_generation` 0.2 intacto).
**3) Guardar un modelo no se aplicaba — CIERTO.** El endpoint escribía el `.env` y respondía «AXIOMA lo usa
para ese rol», que era falso hasta reiniciar (la pantalla, que lee `settings`, seguía mostrando el viejo).
Ahora se actualiza la copia en memoria y se descarta el router (`cleanup_router()`), que se rearma leyendo
los valores nuevos; el mensaje dice que se aplica ya. Verificado que el despachador **no** es otro proceso
(`dispatcher_process.py` es un módulo): el cambio alcanza a todo el proceso.
**4) Sin validación de `Host` — CIERTO.** Con el CORS ya corregido, quedaba el **DNS rebinding**: una página
apunta un dominio propio a 127.0.0.1, el navegador lo ve como el mismo origen y el CORS no la frena; podía
llamar a las rutas que **escriben el `.env`**. Se agregó la validación cuando AXIOMA está ligado a la
dirección local (sólo `127.0.0.1`/`localhost`/`::1`), y **no** se restringe si el usuario lo expone a
propósito (`AXIOMA_WEB_HOST=0.0.0.0`, que ya era una decisión documentada). Las pruebas pasaron a hablar por
`base_url="http://127.0.0.1"` (el `Host` por defecto de `TestClient` es `testserver`).
**5) `vision_model_selector` — CIERTO pero MÁS inocuo de lo que dice.** Era un `Literal["auto",
"qwen3-vl:4b"]`, pero medido: `get_vision_config()` devolvía lo mismo en las dos ramas y en la «inválida» —
o sea **no tenía ningún efecto**. Ahora es texto libre («auto» = el VLM que configuraste) y se eliminó la
rama muerta.
**6) Limpieza — PARCIAL, y acá el informe se equivoca.** `.dockerignore:6,16` y `Dockerfile:87` con
`deepseek-harness` son **deliberados**: una prueba publicada EXIGE esa exclusión (cuando la saqué, el CI se
puso rojo; ver D-36). El `CHANGELOG` tiene **7** menciones del nombre del autor, no 26: la historia se
restauró a propósito. Lo accionable era el mensaje de `tools/verificar_imagen.sh` que manda al usuario a
`docs/PLAN_CONTENEDORES.md` (no se publica) → corregido; y los datos de prueba con el nombre del autor en 5
archivos → pasados a un nombre neutro (la guarda que vigila que no aparezca sigue puesta).
**Dos errores míos en esta misma vuelta (los dos cazados por pruebas del proyecto)**: (a) editar un `.ps1`
con las herramientas le **borra el BOM** y (b) volví a meter el emoji ✅ en un `.ps1`, que la consola de
Windows imprime mal. Los dos los marcó `tests/test_axioma_27_portabilidad.py`. **Regla desde ahora: después
de tocar cualquier `.ps1`, correr ese archivo de pruebas antes de seguir.**
**Lo que queda (nadie lo puede hacer desde acá)**: probar en un Windows real, la prueba de máquina limpia, y
el modo paralelo con una placa de video utilizable.

### D-40 · Refactorización 50/50 de los archivos >1000 líneas (2026-10-22/24)
**Por qué**: el usuario lo pidió explícitamente —*«la refactorización es útil porque hace que el
mantenimiento sea mejor, no tenemos que compartir archivos monolíticos»*— y porque el auditor marcaba
los archivos de más de 1000 líneas como «Archivo pesado». La regla R14 se amplió el 2026-10-12 para
permitir **dividir** (no crear carpetas nuevas).
**Cómo se hizo (patrón R6, 50/50)**: el archivo original conserva la **API pública** y **re-exporta**
lo movido con los mismos nombres y objetos; la otra mitad va a un **hermano** en la misma carpeta; la
dependencia queda en **un solo sentido** (original → hermano); los nodos movidos se verificaron por
**AST idéntico** al HEAD; `symtable` vacío; suite verde **antes y después de cada parte**.
**Particiones medidas** (original → hermano):

| Original | Antes | Después | Hermano(s) |
|---|---|---|---|
| `src/core/dispatcher_process.py` | 1423 | **988** | `dispatcher_process_steps.py` (681), `dispatcher_process_guard.py` (330) |
| `src/core/dispatcher_handlers_extra.py` | 1390 | **869** | `dispatcher_handlers_extra_code.py` (588) |
| `src/router/classifier_core.py` | 1214 | **312** | `classifier_keywords.py` (578), `classifier_keyword_rules.py` (211), `classifier_api.py` (617) |
| `config/settings.py` | 1146 | **317** | `settings_llm.py` (214), `settings_memory.py` (146), `settings_agents.py` (162), `settings_computations.py` (503) |
| `src/interfaces/web/app.py` | 1134 | **759** | `app_ui.py` (578) |
| `src/interfaces/cli/main.py` | 1215 | **863** | `backup.py` (421) |
| `src/agents/analyst.py` | 1505 | **853** | `analyst_review.py` (748), `analyst_file_handlers.py` (298) |
| `src/memory/gateway.py`, `task_queue.py`, `client_base.py`, `sandbox.py`, `voice_pipeline.py`, `chat.py`, `command_parser.py`, `dispatcher_handlers.py` | 1054-1123 | 483-790 | un hermano por archivo |
**Estado medido (2026-10-25)**: `find src -name '*.py' -exec wc -l {} +` ⇒ **0 archivos >1000
líneas**. La C4 de §8 quedó **cerrada**.
**Lo que se aprende**: una partición no es “mover texto”: hay que barrer parches de tests a globals
del módulo, re-exports y lecturas diferidas para no romper la API ni crear ciclos (AUD-CIR).
**Costo**: cada parte se cerró con suite y gate; las que movieron estado mutable compartido
(`_PENDING_APPROVALS`, singleton de `TaskQueue`, ContextVar de cancelación) exigieron lectura
diferida y una sonda real, no sólo tests.

### D-41 · Seguridad de la interfaz: token, `session_id`, bind local y los 15 MEDIUM (2026-10-25)
**Qué se decidió**:
1. **Token en las rutas que ESCRIBEN configuración** (`POST /api/v1/preflight/claves` y
   `/preflight/modelos`): `AXIOMA_WEB_TOKEN` en entorno/`.env` (campo `settings.axioma_web_token`),
   header **`X-AXIOMA-Token`**, comparación con **`hmac.compare_digest`** y **sin registrar el valor
   recibido**. Default seguro: si hay token se exige **siempre** (también en loopback); si no hay
   token, se permite en loopback y se **rechaza con 403** cuando el bind no es local. Las rutas de
   sólo lectura siguen abiertas. El código de acceso se **genera solo** (`data/.web_token`, 0600,
   escritura atómica) y se rota con `python main.py --rotar-token`.
2. **`session_id` validado en un único punto** (`WorkspaceManager._workspace_path`):
   `^[A-Za-z0-9_-]{1,64}$` + contención real `resolve().is_relative_to(base)`, **rechazando con
   `ValueError`** (sin sanear en silencio). Las rutas `/workspace/download` y `/workspace/attempts`
   devuelven **400**; la tool devuelve un fallo con el mensaje. Motivo medido: `session_id=/etc`
   descartaba el base y `GET …/download` devolvía **200 con 2182 B de `/etc/passwd`**;
   `cleanup('../evil')` borraba fuera del base (alcanzable por prompt injection).
3. **Bind local por defecto**: `start_server()` y el wrapper de la UI pasan de `0.0.0.0` a
   **`127.0.0.1`**; para exponerla hay que pedirlo explícitamente (`AXIOMA_WEB_HOST=0.0.0.0`).
   Motivo medido: la UI y su websocket quedaban accesibles desde la LAN aunque `/api/*` estuviera
   protegido.
4. **Los 15 MEDIUM del auditor corregidos de verdad** (sin `# noqa`): **7 paths** derivados de
   parámetros (`save_code_to_file`, `upload_file_endpoint`, `review_system`, `save_transcript`,
   `_stage`, `export_research`, `_load_template`) validados **antes** de tocar disco con allowlist +
   contención real, y **8 imports nativos** (`lancedb` ×3, `cv2`, `faster_whisper`, `webrtcvad`,
   `pvporcupine`, `openwakeword`) que sólo atrapaban `ImportError` y pasaron a
   `except (ImportError, OSError)` (un fallo de libportaudio/libGL/CUDA lanza `OSError`).
   Medido: **MEDIUM 15 → 0**, deuda **143 → 68**, `audit_baseline.json` regenerado; 6 vectores
   escapaban del base antes y **40/40 PASS** después; los 8 imports degradan sin tumbar el módulo.
**Sobre los «9 hallazgos de seguridad»**: ninguna fuente del repo los enumera como un conjunto de 9
(se buscó en `CHANGELOG.md`, `docs/` y los mensajes de commit). Lo **verificable** es: **15 MEDIUM**
del auditor (7 paths + 8 imports), **1 HIGH** de `rmtree` sin contención en el snapshot `c4cd41f^`
(hoy 0 HIGH) y **3 exposiciones** de la interfaz (token faltante en rutas de escritura,
`start_server` en `0.0.0.0`, `session_id` sin validar). Si «9» venía de otro informe, **no se pudo
reconstruir**: se deja dicho para no inventar la lista.
**Verificación**: sondas reales con `TestClient` (antes 200/escribe, después 403/sin tocar el `.env`;
con token válido 200), tests en `tests/test_axioma_29_seguridad_web.py` (27 passed),
`test_axioma_34_auditor_honesto.py` (0 MEDIUM de esos dos detectores) y la suite completa.

### D-42 · Pasada de honestidad del auditor A1-A9 (2026-10-25)
**El problema de partida**: el SCORE 100 **no medía calidad** — el snapshot `c4cd41f^` (path
traversal + `rmtree` fuera del base + 49 conexiones SQLite sin cerrar) también sacaba 100.
**Qué se hizo**:
- **A1 — SCORE honesto**: el SCORE sigue siendo la métrica del gate (sólo resta CRITICAL/HIGH) pero
  ya no se presenta como nota de calidad. El JSON agrega `score_meaning`, `quality_notice`, `debt`
  (MEDIUM×5 + LOW×1), `scope` y `regression`; `audit_baseline.json` **versionado** congela la deuda
  y `--ratchet` sale con **2** si empeora por detector. El gate suma **ratchet bloqueante** y deuda
  informativa (13 → **15 condiciones**).
- **A2 — 4 detectores nuevos** (34 en total): `path_containment` (AUD-PTH), `unsafe_path_delete`
  (AUD-DEL, HIGH), `native_import_oserror` (AUD-IEO) y `unsafe_exec` (AUD-EXEC). Sonda con el
  snapshot: de 180 hallazgos/MEDIUM 0/SCORE 100 a **23 hallazgos, 1 HIGH, MEDIUM 10+12** ⇒ SCORE 90.
- **A3** `connection_per_method` evalúa **cada** `sqlite3.connect` y acepta cierre real
  (`closing`, `try/finally`, atributo cerrado por la clase o pool); `with` **no** cierra (hace
  commit/rollback).
- **A4** `config_refs` pasa a **AST-aware** (no marca strings, anotaciones ni comentarios).
- **A5** `silent_exceptions` deja de sumar ruido: excluye los que re-lanzan, los que reportan con
  `warnings.warn`/`traceback` y las sondas que devuelven default; el resto queda como **INFO**.
- **A6** `integration` cuenta **líneas efectivas** (sin blancos/comentarios/docstrings) y hace
  visible el alcance (`files_indexed`, excluidos por carpeta, `parse_errors`).
- **A7** historial append-only `logs/audit_history.jsonl` (timestamp, commit, versión, score, deuda,
  conteos, por detector), que la poda **no** toca.
- **A8** `--fail-on HIGH|MEDIUM` (sale 2 sin cambiar el JSON).
- **A9** versión y conteo unificados: **34** detectores, versión leída de `src/__init__.py`
  (**0.7.5**), documentados `--write-baseline/--ratchet/--fail-on/--include-tools-tests`.
**Resultado medido**: la pasada cerró en **26/26 archivos, 1190 tests, 132 hallazgos (0/0/15 MEDIUM),
deuda 143, gate 15/15**; al corregir los 15 MEDIUM (D-41) quedó en **117 hallazgos (0/0/0, 68 LOW,
49 INFO), deuda 68, SCORE 100, 1191 tests**. La lección: un número que no baja cuando el código
empeora no es una métrica, es un adorno.

### D-43 · Documentador D1-D10: que el documento no mienta (2026-10-25)
**Por qué**: el generador aprobaba secciones **por nombre de clase** y `--validate` imprimía
«VALIDACIÓN APROBADA» **sin llamar al validador**; `--strict-secrets` **fallaba abierto** si no
encontraba el escáner.
**Qué se hizo (D1-D10)**:
- **D1** §5 verifica **`Clase.método`** (con herencia y mixins): los 6 métodos inexistentes salen
  `❌ NO ENCONTRADO`; se corrigieron los nombres de `CRITICAL_FLOWS_DEFINITION`.
- **D2** `--trace-flows` corre **por defecto**; si se desactiva, el encabezado avisa que §5/§6/§18
  están incompletas.
- **D3** `--validate` mide cobertura **real** con `validate_docs.py`, muestra el desglose y sale 1
  bajo el umbral (**58,2 %** medido, umbral **55 %**; antes contaba 8545 símbolos fuera de alcance).
- **D4** `--strict-secrets` **fail-closed**: el escáner se integró en `sensitive_filter.py`
  (versionado), `secret_scanner.py` se eliminó y si el filtro no está disponible sale **7** antes de
  generar.
- **D5** IA **opt-in** (`--ollama`), modelo validado contra `ollama list`, caché por modelo+versión,
  `strip_ansi()` (0 ESC en el `.md`), docstring curado primero y resúmenes de método visibles.
- **D6** encabezado con **commit/fecha/alcance** y «cómo leer»; §8 con índice de módulos y clases
  (462 clases, 21 paquetes) y separada por paquete.
- **D7** poda de código muerto (1.484 líneas del generador histórico + 323 de `axioma_safe_doc.py`),
  `--force`/`--strict-include` (no-ops) eliminados y `--clear-cache` implementado de verdad.
- **D8** §10/§13 avisan si falta `data/` (antes quedaban vacías sin decirlo).
- **D9** `tests/test_axioma_33_documentador.py` (11 pruebas, ~10 s) genera el documento real en
  `tmp_path` y cubre §5/§7/§18, flags, `--validate`, `--clear-cache` y ausencia de ANSI.
- **D10** §7 muestra **todos** los `async def` (336 de 336; antes cortaba en 40).
**Costo**: el documento final pesa ~592 KB (índice y anclas ~168 KB) y la corrida sin IA ~6,6 s.
**Lo que se aprende**: una herramienta de documentación que “aprueba” sin medir es peor que no
tenerla: produce confianza falsa.

### D-44 · Instaladores: imagen desde GHCR y Release con `.exe` (2026-10-23, v0.7.5)
**Por qué**: el usuario final no debe clonar el repositorio, ni tener Python, ni construir la imagen
(~6 min más descargas). **Descarga un archivo y lo ejecuta.**
**Qué se decidió**:
- **La imagen se publica en GHCR al etiquetar un release** (job `publicar-imagen`, después de que la
  suite pase: `needs: [test]`). Corre `tools/verificar_imagen.sh` (compila, mide el tamaño y corre la
  suite **adentro**) porque **una imagen que no pasa sus pruebas no se publica**. Nombre desde
  `github.repository` en minúsculas, tag del release + `latest`; **sólo desde el repositorio público**
  (un paquete publicado desde uno privado nace privado).
- **Los instaladores bajan en vez de construir**: `docker pull ghcr.io/<repo>:<versión>` → `latest` →
  y **sólo si fallan los dos**, build. Lo bajado se etiqueta **`axioma:local`**; usan un
  `DOCKER_CONFIG` temporal con `{"auths":{}}` para que un ayudante de credenciales roto no tumbe el
  pull.
- **Release con `AXIOMA-Setup.exe`** (`instaladores-release`): se construye en **Windows**
  (PyInstaller no compila cruzado), **se verifica que arranca** y se publican los dos assets. El
  código distribuido **no lleva el repositorio escrito**: va un marcador `__REPO__` que el CI
  reemplaza junto con la versión del tag, y si el `sed` no engancha el job **falla** en vez de
  publicar un instalador que baja otra cosa.
**Verificación**: `tests/test_axioma_32_instaladores_pull.py` ejecuta el instalador real sobre un
árbol temporal con un `docker` falso (4 escenarios: tag de versión, fallback a `latest`, fallan los
dos ⇒ dos pulls + build, sin `AXIOMA_REPO` ⇒ build), hermético en 0,07 s.
**Pendiente vivo**: el **`.exe` del v0.7.5 no se probó en un Windows real**; el CI lo construye y
verifica que arranca, nada más.

### D-45 · Limpieza de `docs/` sin backup: borrados definitivos (decisión del dueño, 2026-10-25)
**Contexto**: `docs/` está **gitignoreado desde el 2026-09-13** (`.gitignore:120`, commit `6816737`).
Sus **12 archivos no están versionados** y **6 nunca lo estuvieron** (medido con
`git log --all --diff-filter=A --name-only -- docs/`: el plan decía 5): `FASE0_BLOQUEADORES.md`,
`FIX_RAC_v0.6.9w.md`, `FIX_VERSIONES_v0.6.9x.md`, `PLAN_CONTENEDORES.md`,
`PLAN_MEJORAS_2026-10-07.md` y `REVISION_PLAN_QWEN.md`. Borrar ahí **no es reversible** por git.
**Decisión del dueño del proyecto**: **no** se crea tar de respaldo para la limpieza (se descartó el
`data/backups/docs_pre_limpieza_20261025.tar.gz` que preveía el plan). Asume que los borrados de la
Fase 3 serán **definitivos** y que `docs/` no está en git **a propósito**.
**Condición que se puso antes de borrar**: primero se migra **toda** la trazabilidad a
`docs/ISSUES_HISTORY.md` (IDs `119`, `129`, `148`, `149`, `150`, `151`, más `085` y `137` que sólo
vivían en la lista de trabajo), se corrige el estado real en `AGENT_GUIDE.md` y se registra la semana
en este archivo. **No se borra nada en esta fase**: los 4 archivos de la Fase 3 se conservan hasta la
pasada siguiente.
**Ejecutado (2026-10-25, Fase 3)**: se borraron `docs/FASE0_BLOQUEADORES.md` (B1-B6 corregidos y
verificados, sin referencias entrantes), `docs/documentador.txt` (notas de la v0.5.0, duplicadas por
`COMANDOS_AXIOMA.txt`), `docs/PLAN_MEJORAS_2026-10-07.md` (cumplido; su contenido quedó en D-21…D-26) y
`docs/REVISION_PLAN_QWEN.md` (obsoleto; sus conclusiones quedan en D-20). Antes de borrar se verificó
con `grep -rn` que no quedaran referencias vivas (sólo el `CHANGELOG` histórico y `logs/`).
**Riesgo aceptado**: si un documento borrado hacía falta después, sólo se puede recuperar de una
copia externa o de la memoria de quien lo escribió. Queda dicho acá para que la decisión sea
explícita y no un accidente.

## Errores propios y qué aprendimos

1. **Preparé la copia pública en una carpeta que no era el clon** del repositorio: la copia quedaba
   impecable y **no se podía subir**. → El guion ahora avisa fuerte cuando el destino no es un repositorio
   git, y el verificador de publicación lo comprueba.
2. **La limpieza por líneas dejó un fragmento colgado** en la documentación pública (D-13). → Marcadores de
   sección y verificación; además el guion corta si una marca queda abierta.
3. **`--profile puente` levantaba también el servicio de Linux** y provocaba un bucle de reinicios (D-09).
   → Se nombran los servicios uno por uno, con prueba que lo exige.
4. **Acepté los códigos 0/1 del chequeo dentro de la imagen**, pero el preflight sin Ollama sale con **4**:
   la herramienta abortaba el commit por un motivo equivocado. → Se aceptan 0 y 4, y cualquier otro código
   sí aborta. Lección: **medir el código de salida real**, no suponerlo.
5. **Un cambio de sólo comentarios contaba como cambio real**. → En los `.py` se compara el contenido sin
   comentarios ni espacios; en documentación y configuración un comentario **sí** es contenido.
6. **El editor quitó el BOM de un `.ps1`** (lo cazó mi propia prueba). → Después de editar un guion de
   PowerShell hay que **volver a comprobar el BOM**.
7. **Tres especificaciones mías estaban mal escritas** en las pruebas nuevas: contaba los recuadros `═` de
   los títulos como emoji, prohibía la palabra «Escritorio» (que en Windows es legítima: el del menú) y
   prohibía `network_mode` incluso en un **comentario** que explicaba la diferencia. → **Leer el motivo del
   fallo antes de «arreglar» el código**: en los tres casos el equivocado era el test.
8. **Una prueba de mutación no cazaba el fallo** porque el texto buscado también aparecía en los
   comentarios. → Las comprobaciones de comportamiento miran **sólo las líneas que se ejecutan**.
   (El mismo día, otra vez: exigí que **todo** modelo se instalara con `ollama pull`, y el de voz es un
   archivo de Piper que se baja a `data/piper/`. Otra vez el equivocado era el test.)
9. **El lanzador con `nohup` murió al morir el trabajo que lo lanzó** y la verificación no corrió. → Usar
   trabajos de fondo **gestionados**, y confirmar que el proceso sigue vivo antes de esperarlo.
10. **Un respaldo tomado antes de una corrección revertía el arreglo** (sesiones anteriores). → Respaldar
    **después** de corregir, y comparar con `diff` antes de dar por restaurado.

11. **Agoté la cuota de la API de GitHub** consultando el CI cada 30 segundos: la tercera publicación
    terminó en «HTTP 403: rate limit exceeded» y no pude confirmar el resultado. → Ahora se consulta cada
    **90 segundos** (la API sin credenciales permite 60 consultas por hora) y, si igual se agota, la
    herramienta lo dice y deja el enlace para mirarlo a mano. Lección: una consulta en un bucle **gasta
    algo**; hay que espaciarla.

12. **El mensaje del commit público nombraba archivos del privado**: `DECISIONES.md` no existe en el
    repositorio público, y el mensaje igual lo listaba (usaba el resumen del privado). → El commit del
    público ahora arma su mensaje con **lo que realmente viaja** al público. Lección: cada repositorio
    cuenta **su** cambio, no el del otro.

13. **La herramienta no veía los commits hechos a mano**: el usuario commiteó por su cuenta y
    `publicar_cambios.py` contestó «no hay cambios reales que publicar», porque miraba sólo el **árbol de
    trabajo**. Un commit local sin subir es un cambio pendiente: ahora se mira también
    `git rev-list @{upstream}..HEAD`, se avisa cuántos son, y en ese caso **no se vuelve a commitear**
    (sólo se sube y se publica). Lección: preguntar «¿hay algo distinto en los archivos?» no es lo mismo
    que preguntar «¿hay algo pendiente de publicar?».

14. **Apliqué un parche que nunca se escribió**: edité `tools/publicar_cambios.py` con una lista de
    reemplazos, **no comprobé que cada uno coincidiera** con el texto real y **me olvidé de guardar el
    archivo**. El registro de decisiones decía que la mejora estaba hecha y el código no la tenía (el
    hueco del punto 13 seguía abierto). → Ahora cada reemplazo tiene que aparecer **exactamente** una vez
    (si no, el script corta) y el archivo se escribe al final, siempre. Lección: un cambio que no se
    verifica **no está hecho**, por más que el papel diga que sí.

15. **Rompí el CI entero con una expresión en `shell:`**: el trabajo nuevo de Windows usaba una matriz
    y `shell: ${{ matrix.shell }}`. GitHub **no acepta una expresión ahí**: el workflow no validó, la
    corrida quedó ROJA **sin ningún trabajo** (`total_count: 0`) y la corrida aparecía con el nombre del
    archivo (`.github/workflows/ci.yml`) en vez de «CI — AXIOMA pytest». Se arregló con dos pasos y
    `shell` literal, y la prueba ahora lo prohíbe. **Lo bueno**: lo cazó la herramienta sola (avisó
    «CI ROJO» con el enlace) — el paso 7 de `publicar_cambios.py` sirvió para lo que está hecho.

16. **Publiqué con una prueba en ROJO**: encadené `pytest … | tail -2 && publicar_cambios.py` y el
    código de salida que llegó era el de `tail` (0), no el de pytest: la prueba nueva fallaba y la
    publicación siguió igual, así que el CI quedó rojo otra vez. → Cuando el resultado de un comando
    decide si se sigue, **no se lo puede pasar por una tubería** sin mirar su código: se usa
    `; echo "SALIDA=$?"` y se lee. Lección repetida (es la misma familia del punto 8: la comprobación se
    tropezaba con su propia explicación en un comentario).

17. **`"$relativo:"` rompió el guion de Windows antes de arrancar**: en PowerShell, los dos puntos
    después de un nombre de variable se leen como una **unidad** (`$relativo:`), así que `Fallo
    "sintaxis de $relativo: ..."` es un error de **parseo**: el guion no se ejecuta ni una línea
    (*Variable reference is not valid. ':' was not followed by a valid variable name character*).
    Se escribe `"${relativo}:"`. **Lo cazó el trabajo de Windows del CI** (que existe justo para esto) y
    el usuario lo pegó acá. Ahora hay una guarda barata en la suite que busca `$var:` en los `.ps1`
    (la prueba de verdad sigue siendo el CI, con PowerShell de verdad). Lección: mi comprobación de
    llaves balanceadas no podía ver esto — **cada lenguaje necesita su propio verificador**, y el único
    que vale para PowerShell es PowerShell.

18. **Los dos trabajos nuevos de instaladores fallaron por lo mismo: el manejo de errores del CI.**
    - **Linux**: GitHub ejecuta los pasos de `bash` con **`-e`**, así que la comprobación del interruptor
      inexistente (que sale con **2**, y está bien que salga así) **cortaba el trabajo** antes de poder
      mirar el código, y el fallo aparecía sin mensaje. → El paso ahora hace `set +e` y mira los códigos
      a mano.
    - **Windows**: `powershell.exe` escribe en stderr al rechazar el interruptor y, al juntarlo con
      `2>&1`, PowerShell 5.1 lo convierte en un **`NativeCommandError`**: con `$ErrorActionPreference =
      "Stop"` el comprobador **moría sin decir qué pasó** (salida 1). → El comprobador usa `Continue`,
      envuelve la llamada en `try/catch` y **siempre imprime** lo que recibió.
    Lección: **el mismo guion se comporta distinto según quién lo llama** (una consola, el CI, un acceso
    directo). Lo que en mi máquina sale 0 o 2 sin drama, en el CI puede cortar todo el trabajo.
    Lo bueno: el CI lo dijo con precisión las dos veces, y el usuario pegó la salida.

19. **Asimetría entre los dos lanzadores** (la encontré revisando la salida que el usuario pegó de
    `instalar_axioma.sh --dry-run`): el de **Windows** miraba el código de Docker y avisaba «Docker no
    pudo encender AXIOMA (el motivo está arriba)», pero el de **Linux** no: con `set -e` el guion
    **cortaba con el error crudo de Docker** y la persona no sabía que hacer. Ahora los dos explican (y el
    de Linux sale con 4). Se probó con un **`docker` falso** en el PATH que falla al encender (así se
    prueba el camino de error sin tocar contenedores de verdad), y esa prueba quedó **en el CI**.
    **Comprobado además** (en la misma revisión): **el ZIP del repositorio SÍ conserva el permiso de
    ejecución** de los `.sh` — mi sospecha era falsa, y por eso se mide antes de “arreglar”.

### D-49 · Publicar: se corrige la versión rota, no se sube una nueva (2026-10-28)
**Contexto**: v0.7.6 salió con `task_mappings` apuntando a un modelo borrado y, en vez de corregir esa
versión, se publicó **v0.7.7** como parche. El dueño lo señaló: así el proyecto acumula versiones y
obliga a perseguir la última; peor, cualquiera que haya bajado v0.7.6 se quedó con la rota.
**Regla (de acá en adelante)**:
1. **Nada se etiqueta sin verificar el artefacto real**: no alcanza con la suite en verde; hay que bajar
   el **tarball del tag** y comprobar lo que se publicó (versión, modelos, archivos clave). El
   instalador de un solo archivo baja ese tarball, así que **lo que no está verificado ahí, no existe**.
2. **Si la versión publicada está rota, se corrige EN LA MISMA VERSIÓN** mientras no haya usuarios
   reales: se borra el release y el tag, se rehace con el commit corregido y se vuelve a verificar. **No
   se sube un número nuevo para tapar un error del anterior.**
3. **Una versión nueva es sólo para cambios sobre una base que ya funciona**: features, mejoras o
   cambios de comportamiento que el dueño pidió, no parches de algo recién publicado.
4. **Después de publicar, la verificación es del artefacto**: `curl` del tarball del tag y `grep` de lo
   crítico (versión, `config/`, el guard). Si eso falla, la publicación NO terminó.
**Prevención automática**: el bug de v0.7.6 lo habría atrapado
`tests/test_axioma_01_config_domain.py` (verifica que `task_mappings` no apunte a modelos inexistentes),
agregado el 2026-10-28. Esa comprobación corre en la suite **antes** de cualquier tag.

### D-50 · El aislado del sandbox no copia el workspace entero (2026-10-28, v0.7.7)
**Contexto**: en `STRICT`, `SandboxExecutor` copiaba `cwd` a `/tmp` para que el proceso sólo viera una
copia efímera. Sin tope ni exclusiones, un `terminal_exec` desde la raíz copió el repo completo
(`venv_axioma` 2,8 GB incluido) en **cada comando**: **7,4 GB** y `/tmp` (tmpfs de 7,4 GB) al 100 %,
con el equipo inutilizable hasta borrar a mano. Y cuando la copia no se hacía, `work_dir` se pisaba con
el temporal vacío, así que el comando no encontraba sus archivos.
**Decisión**: `max_copy_bytes` (32 MB) + patrones excluidos; si el árbol supera el presupuesto **no se
copia** y se ejecuta en el cwd real (aislado por límites de recursos, no por copia), con aviso en el
log; el cwd se pasa **absoluto**. Sigue habiendo copia para árboles chicos (el caso que la necesitaba:
`PromotionGate._analyze_external()` con imports relativos).
**Verificación**: `/tmp` en 22 MB antes y después de un `terminal_exec` desde la raíz; `pwd` devuelve
el repo; 4 tests nuevos en `test_07` (27 passed).

### D-51 · El embedder se descarga por inactividad, pero sólo bajo presión de RAM (2026-10-28)
**Contexto medido**: `F2LLM-v2-330M` deja el proceso en **1033 MB de RSS** y, hasta hoy, no se
descargaba nunca (`unload()` sólo se llamaba al salir). Clasificar una consulta cuesta **1 ms** (la
caché L1/L2 del router ya cubre los repetidos), así que el costo real no está en el ruteo sino en la
RAM retenida.
**Decisión**: `EmbeddingModelSingleton.maybe_unload()` descarga si **las dos** condiciones se cumplen:
inactividad ≥ `AXIOMA_EMBED_IDLE_UNLOAD_S` (900 s) y RAM usada ≥ `AXIOMA_EMBED_UNLOAD_RAM_RATIO`
(0.80). Un hilo daemon revisa cada 60 s. Con RAM holgada **no** descarga (no se castiga la latencia).
**Medición**: libera **478 MB** (1033 → 555 MB); el resto son librerías de torch ya mapeadas que no se
pueden devolver en proceso (`malloc_trim` sólo suma 54 MB: no se usa). Recarga: **2,4 s**.
**Descartado con datos**: conectar la caché semántica L3 del router — está construida pero **nadie la
consulta** (`_l3` no tiene llamadas) y clasificar cuesta 1 ms, así que no aportaría nada.

### D-52 · `terminal_exec`: allowlist por nombre exacto, no blocklist (2026-10-28)
**Contexto**: la herramienta tenía blocklist por substrings (`python`, `curl`, `sudo`…). Una auditoría
externa mostró que **no alcanza**: `env id`, `timeout 5 id`, `nice id`, `setsid id`, `nohup id`,
`stdbuf -o0 id`, `awk 'BEGIN{system("id")}'` y `perl -e` ejecutaban código; `cp`/`mv`/`tar` escribían
en `/tmp`; `cat .env` y `../` leían secretos; `find -exec` ejecutaba comandos.
**Decisión**: permitir por **nombre exacto** (utilidades de lectura + `pkill`/`pactl`/`wl-*` que usa el
`SystemAgent`, verificado en su mapa de comandos), rechazar **lanzadores** (aunque estén en la lista) y
revisar los argumentos: rutas resueltas dentro de `Paths.ROOT`, archivos sensibles prohibidos y
banderas de ejecución/escritura (`-exec`, `-delete`, `-fprint`, `-output`). La blocklist queda como
segunda capa.
**Costo asumido**: comandos que no estén en la lista se rechazan con el mensaje de los permitidos
(antes pasaban si no matcheaban la blocklist). `sed`/`awk`/`git` quedan fuera a propósito: pueden
ejecutar o escribir.
**Verificación**: 16 bypasses bloqueados + `ls`/`pwd`/`cat` y los comandos del `SystemAgent` siguen
andando (4 tests en `test_07`).

### D-53 · `11434` sólo en loopback y contenedores sin privilegios nuevos (2026-10-28)
**Contexto medido**: `ss -ltn` mostraba `0.0.0.0:11434` — el motor de Ollama del perfil `ollama-local`
quedaba accesible **sin autenticación** desde cualquier equipo de la red.
**Decisión**: publicar `127.0.0.1:11434` (el contenedor de AXIOMA usa `network_mode: host`, así que lo
sigue viendo en localhost), `cap_drop: [ALL]` y `no-new-privileges` en `ollama`, y
`no-new-privileges` en `axioma`. `docker compose config` validado.
**Nota**: para exponerlo a propósito a la LAN hay que editar el compose (queda escrito en el propio
archivo), no es un default.

### D-54 · Con la web expuesta, el token se exige en TODA la API (2026-10-28)
**Contexto medido (auditoría)**: el token sólo protegía `/v1/preflight/claves` y `/v1/preflight/modelos`.
Desde la LAN y sin token respondían 200: lectura de sesiones/trazas/estadísticas y **acciones**
(chat, borrar sesión, RAC y `preflight/instalar`, que descarga y levanta contenedores).
**Decisión**: middleware global que, cuando el bind no es loopback, exige token en todo salvo
`/v1/health`, `/login`, estáticos y docs. Acepta header `X-AXIOMA-Token`, cookie `axioma_token` (la
deja `/login`) o `?token=` en la primera visita. Sin token configurado y expuesto ⇒ 403 (AXIOMA lo
genera al arrancar expuesto).
**Límite conocido**: el middleware HTTP no intercepta el WebSocket de NiceGUI; el HTML que lo inicia
sí queda detrás del token (sin página no hay sesión válida). Anotado para si se expone UI a propósito.

### D-55 · Cortafuegos de SSRF en el fetch de resultados (2026-10-28)
**Contexto**: `_fetch_page_content` seguía redirecciones sin validar el host y leyó servicios locales
(`127.0.0.1`, `localhost`). **Decisión**: resolver el nombre y rechazar loopback, privadas, link-local
(incluida la IP de metadatos), reservadas y multicast; seguir hasta 5 redirecciones validando **cada**
salto. **Verificación**: 7 destinos internos bloqueados, dominios públicos siguen pasando.

### D-56 · Tope de subida y gate de inyección sobre contenido web (2026-10-28)
**Contexto**: `/v1/upload_file` aceptó 70 MB sin auth en modo expuesto; y el gate anti-injection sólo
miraba el input directo, no el texto traído de la web. **Decisión**: tope de 25 MB
(`AXIOMA_UPLOAD_MAX_MB`) escribiendo por trozos, y el contenido descargado pasa por el mismo gate
(normalización de invisibles/acentos/leetspeak + formas compactas + base64); si la página trae
instrucciones dirigidas al modelo, se descarta como fuente.

### D-57 · Desinstalar conserva tus datos por defecto (2026-10-28)
**Contexto**: no había forma de deshacer una instalación a medias; el usuario pidió poder "empezar de
forma limpia". Borrar todo por defecto sería destructivo (conversaciones en `data/` y claves en `.env`).
**Decisión**: el desinstalador (`.ps1`/`.sh`/`--desinstalar` del `.exe`) borra contenedores, imágenes y
accesos directos, y **mueve** `data/`, `logs/` y `.env` a `~/axioma-datos-<fecha>` antes de borrar la
carpeta. Con `-Todo`/`--todo` borra también esos datos y los volúmenes (modelos). Siempre pide
confirmación salvo `-Si`/`--si`, y `-Ensayo`/`--dry-run` muestra el plan.
**Verificación**: tests con `docker` falso (no se toca el Docker real): la carpeta desaparece, el `.db`
y el `.env` quedan en el respaldo, y el modo ensayo no ejecuta nada.

### D-58 · Coherencia entre Dockerfile, requirements y compose (2026-10-29)
**Contexto**: una auditoría (Qwen) encontró tres desincronizaciones que no rompen los tests pero sí el
producto: el `Dockerfile` fijaba `torch==2.11.0+cpu` y `sentence-transformers==5.4.1` mientras
`requirements.txt` usa `2.14.1+cpu` y `6.2.0`; y el compose declaraba `HF_HOME` en `/app/cache` sin
montar `./cache`, así que el contenedor se bajaba el modelo de embeddings en cada arranque.
**Decisión**: el `Dockerfile` **copia los pines de `requirements.txt`** (hay un test que compara
ambos archivos) y `axioma-puente` monta `./cache:/app/cache`. Además: `/analyze_file` corre la
extracción y el LLM en un hilo (`asyncio.to_thread`) para no bloquear la web, `import resource` va
protegido (Windows), las dependencias sólo-Linux llevan marcador `sys_platform`, y la extracción del
tarball valida miembros en Python < 3.12 (CVE-2007-4559).
**Verificación**: 6 tests nuevos (hilo, Dockerfile, compose, marcadores, tar malicioso, cierre de la
caché) + suite completa.

### D-59 · Diagnóstico previo en el instalador y `DOCKER_CONFIG` global (2026-10-30)
**Contexto**: la instalación en Windows falló dos veces por el **ayudante de credenciales** de Docker
(`docker-credential-desktop` fuera del PATH): afecta a pull, build y up. Los guiones ya reintentaban
por comando, pero el error seguía apareciendo (build) y el usuario no tenía forma de saber qué pasaba
antes de esperar la descarga.
**Decisión**: el `.exe` (1) detecta el ayudante ausente y define un **`DOCKER_CONFIG` temporal sin
`credsStore` para todo el proceso** (las imágenes son públicas), que heredan los instaladores; y (2)
hace **comprobaciones previas** —Docker, espacio ≥ 15 GB, WSL 2 en Windows, credenciales— frenando sólo
ante críticos. Además `--solo-diagnostico` genera el reporte completo y sale sin instalar.
**Verificación**: 3 tests con Docker falso (OK ⇒ 0; falla ⇒ 4; config temporal sin `credsStore` y sin
tocar nada si el ayudante existe).

### D-60 · Sin `--profile`: se nombran los servicios (2026-10-30)
**Contexto**: en un Windows real con Docker 29.8.2, `docker compose --profile puente up -d ...` falla
con `unknown flag: --profile`; el paso 6 no arrancaba y el mensaje quedaba tapado por el error de
credenciales de la primera vuelta. Las versiones de Compose con las que se probó antes sí lo aceptaban.
**Decisión**: no usar `--profile` en ningún guion. Se pasan los **nombres de servicio** explícitos
(`axioma-puente` y, si hace falta, `ollama`), y Compose habilita el perfil del servicio nombrado
(comportamiento documentado y estable). `down` va sin perfiles: baja todos los contenedores del
proyecto. Aplica a lanzadores (`.ps1`/`.sh`), desinstaladores y diagnóstico.
**Verificación**: tests de contrato (el lanzador de Windows sin `--profile`, con los servicios
nombrados; el ensayo de Linux muestra `up -d axioma ollama`) + suite completa.

### D-61 · Detectar Compose en vez de asumir `docker compose` (2026-10-30)
**Contexto**: en un Windows real, `docker compose --profile ...` falló con `unknown flag: --profile` y
después `docker compose up -d ...` con `unknown shorthand flag: 'd' in -d`. Investigado en internet: es
el **fallthrough del CLI** cuando falta el plugin **Compose v2** — `docker` recibe los argumentos y
parsea el primer flag como propio. No es un problema de flags nuestros.
**Decisión**: los lanzadores detectan el Compose disponible (`docker compose version`, si no
`docker-compose version`) y usan ése; si no hay ninguno, mensaje accionable y salida 4. El diagnóstico
del `.exe` lo comprueba antes de descargar (crítico). No se depende de `--profile` (D-60).
**Verificación**: tests con `docker`/`docker-compose` falsos (sin Compose ⇒ 4 y mensaje; con v1 ⇒ sigue
y dice "docker-compose v1"); suite completa.
Fuente del fallthrough: https://github.com/multica-ai/multica/commit/9c1d8d265974447bca5d2fccf6bc3f9031fd684b

### D-62 · La pantalla de modelos muestra el CATÁLOGO, no sólo los instalados (2026-10-31)
**Contexto**: la pantalla *Configurar AXIOMA* ofrecía para cada rol únicamente los modelos **ya
instalados**. El usuario no podía ver qué otras opciones existen según su equipo ni seguir la descarga
de las que faltan (sólo había un "descargar los que faltan" global, sin detalle).
**Decisión**: dentro de *Configurar AXIOMA* hay un botón **🧠 Modelos** que lista el catálogo medido
(`config/model_catalog.yaml`): rol, para qué sirve, disco, RAM, estado y **Instalar con barra de
progreso** por modelo. El catálogo se amplió con variantes para poca memoria y razonamiento. El
"Salir" cierra la ventana porque el lanzador abre en modo app (una pestaña normal no se puede cerrar).
**Verificación**: tests del catálogo (≥6 opciones con rol/tamaño/RAM), de la lista/estado, del botón
dentro de Configurar AXIOMA, del progreso (`pull_model`/`linear_progress`) y del cierre de ventana.

### D-46 · Un solo modelo para chat y visión: `qwen3.5:9b` (2026-10-27, v0.7.6)
**Por qué**: el usuario pidió analizar si el modelo de chat/juez/razonamiento podía ser uno solo y
ahorrar un VLM. **Medido en su equipo** (12 núcleos, CPU, 14 GiB): `qwen3.5:9b` es **multimodal
nativo** y rinde igual que `qwen3:8b` en velocidad (6,4 vs 6,5 tok/s); en visión describe texto,
formas **y colores** (el 4B omitía colores). Un modelo menos = 1,9 GB menos en disco y un solo
modelo residente (7,5 GB).
**Qué se decidió**: `llm_default_model` y `llm_vision_model` apuntan los dos a `qwen3.5:9b`
(`DEFAULT_MODELS` **fusiona** las `task_types` de ambos roles: un dict con la misma clave dos veces
pierde la primera). Se borraron `qwen3:8b` y `qwen3-vl:4b`. El "thinking" queda **apagado** y se
**reintenta una vez con `think:true`** si la respuesta vuelve vacía (con `num_predict` chico el
razonamiento se comía el presupuesto y devolvía `""`).
**Verificación**: batería de 7 tareas en castellano, 6/7 en ambos modelos; razonamiento sin thinking
acertó con 8B y falló con 9B, y **con thinking acertaron los dos** (760/671 tokens). Visión: "ERROR
42" + círculo rojo + cuadrado azul. `tests/test_axioma_35_thinking_retry.py` (8 tests, herméticos).

### D-47 · Arranque en Windows: reintento sin el ayudante de credenciales (2026-10-27, v0.7.6)
**Contexto**: una instalación real (v0.7.5) frenó en el **paso 6 de 6** con `error getting
credentials - err: exec: "docker-credential-desktop": executable file not found in %PATH%`. Docker
Desktop tenía `credsStore: desktop` y el ayudante fuera del PATH; el instalador ya lo esquivaba para
bajar **su** imagen, pero `docker compose up` además baja `ollama/ollama` y moría ahí.
**Qué se decidió**: los lanzadores (`iniciar_axioma.ps1` y `.sh`) **reintentan una vez** con un
`DOCKER_CONFIG` temporal (`{"auths":{}}`) cuando la salida contiene `credential`. Las imágenes del
compose son públicas, así que no hace falta credencial. Los guiones de Windows **no llevan emojis**
(la consola CP850 los imprime como `?`) y conservan `catch { $encendido = $false }` (lo exige
`test_27`).
**Verificación**: en Linux con un `docker` falso que emite ese error y sólo funciona con una config
sin credenciales: primer intento falla, reintento **enciende** (`up_ok=1`). El `.ps1` no se pudo
ejecutar acá (no hay `pwsh`): queda la prueba real en Windows.

### D-48 · Sin servidor de audio se degrada, no se rompe (2026-10-27, v0.7.6)
**Contexto**: el CI se cayó al importar módulos multimedia con `PortAudioError: ... 'PulseAudio_
Initialize: Can't connect to server' [PaErrorCode -9999]`. `import sounddevice` **inicializa**
PortAudio al importarse y esa excepción **deriva de `Exception`, no de `OSError`**, así que los
guardas `except (ImportError, OSError)` (puestos en la pasada de "PortAudio opcional") no la
atrapaban. El propio test `test_30` tenía el mismo guarda y **fallaba** en vez de saltearse.
**Qué se decidió**: los 5 guardas de `sounddevice` y el del test pasan a `except Exception`, con el
motivo escrito y logueado; sin audio se desactivan voz/VAD/wake-word y el resto funciona.
**Verificación**: se **simula** el entorno del CI con un `MetaPathFinder` que lanza ese
`PortAudioError` al importar `sounddevice`; los 8 módulos afectados importan y la suite da **27/27**
con gate **15/15** en ese entorno.

### D-20 · Revisión del plan de Qwen3.8 (2026-10-07): qué se verifica y qué se prioriza
El usuario dejó un plan de 5 fases en `mem_axioma/respuestas_qwen.md`. **Se leyó y se verificó punto por
punto sin tocar código** (revisión completa en `docs/REVISION_PLAN_QWEN.md`; ese documento **fue
eliminado en la Fase 3** y sus conclusiones quedan resumidas en esta D-20).

**Confirmado con medición** (el plan tiene razón):
1. **La UI queda expuesta a la red local**: `ss -ltn` → `0.0.0.0:8080 LISTEN`, `network_mode: host` y el
   `CMD` con `--host 0.0.0.0`, **sin autenticación**. Corrige algo que yo mismo dije mal: lo que no se
   expone es **Ollama** (sigue en `127.0.0.1`), pero la **UI sí**.
2. **`storage_secret` fijo** (`'axioma_secret_key_change_in_production'` en `app.py` y `server.py`).
3. **`AXIOMA_SIN_AUDIO` y `AXIOMA_EN_CONTENEDOR` son banderas MUERTAS**: sólo se declaran
   (`Dockerfile:62`, `docker-compose.yml:52/86`) y **ningún** `.py` las lee → rompen la regla **R1** del
   proyecto, y mi comentario «la voz se desactiva sola» es **falso**.
4. **UID/GID de audio y socket de Pulse**: `group_add` usa `${AXIOMA_AUDIO_GID:-996}` pero nadie lo
   calcula (Debian/Ubuntu usan **29**, Fedora **63**), y el socket está fijo en `/run/user/1000/pulse`.
5. **Asimetría de Python**: venv **3.14.7** vs imagen **3.12.15**, con el job 3.14 del CI en
   `continue-on-error` (aunque hoy **pasa en verde**: verificado en la API).

**Corregido del plan** (lo que decía mal o no midió):
- «Las claves se pierden con `docker compose down`» → **falso**: viven en el `.env` del equipo y el compose
  las inyecta con `env_file`; `down` no borra archivos del host (y montar `.env` adentro sería peor).
- «768 tests» → hoy son **1080**.
- «0 de 195 distribuciones excluyen 3.14 ⇒ no hay bloqueantes» → es una **inferencia**, no una medición: se
  mide compilando la imagen en 3.14 y corriendo la suite adentro.

**Orden propuesto**: (1) seguridad de la UI + `storage_secret` aleatorio, (2) banderas muertas, (3) UID/GID
y socket dinámicos, (4) migración de la imagen a 3.14 (medida antes de tocar el CI), (5) `user:` en el
compose para `data/` y `logs/`, (6) fakes de Ollama. **Nada de esto se tocó todavía.**

20. **Revertí mi propio cambio con `git checkout`**: al deshacer una mutación de prueba usé
    `git checkout src/interfaces/web/voice_runtime.py`, y eso devolvió la versión **del último commit**:
    el cambio de la fase 2 (que todavía no estaba commiteado) desapareció. Lo noté porque la mutación
    siguiente ya no fallaba y lo repuse. → Para deshacer una mutación se usa **el respaldo del archivo**
    (`cp archivo /tmp/…` antes de mutar y `cp` atrás después), nunca `git checkout` sobre trabajo sin
    commitear. Es la misma familia del error 10 (respaldar antes de corregir).

24. **Di por bueno un «hallazgo» de una respuesta externa sin verificar el remedio**: al revisar la
    segunda respuesta de Qwen marqué la duplicación de `TaskStatus` como «el mejor hallazgo, y es real», y
    al ir a implementarlo medí en serio: son **tres máquinas de estado distintas** (tablero de
    coordinación, cola de trabajo, orquestador ajeno) con **consumidores distintos** y estados que no se
    solapan (`REFUSED_OOM` vs `CANCELLED/TIMEOUT`). Unificarlas habría sido **peor que dejarlas**. →
    Lección: no alcanza con verificar la **observación** (¿hay tres clases con el mismo nombre? sí);
    hay que verificar el **remedio** (¿son la misma cosa? **no**). El usuario lo pidió con estas palabras:
    «asegurate de que lo que se va a implementar sea lo correcto y no un error de análisis o cálculo».

22. **Di por cerrada una brecha con la medición equivocada**: al arreglar las pruebas que fallaban dentro
    de la imagen verifiqué con el **proyecto montado** (`-v $PWD:/app`) y dije «brecha 1 arreglada». El
    usuario corrió la prueba **completa** y las 3 pruebas seguían fallando: la causa real era otra (7
    pruebas leen `docker-compose.yml`, que **no viaja dentro de la imagen**). → **Una brecha no está
    cerrada hasta que la corrida completa lo dice**, y el escenario que se mide tiene que ser el mismo que
    el real (imagen compilada ≠ proyecto montado). Lo detectó el usuario, no yo.
23. **Dos veces edité documentación con anclas viejas**: el script de documentación falló porque el
    texto que buscaba ya no era el del archivo (le faltaba o le sobraba un espacio), y como no usé `&&`
    entre los pasos, la publicación siguió sin los documentos. Peor: una edición mal cerrada **rompió el
    bloque de código del manual de Rafael** (quedó un `_(sigue)_` suelto y un ` ``` ` de más), y lo vi
    recién al leer el resultado. → El ancla se **copia del archivo real** (no de la memoria), cada
    reemplazo se **cuenta** (si no aparece exactamente una vez, el script corta) y después de editar
    documentación se **lee el resultado**. Misma familia que el error 14 (un cambio que no se verifica no
    está hecho).


25. **Una limpieza por palabras en documentación hace más daño que la suciedad que saca.** Metí
    `saintwick|beer_vgb_mobile|docs/` en la lista de borrado del saneador y el resultado, medido en el diff:
    se llevó la **atribución de autoría** y el **enlace real del repositorio**, **cortó una frase al medio**
    (dejó «(regla R13 — ver …). Se sumaron el canal de imágenes (visión +» sin continuación) y borró el
    **encabezado de una versión** del CHANGELOG dejando sus viñetas colgando de la anterior. → En
    documentación se borra una **línea entera verificada de a una** (o se reemplaza por un texto pensado); la
    regla por palabra queda sólo para **comprobar**, nunca para borrar. Y después de sanear hay que **leer el
    diff completo**, no el informe del guion: los números que imprimía (`-77 en total`) tapaban el daño.
26. **Una verificación que no puede fallar no verifica nada.** `preparar_publicacion.sh` acumulaba `faltas=1`
    e imprimía «❌ lo publicado nombra herramientas que no se publican», pero el guion **terminaba en 0**:
    la publicación seguía como si nada. → Ahora **sale con error 1** (`exit "$faltas"`), y lo comprobé
    **provocando el fallo**, no leyendo el código. Lo mismo con la limpieza nueva: si el texto cambió, el
    paso **corta** en vez de saltearse en silencio.
27. **Marcas de privacidad mal puestas (y anidadas).** Para sacar las secciones del harness marqué **sólo el
    encabezado** —tomé por «fin» el separador que va justo debajo— así que el **cuerpo quedó publicado**
    (`cd deepseek-harness`, el enlace al repositorio). Y al rehacerlo dejé un par **anidado** (la nota del
    token dentro del bloque grande), que el saneador cazó al instante con «hay una marca de fin sin su
    inicio». → Una marca cubre el **bloque entero** (separador de arriba, encabezado, subrayado y todo el
    contenido) y hay que **mirar el archivo publicado**, que es lo que ve el usuario. El chequeo de marcas
    balanceadas funcionó: cortó la publicación en vez de dejarla pasar.


28. **Limpié de más y rompí una prueba publicada: probé el saneador, no la copia publicada.** Saqué de la
    copia pública la mención a `deepseek-harness` en `.dockerignore` y `Dockerfile` (cosmético: es un
    proyecto de código abierto, no un dato privado) y el CI quedó **ROJO**. La causa se reprodujo en **un
    intento** corriendo las dos pruebas que tocan esos archivos **en el clon público**:
    `test_el_contexto_de_compilacion_deja_fuera_lo_pesado_y_lo_ajeno` **exige** que `.dockerignore` excluya
    `deepseek-harness/`, y mi limpieza le borraba la línea. → **Reverté la limpieza, no la prueba**: una
    guarda que se rompe por estética de la documentación está avisando de algo. Y la regla que faltaba:
    después de tocar el saneador hay que correr la suite **en la copia publicada** (que es lo que corre el
    CI), no sólo en el proyecto; el fallo estaba ahí y no se veía desde el privado.


29. **La prueba de navegador encontró lo que 1.080 pruebas unitarias no vieron.** La sección de modelos
    tenía una prueba que verificaba la lista de instalados, los cuatro roles en el código y la regla R7 —
    todo verde —, y en pantalla el rol de código salía **vacío**. → Cuando lo que se muestra es una
    **configuración**, hay que comprobar **el valor que muestra**, y contrastarlo con la fuente (acá: la
    pantalla contra la API y contra Ollama). «La lista cargó» no es «la pantalla dice la verdad». Y una
    captura guardada no verifica nada por sí sola: el contraste se hace con datos.


30. **Una espera fija en una prueba de navegador inventa fallos.** Mi comprobación del cierre esperaba
    1,5 s y reportó «quedó algo tapando»: al medir de nuevo, el diálogo **sí** cerraba — el botón Cerrar
    queda abajo (medido: **y = 1582** en una ventana de 900) y la animación de Quasar tardaba más. → En
    pruebas de navegador se espera **a que pase el hecho** (acá: hasta 10 s a que no queden diálogos
    visibles), no un tiempo adivinado. Y hay que revisar el falso fallo antes de «arreglar» lo que no está
    roto: casi toco el componente por un problema de la prueba.
31. **Verificar que el campo exista no es verificar que se vea donde debe.** La prueba visual de la sección
    de modelos leía los valores de los 4 campos, los comparaba con la API y con Ollama… y todo daba ✅
    mientras los campos se dibujaban **en la página y no en la pantalla**, que es exactamente el defecto
    que el usuario vio. → Cuando algo se muestra, la comprobación incluye **dónde** (¿dentro del diálogo,
    dentro de la tarjeta?), no sólo **qué dice**. Lo encontró el usuario usando el producto, no la prueba.


32. **Repetí un error ya documentado: el emoji en los `.ps1`.** La consola de Windows imprime mal los
    pictogramas y el proyecto lo prohíbe (permitido: Latin-1, puntuación general y líneas de caja). Lo había
    cometido antes y volví a cometerlo al escribir el bloque nuevo de los instaladores; encima, editar esos
    archivos con las herramientas les **borra el BOM**, que PowerShell 5.1 necesita para los acentos. Las dos
    cosas las cazó la suite (`test_axioma_27_portabilidad.py`), pero me costó dos corridas enteras. → Un
    error repetido no es un descuido: es que **no tenía un paso que lo evite**. Ahora, después de tocar
    cualquier `.ps1`, se corre ese archivo de pruebas **antes** de seguir (y no al final).
33. **Una preferencia del usuario que pierde contra un valor fijo es peor que una preferencia que falta.**
    El modelo elegido se guardaba en el `.env`, se mostraba en la pantalla y el preflight lo respetaba… y en
    ejecución **perdía** contra `task_mappings` (que se consulta primero). Todo «parecía» configurado. → Lo
    que el usuario elige tiene que llegar al lugar donde se **decide**; si hay un valor fijo que gana, la
    configuración es una ilusión (misma familia que el error 24: verificar la observación **y** el efecto).
34. **Una configuración sin efecto es deuda silenciosa.** `vision_model_selector` se declaraba, se guardaba,
    se reportaba en los ajustes… y sus dos ramas devolvían lo mismo: no hacía nada desde hacía versiones.
    Nadie lo notó porque «existía». → Cuando un campo de configuración se toca, se comprueba que **cambie
    algo**; si no cambia nada, se saca o se deja dicho por qué está.

---

## Pendientes (con lo que falta para cerrarlos)

| Pendiente | Qué falta |
|---|---|
| ~~**`ResourceWarning` de SQLite al cerrar**~~ | **Cerrado el 2026-10-25** (commit `81ff91e`): **49 → 0** conexiones abiertas al apagar y `ResourceWarning: unclosed database` **60 → 0**. `with sqlite3.connect(...)` **no** cierra (hace commit/rollback): los 5 usos reales pasaron a `with closing(connect(...)) as conn, conn:`, `close_all_pools()` se llama en `Dispatcher.cleanup()` con `atexit` de red y cada pool usa `weakref.finalize` |
| ~~**22 variables F841**~~ | **Cerrado el 2026-10-25** (commit `220d8ae`): 22 corregidas (`ruff --select F841` 28 → 5). Quedan **5 intencionales** que no se tocan: `stm`/`ltm`/`sm`/`gw` en `src/memory/__init__.py:118-142` y `loop` en `src/multimodal/tts_engine.py:406` |
| **F401 en `tools/`** | **Vivo**: `ruff --select F401 tools/` da **136** hallazgos (126 auto-fixables). La limpieza de esta semana cubrió `config/`, `tools/auditor/*` y dos herramientas; el resto de `tools/` quedó fuera del alcance |
| **`.exe` del v0.7.5 sin probar en Windows real** | **Vivo** (D-44): el CI construye `AXIOMA-Setup.exe`, verifica que arranca y lo publica en el Release; falta ejecutarlo en una PC Windows (instalación, accesos directos, Docker Desktop y voz) |
| ~~**Lo que encontró el informe de Sonnet**~~ | **Cerrado el 2026-10-10** con D-39: Windows sin `.env` ni carpetas (bloqueante), los mapeos de tarea que ignoraban el modelo elegido, guardar un modelo que no se aplicaba, sin validación de `Host` y un selector de visión sin efecto. Cada uno con su prueba y su mutación comprobada |
| **Instaladores de Windows** (D-10) | Ejecutarlos en un Windows real; ajustar con lo que salga |
| ~~**Voz en Windows**~~ | **Cerrado el 2026-10-08** con D-27: demonio nativo (`instalar/rafael_windows.ps1`) que habla con la voz de Windows y le manda el texto al contenedor. Falta **probarlo en una PC con Windows** (el CI lo ejecuta en modo ensayo) |
| **Modo paralelo real** | Bloqueado: hace falta un equipo con placa de video utilizable para medir `num_gpu` |
| **Máquina limpia: 2 brechas medidas** (D-29) | `tools/probar_instalacion_limpia.sh` ya existe y corre; falta que las pruebas se salteen cuando no hay `docs/` (4 pruebas fallan en la imagen de una copia limpia) y documentar que el primer arranque en frío necesita internet para el modelo de embeddings |
| **Instalación en una PC sin Docker** | Decidir si Docker queda como requisito manual documentado |
| ~~**Dos hallazgos MEDIUM del auditor** en `src/core/preflight.py`~~ | **Cerrado el 2026-10-10**: quedó la constancia escrita en el código (el plazo es de RED, no de cómputo; el hilo es uno por descarga pedida a mano, cancelable y `daemon`). El auditor los marcaba porque el detector no lee comentarios; **el 2026-10-25 la auditoría ya da 0 MEDIUM** (D-41) |
| ~~**macOS**~~ | **Cerrado el 2026-10-06** con la decisión D-15 |
| ~~**Recomendar modelos por hardware**~~ | **Cerrado el 2026-10-06** con D-16 (`preflight --recomendar` y `--modelo NOMBRE`) |
| ~~**Seguridad de la UI** (fase 1)~~ | **Cerrado el 2026-10-07** con D-21 (local por defecto + secreto de sesión propio) |
| ~~**Banderas muertas** (fase 2)~~ | **Cerrado el 2026-10-07** con D-23 (`AXIOMA_SIN_AUDIO` implementada, `AXIOMA_EN_CONTENEDOR` eliminada) |
| ~~**Audio portable** (fase 3)~~ | **Cerrado el 2026-10-07** con D-24 (UID/GID del equipo calculados por el lanzador y el instalador) |
| ~~**Python 3.14 en la imagen** (fase 4)~~ | **Cerrado el 2026-10-07** con D-22 (imagen y CI en 3.14, medido: 5,6 GB, suite adentro ✅) |
| ~~**Fase 3: repositorio público limpio**~~ | **Cerrado el 2026-10-10** con D-36: se limpió el manual (contradecía al producto), el entorno compartido del autor en `requirements.txt`, las secciones de su entorno en `COMANDOS_AXIOMA.txt`, el enlace colgado a `docs/` y el nombre de su otro proyecto en `.dockerignore`/`Dockerfile`. El saneador pasó de borrar por palabras a borrar **líneas exactas**, y ahora **sale con error** si la verificación falla |
| ~~**Prueba visual (navegador real) de la sección de modelos**~~ | **Cerrado el 2026-10-10** con D-37: la prueba en navegador encontró un error real (dos roles se mostraban vacíos por un nombre de atributo mal armado), arreglado con prueba de regresión. La herramienta ahora también repone el marcador de primer arranque y avisa con claridad si AXIOMA está apagado |
| ~~**`diagnostico_v2.py` en el repositorio público**~~ | **Cerrado el 2026-10-10, con una corrección mía**: afirmé que estaba publicado y **era falso** (nunca estuvo: lo ignora el `.gitignore` que se copia, `/diagnostico_*.py`). Se agregó igual la exclusión explícita como red de seguridad, a pedido del usuario |
| **Permisos de `data/` y `logs/`** (fase 5) | `user:` en el compose; cuidado con que el contenedor siga escribiendo |
| **Fakes de Ollama en el CI** (fase 6) | Prioridad baja (el CI usa un puerto muerto y las pruebas se degradan solas) |
