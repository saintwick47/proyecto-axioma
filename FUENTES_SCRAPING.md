# 🕷️ Fuentes de scraping (datos sin API)

> Actualizado: 2026-11-08. Código: `src/handlers/scraping_fuentes.py` ·
> Configuración: `config/fuentes_scraping.yaml` · Verificador: `tools/verificar_scraping.py`.

## Para qué

Hay datos que las APIs gratuitas no cubren. Ejemplo medido: el **riesgo país de países distintos de
Argentina**. La serie EMBI de Brasil en IPEADATA está **inactiva** y el BCB **dejó de publicarla**; los
bancos centrales de Chile/Perú/Colombia piden token o publican PDFs. Para esos casos AXIOMA puede leer
una página pública y extraer el número.

## Dependencias: NINGUNA nueva

Se usan cosas que **ya están** en `requirements.txt` y en la imagen:

| Paquete | Para qué | Versión declarada |
|---|---|---|
| `httpx` | bajar la página | `0.28.1` |
| `lxml` | disponible si hace falta parsear | `6.1.3` |
| `PyYAML` | leer `fuentes_scraping.yaml` | `6.0.3` |

**No se agregó Scrapy** (reactor, dependencias y arranque pesado para un solo GET) ni Playwright (levanta
un navegador: segundos de latencia). El extractor usa **regex** sobre el HTML; `lxml` queda disponible.

## Cómo agregar una fuente (sin tocar código)

Editá `config/fuentes_scraping.yaml`:

```yaml
fuentes:
  - nombre: "Brasil — sitio X"
    pais: "brasil"                     # como se busca; "*" sirve para todos
    url: "https://sitio.example/brasil"
    patron: '(?P<valor>[0-9]{2,5})\s*(?:puntos|pts|bps)'
    nota: "EMBI+ Brasil"
```

- `patron`: regex; el número **tiene que** quedar en el grupo `(?P<valor>…)`.
- Si el patrón no matchea o la página falla, la función devuelve `None` y el chat dice la verdad
  (nunca se inventa un número).
- **Caché 15 min** en memoria y **timeout 5 s**: no suma latencia perceptible al chat.

## Verificarla antes de confiar

```bash
venv_axioma/bin/python tools/verificar_scraping.py
venv_axioma/bin/python tools/verificar_scraping.py --pais argentina --timeout 10
```

Muestra `país: valor · fuente · ms` y sale con código 4 si alguna fuente falla. Medido el 2026-11-08 con
la fuente de Ámbito: **940 puntos en 51 ms** (y la API daba 939 el mismo día).

## Fuente real incluida (y su límite)

`config/fuentes_scraping.yaml` trae **Ámbito — riesgo país** (Argentina) como ejemplo real:
el número aparece en el texto de la nota (*"…rozó los 940 puntos"*), no en una tarjeta estable.
Funciona hoy, pero es **frágil**: si cambian la redacción, deja de matchear y se cae al aviso.
Para Argentina la fuente primaria sigue siendo la **API** de argentinadatos; el scraping es respaldo
y prueba del mecanismo.

## Pruebas

| Prueba | Qué cubre |
|---|---|
| `test_scraping_extrae_el_numero_y_cachea` | extracción + caché (HTML simulado) |
| `test_scraping_sin_match_devuelve_none` | patrón que no matchea → `None` |
| `test_la_fuente_configurada_matchea_un_html_real` | el patrón del YAML sobre texto real de Ámbito |
| `test_scraping_en_vivo_si_se_pide` | red real, **solo** con `AXIOMA_SCRAPING_LIVE=1` |

```bash
venv_axioma/bin/python -m pytest tests/test_axioma_13_finance_dolar.py -q
AXIOMA_SCRAPING_LIVE=1 venv_axioma/bin/python -m pytest tests/test_axioma_13_finance_dolar.py -q -k en_vivo
```

## Confiabilidad (leer antes de usarlo en producción)

- **Sí** para datos donde una página pública y estable los publica (verificado con Ámbito).
- **Frágil por naturaleza**: un rediseño o cambio de redacción rompe el patrón. El verificador existe
  justamente para detectarlo.
- **Nunca** se usa como única fuente de algo crítico: el llamador debe tener un aviso/respaldo.
- Respetar los términos del sitio; una consulta cada 15 min (caché) es un uso razonable.
