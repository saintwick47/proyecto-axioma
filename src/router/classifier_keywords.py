#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Tablas de palabras del clasificador de intención
# Ruta: src/router/classifier_keywords.py
# Extraído de: src/router/classifier_core.py (2026-10-13)
#
# Propósito:
#   Las tablas de DATOS (palabras, patrones y normalización) que usa `IntentClassifierCore`, separadas para que el archivo del
#   clasificador se lea de corrido. Acá NO hay lógica: son datos.
#
# Relación con classifier_core.py (el patrón del proyecto, como `settings_computations.py`):
#   `classifier_core.py` las importa y las re-expone como ATRIBUTOS DE CLASE
#   (`IntentClassifierCore.KEYWORD_MAP`, …), así que el código que las usa como
#   `self.KEYWORD_MAP` o `IntentClassifierCore._WORD_BOUNDARY_KEYWORDS` sigue funcionando igual.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

from typing import Dict, List, Set

# Las tablas NO son sólo texto: `KEYWORD_MAP` arma sus valores con `TaskType.….value` (274 usos)
# y hay un uso de `settings`. Son exactamente los mismos imports que tenía `classifier_core.py`.
from src.domain.types import TaskType

KEYWORD_MAP: Dict[str, str] = {
    # ── CODE_GENERATION ──────────────────────────────────────
    # ✅ v4.7.0: Eliminados 13 FP — codigo/código/programar/programa/
    #   genera/generá/crea/creá/escribí/escribe/función/funcion/clase
    #   ya están en CODE_CONTEXT_KEYWORDS o CODE_VERBS y son manejados
    #   por el compound check (code_verb + code_context) con mayor precisión.
    # ── CODE_DEBUGGING ───────────────────────────────────────
    # ✅ v4.7.0: Eliminado "error" — "fue un error", "error gramatical".
    "debug": TaskType.CODE_DEBUGGING.value,
    "depurar": TaskType.CODE_DEBUGGING.value,
    # ✅ v0.6.9e: prompts de debug por descripción (batería de calidad)
    "race condition": TaskType.CODE_DEBUGGING.value,
    "encuentra el error": TaskType.CODE_DEBUGGING.value,
    "encontrala": TaskType.CODE_DEBUGGING.value,
    # ── CODE_EXPLANATION ─────────────────────────────────────
    # ✅ v4.7.0: Eliminados "explica","explicar","qué hace","que hace",
    #   "cómo funciona","como funciona" — ya en CODE_EXPLANATION_KEYWORDS
    #   gateados por has_code_context. Sin ese gate → falsos positivos:
    #   "explica la historia", "cómo funciona el corazón".
    "explicar código": TaskType.CODE_EXPLANATION.value,
    "explicar codigo": TaskType.CODE_EXPLANATION.value,
    # ── CODE_REVIEW ──────────────────────────────────────────
    "revisar código": TaskType.CODE_REVIEW.value,
    "revisar codigo": TaskType.CODE_REVIEW.value,
    # ── CODE_OPTIMIZATION ────────────────────────────────────
    "optimizar": TaskType.CODE_OPTIMIZATION.value,
    "optimizá": TaskType.CODE_OPTIMIZATION.value,
    "refactor": TaskType.CODE_OPTIMIZATION.value,
    # ── CODE_TESTING ─────────────────────────────────────────
    "test": TaskType.CODE_TESTING.value,
    "prueba": TaskType.CODE_TESTING.value,
    # ✅ v0.6.8pp: "probá/probar" imperativos con word boundary —
    # al ignorar "test" citado (nombre de archivo) hay que cubrir el
    # verbo fuera de comillas; evita FP en "comprobá/aprobá/aprobar".
    "probá": TaskType.CODE_TESTING.value,
    "probar": TaskType.CODE_TESTING.value,
    # ── CODE_DOCUMENTATION ───────────────────────────────────
    "documentar": TaskType.CODE_DOCUMENTATION.value,
    # ── CODE_MIGRATION ───────────────────────────────────────
    "migrar": TaskType.CODE_MIGRATION.value,
    # ── CODE_DEPLOYMENT ──────────────────────────────────────
    "desplegar": TaskType.CODE_DEPLOYMENT.value,
    # ── CODE_CORRECTION ──────────────────────────────────────
    "corregir": TaskType.CODE_CORRECTION.value,
    "corregí": TaskType.CODE_CORRECTION.value,
    "sintacticamente": TaskType.CODE_CORRECTION.value,
    "sintáctico": TaskType.CODE_CORRECTION.value,
    "sintactico": TaskType.CODE_CORRECTION.value,
    # ── QUALITY_VALIDATION ───────────────────────────────────
    "valida": TaskType.QUALITY_VALIDATION.value,
    "validar": TaskType.QUALITY_VALIDATION.value,
    "verifica": TaskType.QUALITY_VALIDATION.value,
    "verificar": TaskType.QUALITY_VALIDATION.value,
    "evalúa": TaskType.QUALITY_VALIDATION.value,
    "evalua": TaskType.QUALITY_VALIDATION.value,
    "evaluar": TaskType.QUALITY_VALIDATION.value,
    "puntúa": TaskType.QUALITY_VALIDATION.value,
    "puntua": TaskType.QUALITY_VALIDATION.value,
    "revisa calidad": TaskType.QUALITY_VALIDATION.value,
    # ── WEB_SEARCH ───────────────────────────────────────────
    # v4.6.0: Solo keywords inequívocamente de búsqueda.
    # ✅ v4.7.0: Eliminados mejores/recientes/últimas/últimos/
    #   alternativas/alternativa/populares — superlativos genéricos
    #   que disparan WEB_SEARCH en preguntas no relacionadas.
    "buscar": TaskType.WEB_SEARCH.value,
    "busca": TaskType.WEB_SEARCH.value,
    "buscame": TaskType.WEB_SEARCH.value,
    "buscáme": TaskType.WEB_SEARCH.value,
    "investigar": TaskType.WEB_SEARCH.value,
    "investiga": TaskType.WEB_SEARCH.value,
    "buscar informacion": TaskType.WEB_SEARCH.value,
    "buscar información": TaskType.WEB_SEARCH.value,
    "buscar datos": TaskType.WEB_SEARCH.value,
    "github": TaskType.WEB_SEARCH.value,
    "gitlab": TaskType.WEB_SEARCH.value,
    "bitbucket": TaskType.WEB_SEARCH.value,
    "repositorio": TaskType.WEB_SEARCH.value,
    "repositorios": TaskType.WEB_SEARCH.value,
    "repo": TaskType.WEB_SEARCH.value,
    "documentación": TaskType.WEB_SEARCH.value,
    "documentacion": TaskType.WEB_SEARCH.value,
    "trending": TaskType.WEB_SEARCH.value,
    "open source": TaskType.WEB_SEARCH.value,
    "código abierto": TaskType.WEB_SEARCH.value,
    "codigo abierto": TaskType.WEB_SEARCH.value,
    # ── NEWS_SEARCH ──────────────────────────────────────────
    "noticia": TaskType.NEWS_SEARCH.value,
    "noticias": TaskType.NEWS_SEARCH.value,
    # ── ACADEMIC_SEARCH ──────────────────────────────────────
    "estudio": TaskType.ACADEMIC_SEARCH.value,
    # ── CODE_SEARCH ──────────────────────────────────────────
    "buscar código": TaskType.CODE_SEARCH.value,
    "buscar codigo": TaskType.CODE_SEARCH.value,
    # ── FILE_SEARCH (✅ plan_rafael 2026-08-31) ───────────────
    # Búsqueda de archivos/carpetas en el disco ("dónde está el archivo X").
    # Se buscan ANTES de "archivo" → DOCUMENT_ANALYSIS para no pisarse.
    "buscá el archivo": TaskType.FILE_SEARCH.value,
    "busca el archivo": TaskType.FILE_SEARCH.value,
    "buscar el archivo": TaskType.FILE_SEARCH.value,
    "buscá la carpeta": TaskType.FILE_SEARCH.value,
    "busca la carpeta": TaskType.FILE_SEARCH.value,
    "buscar la carpeta": TaskType.FILE_SEARCH.value,
    "buscar carpeta": TaskType.FILE_SEARCH.value,
    "dónde está el archivo": TaskType.FILE_SEARCH.value,
    "donde esta el archivo": TaskType.FILE_SEARCH.value,
    "dónde está la carpeta": TaskType.FILE_SEARCH.value,
    "donde esta la carpeta": TaskType.FILE_SEARCH.value,
    "ubicación del archivo": TaskType.FILE_SEARCH.value,
    "ubicacion del archivo": TaskType.FILE_SEARCH.value,
    "ruta del archivo": TaskType.FILE_SEARCH.value,
    "encontrá el archivo": TaskType.FILE_SEARCH.value,
    "encuentra el archivo": TaskType.FILE_SEARCH.value,
    "buscar archivos": TaskType.FILE_SEARCH.value,
    "buscar archivo": TaskType.FILE_SEARCH.value,
    "ubicación de la carpeta": TaskType.FILE_SEARCH.value,
    "ubicacion de la carpeta": TaskType.FILE_SEARCH.value,
    # ── FILE_SEARCH: lectura/resumen/cita de archivos (✅ plan_rafael 2026-08-31) ──
    "leé el archivo": TaskType.FILE_SEARCH.value,
    "lee el archivo": TaskType.FILE_SEARCH.value,
    "leer el archivo": TaskType.FILE_SEARCH.value,
    "léeme el archivo": TaskType.FILE_SEARCH.value,
    "leeme el archivo": TaskType.FILE_SEARCH.value,
    "resumí el archivo": TaskType.FILE_SEARCH.value,
    "resumi el archivo": TaskType.FILE_SEARCH.value,
    "resumime el archivo": TaskType.FILE_SEARCH.value,
    "resumir el archivo": TaskType.FILE_SEARCH.value,
    "resumen del archivo": TaskType.FILE_SEARCH.value,
    "citá un bloque del archivo": TaskType.FILE_SEARCH.value,
    "cita un bloque del archivo": TaskType.FILE_SEARCH.value,
    "citá el archivo": TaskType.FILE_SEARCH.value,
    "citar el archivo": TaskType.FILE_SEARCH.value,
    "mostrame el archivo": TaskType.FILE_SEARCH.value,
    "mostrame el contenido del archivo": TaskType.FILE_SEARCH.value,
    "mostrame el contenido de": TaskType.FILE_SEARCH.value,
    "contenido del archivo": TaskType.FILE_SEARCH.value,
    # ── FILE_SEARCH: análisis de carpeta/directorio con grounding ──
    # ✅ v0.6.8ii: "analiza la carpeta X / qué hacen sus archivos" solía
    #   caer a general_chat / code_explanation SIN leer el FS → el modelo
    #   divagaba (ver logs/reg_error.txt). Ahora → file_search, cuyo handler
    #   lista la carpeta y lee los archivos de verdad.
    "analiza la carpeta": TaskType.FILE_SEARCH.value,
    "analizá la carpeta": TaskType.FILE_SEARCH.value,
    "analizar la carpeta": TaskType.FILE_SEARCH.value,
    "analiza el directorio": TaskType.FILE_SEARCH.value,
    "analizá el directorio": TaskType.FILE_SEARCH.value,
    "analizar el directorio": TaskType.FILE_SEARCH.value,
    "explora la carpeta": TaskType.FILE_SEARCH.value,
    "explorá la carpeta": TaskType.FILE_SEARCH.value,
    "explorar la carpeta": TaskType.FILE_SEARCH.value,
    "explica la carpeta": TaskType.FILE_SEARCH.value,
    "explicá la carpeta": TaskType.FILE_SEARCH.value,
    "revisa la carpeta": TaskType.FILE_SEARCH.value,
    "revisá la carpeta": TaskType.FILE_SEARCH.value,
    "revisar la carpeta": TaskType.FILE_SEARCH.value,
    "estudia la carpeta": TaskType.FILE_SEARCH.value,
    "estudiá la carpeta": TaskType.FILE_SEARCH.value,
    "qué hace la carpeta": TaskType.FILE_SEARCH.value,
    "que hace la carpeta": TaskType.FILE_SEARCH.value,
    "qué hacen sus archivos": TaskType.FILE_SEARCH.value,
    "que hacen sus archivos": TaskType.FILE_SEARCH.value,
    "analiza tools": TaskType.FILE_SEARCH.value,
    "analiza src": TaskType.FILE_SEARCH.value,
    # ── DATA_ANALYSIS ────────────────────────────────────────
    # ✅ v4.7.0: Eliminados "analizar" y "analiza" — ya cubiertos por el
    #   compound check has_analiza+has_datos antes del KEYWORD_MAP scan.
    #   Como keywords sueltos → "analiza tu corazón" → DATA_ANALYSIS (FP).
    "estadística": TaskType.DATA_ANALYSIS.value,
    "estadistica": TaskType.DATA_ANALYSIS.value,
    # ── DOCUMENT_ANALYSIS ────────────────────────────────────
    "documento": TaskType.DOCUMENT_ANALYSIS.value,
    "pdf": TaskType.DOCUMENT_ANALYSIS.value,
    "archivo": TaskType.DOCUMENT_ANALYSIS.value,
    # ── SENTIMENT / TREND / RISK ─────────────────────────────
    "sentimiento": TaskType.SENTIMENT_ANALYSIS.value,
    "tendencia": TaskType.TREND_ANALYSIS.value,
    "riesgo": TaskType.RISK_ANALYSIS.value,
    # ── GENERAL_CHAT ─────────────────────────────────────────
    "hola": TaskType.GENERAL_CHAT.value,
    "buenos días": TaskType.GENERAL_CHAT.value,
    "buenas tardes": TaskType.GENERAL_CHAT.value,
    "cómo estás": TaskType.GENERAL_CHAT.value,
    "qué haces": TaskType.GENERAL_CHAT.value,
    "imagen": TaskType.GENERAL_CHAT.value,
    "foto": TaskType.GENERAL_CHAT.value,
    # ── TECHNICAL_SUPPORT ────────────────────────────────────
    # ✅ v4.7.0: Eliminado "ayuda" — demasiado genérico.
    #   "ayuda con mi vida", "ayuda a entender X" → TECHNICAL_SUPPORT (FP).
    #   PASO 4 LLM distinguirá soporte técnico de chat general.
    # ── TUTORING ─────────────────────────────────────────────
    "enseñar": TaskType.TUTORING.value,
    "tutorial": TaskType.TUTORING.value,
    # ── BRAINSTORMING / ROLEPLAY ─────────────────────────────
    "lluvia de ideas": TaskType.BRAINSTORMING.value,
    "roleplay": TaskType.ROLEPLAY.value,
    # ── CONTENT_CREATION ─────────────────────────────────────
    "escribir": TaskType.CONTENT_CREATION.value,
    "crear": TaskType.CONTENT_CREATION.value,
    "historia": TaskType.CONTENT_CREATION.value,
    # ── POETRY / SCRIPT ──────────────────────────────────────
    "poema": TaskType.POETRY_GENERATION.value,
    "guión": TaskType.SCRIPT_WRITING.value,
    "guion": TaskType.SCRIPT_WRITING.value,
    # ── TRANSLATION ──────────────────────────────────────────
    "traducir": TaskType.TRANSLATION.value,
    "traducción": TaskType.TRANSLATION.value,
    "traduccion": TaskType.TRANSLATION.value,
    # ── SUMMARIZATION ────────────────────────────────────────
    "resumir": TaskType.SUMMARIZATION.value,
    "resumen": TaskType.SUMMARIZATION.value,
    # ── WEATHER_QUERY ────────────────────────────────────────
    "clima": TaskType.WEATHER_QUERY.value,
    "pronóstico": TaskType.WEATHER_QUERY.value,
    "pronostico": TaskType.WEATHER_QUERY.value,
    "temperatura": TaskType.WEATHER_QUERY.value,
    "tiempo": TaskType.WEATHER_QUERY.value,
    # ── TIME_QUERY ───────────────────────────────────────────
    "hora": TaskType.TIME_QUERY.value,
    # ── FINANCE_QUERY ────────────────────────────────────────
    "finanza": TaskType.FINANCE_QUERY.value,
    "cotización": TaskType.FINANCE_QUERY.value,
    "cotizacion": TaskType.FINANCE_QUERY.value,
    # ── FACT_CHECKING ────────────────────────────────────────
    "comprobar": TaskType.FACT_CHECKING.value,
    # ── SECURITY_AUDIT ───────────────────────────────────────
    "auditoria": TaskType.SECURITY_AUDIT.value,
    "auditoría": TaskType.SECURITY_AUDIT.value,
    "seguridad": TaskType.SECURITY_AUDIT.value,
    "escanear": TaskType.CODE_GENERATION.value,
    "escaneo": TaskType.CODE_GENERATION.value,
    "escanea": TaskType.CODE_GENERATION.value,
    "escaner": TaskType.CODE_GENERATION.value,
    "escáner": TaskType.CODE_GENERATION.value,
    "scanner": TaskType.CODE_GENERATION.value,
    "scan": TaskType.CODE_GENERATION.value,
    "red local": TaskType.CODE_GENERATION.value,
    "mi red": TaskType.CODE_GENERATION.value,
    "dispositivos en red": TaskType.CODE_GENERATION.value,
    "puertos abiertos": TaskType.CODE_GENERATION.value,
    "detectar dispositivos": TaskType.CODE_GENERATION.value,
    "detectar ataques": TaskType.CODE_GENERATION.value,
    "detectar intrusos": TaskType.CODE_GENERATION.value,
    "detectar malware": TaskType.CODE_GENERATION.value,
    "monitoreo de red": TaskType.CODE_GENERATION.value,
    "monitorear red": TaskType.CODE_GENERATION.value,
    "tráfico de red": TaskType.CODE_GENERATION.value,
    "trafico de red": TaskType.CODE_GENERATION.value,
    "vulnerabilidades": TaskType.CODE_GENERATION.value,
    "vulnerabilidad": TaskType.CODE_GENERATION.value,
    "fingerprint": TaskType.CODE_GENERATION.value,
    "ping sweep": TaskType.CODE_GENERATION.value,
    "arp scan": TaskType.CODE_GENERATION.value,
    "port scan": TaskType.CODE_GENERATION.value,
    "network scan": TaskType.CODE_GENERATION.value,
    "blue team": TaskType.CODE_GENERATION.value,
    "intrusion": TaskType.CODE_GENERATION.value,
    "intrusión": TaskType.CODE_GENERATION.value,
    "administración de red": TaskType.CODE_GENERATION.value,
    # ── SCREENSHOT_ANALYSIS ──────────────────────────────────
    "qué estoy viendo": TaskType.SCREENSHOT_ANALYSIS.value,
    "que estoy viendo": TaskType.SCREENSHOT_ANALYSIS.value,
    "qué ves": TaskType.SCREENSHOT_ANALYSIS.value,
    "que ves": TaskType.SCREENSHOT_ANALYSIS.value,
    "qué hay en pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "que hay en pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "analiza la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "analiza mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "analizar pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "captura de pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "screenshot": TaskType.SCREENSHOT_ANALYSIS.value,
    "qué se ve": TaskType.SCREENSHOT_ANALYSIS.value,
    "que se ve": TaskType.SCREENSHOT_ANALYSIS.value,
    "describe la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "describe lo que ves": TaskType.SCREENSHOT_ANALYSIS.value,
    "qué dice la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "que dice la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "ver pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "mirar pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "lee la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "leer pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "qué hay en mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "que hay en mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "dime que ves": TaskType.SCREENSHOT_ANALYSIS.value,
    "dime qué ves": TaskType.SCREENSHOT_ANALYSIS.value,
    "describe mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    "describí mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
    # ── JARVIS_QUERY ─────────────────────────────────────────
    "modo jarvis": TaskType.JARVIS_QUERY.value,
    "activa jarvis": TaskType.JARVIS_QUERY.value,
    "axioma escucha": TaskType.JARVIS_QUERY.value,
    "dime axioma": TaskType.JARVIS_QUERY.value,
    "oye axioma": TaskType.JARVIS_QUERY.value,
    # ── AUTONOMOUS_TASK ──────────────────────────────────────
    "haz esto solo": TaskType.AUTONOMOUS_TASK.value,
    "ejecuta autonomamente": TaskType.AUTONOMOUS_TASK.value,
    "ejecuta autónomamente": TaskType.AUTONOMOUS_TASK.value,
    "tarea autonoma": TaskType.AUTONOMOUS_TASK.value,
    "tarea autónoma": TaskType.AUTONOMOUS_TASK.value,
    "hazlo de forma autonoma": TaskType.AUTONOMOUS_TASK.value,
    "planifica y ejecuta": TaskType.AUTONOMOUS_TASK.value,
    # ── LOCATION_QUERY ───────────────────────────────────────
    "dónde estamos": TaskType.LOCATION_QUERY.value,
    "donde estamos": TaskType.LOCATION_QUERY.value,
    "dónde estoy": TaskType.LOCATION_QUERY.value,
    "donde estoy": TaskType.LOCATION_QUERY.value,
    "dónde estás": TaskType.LOCATION_QUERY.value,
    "donde estás": TaskType.LOCATION_QUERY.value,
    "en qué país": TaskType.LOCATION_QUERY.value,
    "en que pais": TaskType.LOCATION_QUERY.value,
    "qué país": TaskType.LOCATION_QUERY.value,
    "que pais": TaskType.LOCATION_QUERY.value,
    "mi ubicación": TaskType.LOCATION_QUERY.value,
    "mi ubicacion": TaskType.LOCATION_QUERY.value,
    "geolocalización": TaskType.LOCATION_QUERY.value,
    "geolocalizacion": TaskType.LOCATION_QUERY.value,
    "ubicación actual": TaskType.LOCATION_QUERY.value,
    "ubicacion actual": TaskType.LOCATION_QUERY.value,
    "localización": TaskType.LOCATION_QUERY.value,
    "localizacion": TaskType.LOCATION_QUERY.value,
    "mi localización": TaskType.LOCATION_QUERY.value,
    "mi localizacion": TaskType.LOCATION_QUERY.value,
}

