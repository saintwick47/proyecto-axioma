# 📊 Rendimiento de AXIOMA — mediciones (2026-11-12)

> Objetivo: decidir con números si conviene cambiar de lenguaje, optimizar Python o atacar el LLM.
> Equipo: **CPU-only** (Ryzen 5 8500G), `qwen3.5:9b`, embeddings `codefuse-ai/F2LLM-v2-330M` (896 dims).
> Todo lo de abajo está **medido**, no estimado. Reproducible al final.

## 1. Resultados

| Medición | Resultado |
|---|---|
| **Prefill** con system de 2.474 tokens (arranque frío) | **49,6 s** (~50 tok/s) |
| Turnos siguientes con el mismo system | **~11 s** cada uno (se evalúan ~508 tokens) |
| **Decode** | **5,4 tok/s** (736 tok → 136 s; 1.324 tok → 244 s) |
| **Embeddings — carga** | **6,27 s** |
| **Embeddings — RSS** | **+658 MB** (683 MB total en un proceso limpio) |
| **Embeddings — latencia** | **26,4 ms** por texto (mediana 26,3) |
| **Coseno 168×896 — Python puro** | **11,06 ms** |
| **Coseno 168×896 — numpy** | **0,013 ms** (~850× más rápido) |

## 2. Lectura

1. **El costo dominante es el LLM**: prefill de segundos (11-50 s según contexto) + decode a **5,4 tok/s**.
   Una respuesta de 500 tokens tarda ~92 s solo en generarse.
2. **Lo de Python es ruido**: embeddings (26 ms) + coseno (11 ms) ≈ **0,05 %** de un turno.
   Un lenguaje nuevo **no mueve la aguja**: Rust ya está en `tokenizers`, `pydantic-core`, `orjson`,
   `ruff` y `uv`.
3. **El caché de prefijo ayuda pero no elimina**: el turno frío pagó 2.474 tokens (49,6 s); los
   siguientes bajaron a ~11 s (508 tokens). Cada turno sigue pagando ~11 s de prefill.
4. **`numpy` en el coseno**: 850× más rápido, pero ahorra **11 ms** por consulta. Barato, impacto
   despreciable: **backlog**, no prioridad.

## 3. Decisión

- **No cambiar de lenguaje.**
- **Palanca principal: contexto por turno** (system prompt, historial, chunks recuperados): ahí están
  los 11-50 s de prefill.
- **Tope de salida** (`num_predict`) y respuestas más cortas: a 5,4 tok/s el largo manda.
- **Investigar el caché de prefijo**: por qué aún se evalúan ~508 tokens por turno.
- **`orjson`**: solo donde `py-spy`/`cProfile` muestre JSON caliente (hoy no hay evidencia).

## 4. Límites de la medición

- Una sola corrida por escenario; el equipo estaba haciendo otras tareas.
- El "system largo" es un texto de 10.080 caracteres (no el prompt real de AXIOMA); sirve para
  **escalar** el prefill, no como valor exacto de producción.
- El conteo de tokens del caché (508) es lo que reporta Ollama como evaluado; no se instrumentó
  el KV cache por dentro.
- No se midió el turno completo de AXIOMA (web + router + memoria); se midieron los componentes.

## 5. Cómo reproducir

```bash
# Prefill/decode (dos turnos, respuesta corta)
python - <<'PY'
import httpx, time
largo = ("Sos AXIOMA, un sistema multiagente. Reglas: medir antes de afirmar; no inventar; "
         "responder en castellano; usar herramientas cuando corresponda. " * 70)
for i in range(2):
    t0 = time.time()
    d = httpx.post("http://127.0.0.1:11434/api/chat", timeout=300, json={
        "model": "qwen3.5:9b", "stream": False, "keep_alive": "10m",
        "messages": [{"role": "system", "content": largo},
                     {"role": "user", "content": f"Respondé solo: ok {i}"}],
        "options": {"num_ctx": 4096, "num_predict": 16},
    }).json()
    print(i, d["prompt_eval_count"], d["prompt_eval_duration"]/1e9, d["eval_count"], d["eval_duration"]/1e9)
PY

# Embeddings (carga, RSS, latencia)
venv_axioma/bin/python - <<'PY'
import time, statistics, psutil
from src.memory.embedding_model_singleton import get_embedding_model, EMBED_MODEL_NAME
p = psutil.Process(); r0 = p.memory_info().rss
t0 = time.time(); m = get_embedding_model(); m.load(); print(EMBED_MODEL_NAME, time.time()-t0, (p.memory_info().rss-r0)/1e6)
lat = []
for i in range(20):
    t0 = time.time(); m.embed(f"texto {i}"); lat.append((time.time()-t0)*1000)
print("media ms", statistics.mean(lat))
PY
```

## 6. Próximo paso medible

Acortar el **system prompt** (y el historial) y volver a medir el prefill por turno. Es el único
cambio que puede mover segundos, y se verifica con el mismo comando del punto 5.

## 7. Instrumentación para medir el caché real (2026-11-12)

Con `AXIOMA_DEBUG_PROMPT=1` (o `settings.debug`), cada turno deja dos líneas correlacionables:

- `[PROMPT]` (dispatcher, al armar `messages`): `system_sha1`, `msgs`, `roles`,
  **`msg_hashes`** (un hash corto por mensaje, incluido el último `user`), `hist_head`, `hist_tail`.
- `[OLLAMA]` (cliente, al recibir la respuesta): `method=chat|chat_stream`, los mismos
  `msg_hashes`, `prompt_eval_count`, `prompt_eval_duration`, `eval_count`, `eval_duration`,
  `total`, `load` y `reply_sha1` (hash del `assistant` con el mismo formato que `msg_hashes`).