FINANCE_KEYWORDS: Set[str] = {
    'dolar', 'dólar', 'euro', 'peso', 'libra', 'yen',
    'blue', 'mep', 'ccl', 'argenpesos',
    'cotizacion', 'cotización', 'bolsa',
    'bitcoin', 'crypto', 'cripto', 'usd', 'ars',
    'tasa', 'cambio', 'divisa',
}

# v4.6.0: "articulo" eliminado (ya no está en KEYWORD_MAP)
_WORD_BOUNDARY_KEYWORDS: Set[str] = {
    "genera", "generá", "crea", "creá", "programa",
    "test", "scan", "clase", "clima", "hora", "finanza",
    "probá", "probar",
    "busca", "buscame", "buscáme", "noticia",
    "analiza", "estudio", "archivo", "riesgo",
    "ayuda", "poema", "historia", "foto", "imagen",
    "error", "explica",
    "pronostico", "funcion",
    "pantalla", "screenshot",
    "jarvis", "axioma", "autonoma", "autonomamente",
    "geolocalización", "geolocalizacion",
    "ubicación", "ubicacion", "localización", "localizacion",
}

TRAINING_EXAMPLES: Dict[str, List[str]] = {
    TaskType.FILE_SEARCH.value: [
        "buscá el archivo benchmark_calibration", "dónde está el archivo settings.py",
        "busca la carpeta tools", "ubicación del archivo README",
        "encontrá el archivo main.py", "dónde está la carpeta data",
        "buscá el archivo .env", "ruta del archivo memory_guard",
    ],
    TaskType.CODE_GENERATION.value: [
        "crea una función en Python", "escribe código para", "necesito un script que",
        "implementa una clase", "genera código para", "genera un programa que",
        "crea un programa para", "hacé un script que", "escribí código para analizar archivos",
        "genera un programa q analice archivos y los corrija",
    ],
    TaskType.CODE_DEBUGGING.value: [
        "este código no funciona", "encuentra el error en", "debuggear este código",
        "por qué falla esto", "corrige el bug",
    ],
    TaskType.CODE_EXPLANATION.value: [
        "explica este código", "qué hace esta función", "cómo funciona este decorator",
        "explica cómo funciona", "para qué sirve este código",
    ],
    TaskType.QUALITY_VALIDATION.value: [
        "valida esta respuesta", "verifica la información", "evalúa este código",
        "puntúa esta respuesta", "revisa la calidad",
    ],
    TaskType.WEB_SEARCH.value: [
        # Búsqueda explícita
        "busca información sobre", "investiga acerca de",
        "qué dice internet sobre", "busca en la web",
        "buscame datos de", "buscar información de",
        # Hechos actuales — deportes
        "cuantos goles marco messi en el mundial 2026",
        "quien gano el partido de ayer",
        "resultado del argentina vs brasil",
        "en este mundial cuantos goles lleva argentina",
        "quien gano el torneo de tenis esta semana",
        "clasificacion actual del mundial 2026",
        # Hechos actuales — personas / cargos
        "quien es el presidente de argentina ahora",
        "cual es el ceo actual de apple",
        # Hechos actuales — noticias
        "que paso con messi en el partido de hoy",
        "noticias de hoy sobre inteligencia artificial",
        "ultimas noticias de tecnologia esta semana",
        # Hechos actuales — info general
        "cual es la situacion actual en ucrania",
        "que paso con el terremoto de ayer",
    ],
    TaskType.GENERAL_CHAT.value: [
        "hola", "buenos días", "cómo estás", "qué tal", "hablemos",
        "qué puedes hacer por mí", "puedes ayudarme", "ayuda con",
    ],
    TaskType.SUMMARIZATION.value: [
        "resume esto", "haz un resumen de", "puntos clave de", "sintetiza", "extrae lo importante",
    ],
    TaskType.DATA_ANALYSIS.value: [
        "analiza estos datos", "encuentra patrones en", "estadísticas de",
    ],
    TaskType.DOCUMENT_ANALYSIS.value: [
        "analiza este documento", "extrae información del pdf", "resume el archivo",
    ],
    TaskType.TRANSLATION.value: [
        "traduce al inglés", "traduce al español", "traducción de",
    ],
    TaskType.CONTENT_CREATION.value: [
        "escribe un artículo", "crea contenido para", "genera texto sobre",
    ],
    TaskType.WEATHER_QUERY.value: [
        "qué clima hace hoy", "pronóstico del tiempo", "va a llover mañana",
        "temperatura en Buenos Aires", "clima en Córdoba", "pronóstico para hoy",
        "pronóstico hoy", "dame el pronostico para hoy",
        "dame el pronostico para cordoba", "pronostico para villa general belgrano",
        "pronostico del clima", "como esta el clima", "que temperatura hace",
    ],
    TaskType.NEWS_QUERY.value: [
        "últimas noticias de Argentina", "noticias sobre tecnología",
        "qué pasó hoy", "breaking news",
    ],
    TaskType.FINANCE_QUERY.value: [
        "cotización del dólar hoy", "precio del bitcoin", "cómo está la bolsa",
        "valor del euro", "dolar blue hoy", "cotizacion del dolar blue",
        "cuánto vale el dólar blue", "dolar mep cotización",
        "ccl precio hoy", "argenpesos valor",
    ],
    TaskType.TIME_QUERY.value: [
        "qué hora es", "hora en Londres", "zona horaria de Tokio", "current time",
    ],
    TaskType.CODE_CORRECTION.value: [
        "corregí este código", "arreglá este script", "corrige los errores sintácticos",
        "analice archivos y los corrija sintacticamente",
    ],
    TaskType.SCREENSHOT_ANALYSIS.value: [
        "qué estoy viendo", "qué hay en mi pantalla", "analiza la pantalla",
        "describe lo que ves en pantalla", "qué se ve en pantalla",
        "lee lo que hay en pantalla", "qué dice la pantalla",
        "captura y analiza la pantalla", "screenshot análisis",
        "describe mi pantalla", "qué está en la pantalla",
        "analiza lo que estoy viendo", "mira la pantalla y dime",
        "qué ves en pantalla", "leer pantalla", "ver mi pantalla",
    ],
    TaskType.JARVIS_QUERY.value: [
        "modo jarvis", "axioma escucha", "oye axioma", "dime axioma",
        "activa el modo jarvis", "axioma qué hora es",
        "axioma cómo estoy", "axioma dime el clima",
    ],
    TaskType.SCREEN_DESCRIBE.value: [
        "describe la pantalla", "describe lo que ves", "qué hay en mi pantalla",
        "lee la pantalla", "leer pantalla", "describe la ventana activa",
    ],
    TaskType.AUTONOMOUS_TASK.value: [
        "haz esto solo", "ejecuta autónomamente", "tarea autónoma",
        "hazlo de forma autónoma", "planifica y ejecuta",
        "ejecuta los pasos necesarios", "termina esto por tu cuenta",
    ],
    TaskType.LOCATION_QUERY.value: [
        "dónde estamos ahora", "en qué país estamos", "dónde estoy",
        "cuál es mi ubicación", "puedes geolocalizarme", "dime dónde estoy",
        "en qué ciudad estoy", "cuál es mi país actual",
        "dónde me encuentro", "mi ubicación actual",
        "dime en qué país estamos", "dónde estamos ubicados",
        "en qué lugar estamos", "cuál es mi localización",
        "dime mi ubicación", "geolocalízame", "dónde estoy parado",
        "en qué región estamos", "dime dónde nos encontramos",
    ],
}

GREETING_PATTERNS: List[str] = [
    r'^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey)\b',
    r'^\s*(cómo estás|como estas|qué tal|que tal|cómo va|como va)\b',
    r'^\s*(qué puedes hacer|que puedes hacer|en qué me ayudas|en que me ayudas)\b',
]

CODE_CONTEXT_KEYWORDS: Set[str] = {
    "python", "javascript", "java", "c++", "rust", "go", "typescript",
    "función", "funcion", "clase", "método", "metodo", "variable",
    "return", "import", "from", "def ", "class ", "async", "await",
    "list", "dict", "array", "string", "int", "float", "bool",
    "try", "except", "raise", "error", "exception",
    "docstring", "type hint", "typing", "pydantic", "fastapi",
    "decorator", "decorador", "módulo", "modulo", "paquete",
    "código", "codigo", "script", "programa",
    # ✅ v0.6.9e (batería calidad de código): prompts que piden CÓDIGO por
    # descripción, sin sintaxis inline. Antes: keywords_miss → general_chat
    # (LRU, cola de tareas, cliente HTTP, bus pub/sub tardaban ~195 s y
    # devolvían error).
    "tests", "pytest", "race condition", "cola de tareas", "cliente http",
    "httpx", "circuit breaker", "handlers", "pub/sub", "topic",
}