**Cómo leerlo**: entre dos turnos consecutivos, el **prefijo común** de `msg_hashes` (posiciones
iguales desde el inicio) debería coincidir con `prompt_eval_count` si Ollama reutiliza el caché.
Si el prefijo común es largo pero `prompt_eval_count` es alto, el prefijo se rompe por algo que no
está en los hashes (p. ej. la plantilla del modelo). `reply_sha1` del turno N contra el hash del
mensaje `assistant` en el turno N+1 detecta si el historial se reinyecta distinto.

```fish
AXIOMA_DEBUG_PROMPT=1 venv_axioma/bin/python main.py --web
# 6 turnos sin herramientas + 2 con herramientas; después:
grep -E "\[PROMPT\]|\[OLLAMA\]" logs/*.log | tail -20
```

## 8. Tres brazos de `think` (2026-11-12): no cambiar el default

Guion de 6 turnos encadenados, `num_ctx=2048`, `num_predict=512`, mismo system (con marca de brazo
para aislar cachés), medido con `/api/chat` stream contra Ollama directo:

| Brazo | `prompt_eval` (6) | `eval` (6) | `total` (6) | vacíos | pensamiento |
|---|---|---|---|---|---|
| **A: `think:false` (actual)** | 1.409 | 271 | **106,4 s** (17,7 s/turno) | **0** | 0 |
| B: sin `think` | 781 | 2.879 | 575,6 s (95,9 s/turno) | **3** | 1.124-2.359 chars |
| C: `/no_think` en el system | 644 | 2.717 | 519,0 s (86,5 s/turno) | **4** | 1.874-2.472 chars |

**Conclusión:**

- **No cambiar `think:false`.** Sin `think` (B) o con `/no_think` (C) el modelo **piensa igual**
  (1-2,5 k chars), consume los 512 tokens de `num_predict`, tarda **~5× más** y devuelve respuestas
  **vacías** en 3-4 de 6 turnos. Qwen3.5 en Ollama **no respeta `/no_think`**.
- En A el caché de prefijo **no se reutiliza** (`prompt_eval` crece 89→403), pero el prefill (~5-10 s)
  es menor que el decode del pensamiento (~80 s/turno). El caché queda como optimización aparte
  (posible causa: plantilla Qwen que borra el bloque `<think>` vacío), no como palanca inmediata.
- **Conflicto de `num_ctx` confirmado**: el turno 1 de A cargó el modelo en **15,7 s** porque la
  instancia tenía `num_ctx=4096` y el request pedía 2048 → recarga y pérdida del KV. Conviene que
  **todos** los caminos usen el mismo `num_ctx` (o fijarlo por modelo).

## 9. Cambios aplicados (2026-11-13)

- **`num_ctx` monotónico por modelo**: el cálculo por buckets sigue igual, pero nunca baja del máximo
  ya usado por ese modelo (evita la recarga de ~15 s y la pérdida de KV del ISSUE-086).
- **Historial por bloques de 8**: la ventana se corre de a 8 mensajes alineados (antes 1 por turno).
  Con el guion medido, el primer salto pasa del turno 6 al 9; entre saltos el prefijo es estable.
- **Cómo verificarlo**: en los logs, `load=0` después del primer turno y `msg_hashes` con el mismo
  prefijo hasta el salto (n=17). El caché de prefijo con `think:false` sigue sin reutilizarse (tema
  de plantilla, §8), pero el prefill ya no paga recargas.

## 10. Verificación en vivo de 9 turnos (2026-11-13)

Instancia aparte, `AXIOMA_DEBUG_PROMPT=1`, sesión nueva, 9 turnos encadenados por la UI:

| Turno | msgs | prefijo común | num_ctx | load | prompt_eval | prefill |
|---|---|---|---|---|---|---|
| 1 | 2 | – | 2048 | 0,0 | 92 | 3,65 s |
| 2 | 4 | 2 | 2048 | 0,0 | 135 | 4,65 s |
| 3 | 6 | 4 | 2048 | 0,0 | 220 | 5,59 s |
| 4 | 8 | 6 | 2048 | 0,0 | 300 | 6,86 s |
| 5 | 10 | 8 | 2048 | 0,0 | 390 | 8,93 s |
| 6 | 12 | 10 | 2048 | 0,0 | 463 | 10,28 s |
| 7 | 14 | 12 | 2048 | 0,0 | 549 | 12,52 s |
| 8 | 16 | 14 | 2048 | 0,0 | 645 | 14,54 s |
| 9 | 18 | 16 | 2048 | 0,0 | 734 | 15,78 s |

**Resultado:**

- **`num_ctx=2048` estable y `load=0,0` en todos los turnos**: la recarga de ~15 s desapareció
  (D-75). La instancia ya estaba cargada en 2048 y nunca se le pidió menos.
- **El prefijo crece de a 2 por turno hasta el 9 (16 mensajes)**: la ventana por bloques mantiene el
  comienzo estable; el primer salto (`_inicio_bloque(17)=8`) cae en el **turno 10** (por eso 9 turnos
  no lo muestran). Al saltar, el prefijo se rompe una vez y se re-evalúa todo (tradeoff de D-74).
- **El caché sigue sin reutilizarse** (`prompt_eval` 92→734, ~el prompt completo): es el tema de
  plantilla/`think:false` (§8), independiente de estos cambios. El prefill crece con el prompt
  (3,6→15,8 s), pero ya no hay recargas.
- Duraciones de turno en la UI: 36 s el primero, 82-92 s los siguientes (respuestas breves + prefill).