CODE_VERBS: Set[str] = {
    "genera", "generá", "crea", "creá", "escribí", "escribe",
    "programa", "programar", "corregir", "corregí",
    "debug", "depurar", "optimizar", "refactor", "compilar",
    # ✅ v0.6.9e: imperativos/voseo de construcción de código
    "implementá", "implementa", "diseñá", "diseña",
    "construí", "construye",
}

CODE_EXPLANATION_KEYWORDS: Set[str] = {
    "explica", "explicar", "qué hace", "que hace", "cómo funciona",
    "como funciona", "para qué sirve", "para que sirve",
    "analiza este código", "analizá este código",
    "revisa este código", "revisá este código",
}

QUALITY_VALIDATION_KEYWORDS: Set[str] = {
    "valida", "validar", "verifica", "verificar",
    "evalúa", "evaluar", "puntúa", "puntuar",
    "revisa calidad", "chequea", "controla",
    "judge", "validation",
    # ✅ v0.6.9f: variantes voseo/imperativo (validá, verificá, evaluá…)
    "validá", "verificá", "evaluá", "puntuá", "chequeá",
}

DOCUMENT_NOUNS: Set[str] = {
    "archivo", "documentos", "documento", "pdf", "texto",
    "leer", "extraer", "procesar", "analizar documento",
}

VISION_SCREEN_KEYWORDS: Set[str] = {
    "pantalla", "screenshot", "captura de pantalla",
    "analiza la pantalla", "analiza mi pantalla", "analizar pantalla",
    "qué estoy viendo", "que estoy viendo",
    "qué hay en pantalla", "que hay en pantalla",
    "qué se ve", "que se ve",
    "qué dice la pantalla", "que dice la pantalla",
    "ver pantalla", "mirar pantalla", "qué ves", "que ves",
}

LOCATION_PATTERNS: List[str] = [
    r'\b(dónde|donde)\s+(estamos|estoy|estás|están)\b',
    r'\b(en\s+)?(qué|que)\s+(país|pais|ciudad|lugar|región|region)\b',
    r'\b(mi\s+)?(ubicación|ubicacion|localización|localizacion)\b',
    r'\bgeolocaliz',
    r'\bdime\s+(dónde|donde)\b',
    r'\ben\s+(qué|que)\s+(país|pais)\s+estamos\b',
    r'\bcuál\s+es\s+mi\s+(ubicación|ubicacion|país|pais|ciudad)\b',
    r'\bcual\s+es\s+mi\s+(ubicación|ubicacion|país|pais|ciudad)\b',
]

TASK_TYPE_NORMALIZATION: Dict[str, str] = {
    "news_query": TaskType.NEWS_SEARCH.value,
    "web_query": TaskType.WEB_SEARCH.value,
    "code_gen": TaskType.CODE_GENERATION.value,
    "code_fix": TaskType.CODE_DEBUGGING.value,
    "code_explain": TaskType.CODE_EXPLANATION.value,
    "data_analyze": TaskType.DATA_ANALYSIS.value,
    "validate": TaskType.QUALITY_VALIDATION.value,
}
