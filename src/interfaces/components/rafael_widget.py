#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.7 — Rafael Widget (Flet Desktop Overlay)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/components/rafael_widget.py
# Autor: SaintWick
# Uso:  python src/interfaces/components/rafael_widget.py [--dialog]
# Requiere: flet[desktop]
# Cambios v0.6.7: page.window.left/top a mano no mueve la ventana en
#   Wayland (protocolo no permite reposicionamiento arbitrario del
#   cliente). Reemplazado por page.window.start_dragging() — API nativa
#   de Flet para drag de ventana frameless, funciona en X11 y Wayland.
# Cambios v0.6.6: fix arrastre — DragUpdateEvent no tiene global_x/global_y
#   en flet 0.85.1, usa e.local_delta.x/y (ya es delta, no había que
#   trackear last_gx/last_gy). Quitado wait_for_completion() (no existe
#   en Page.window, tiraba AttributeError y disparaba el fallback viejo).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import math
import sys
import threading
import time
import uuid
import logging
from pathlib import Path
from typing import List

import flet as ft
import flet.canvas as cv

logger = logging.getLogger(__name__)

STATE_FILE = Path("/tmp/.axioma_widget_state")
DIALOG_IN_FILE = Path("/tmp/.axioma_rafael_dialog_in")
DIALOG_OUT_FILE = Path("/tmp/.axioma_rafael_dialog_out")

# Archivo para recordar la posición de la ventana
CONFIG_FILE = Path.home() / ".axioma_widget_pos.json"

# --- Tamaños compactos y no invasivos -----------------------------
_WIDGET_SIZE = 64
_MARGIN_RIGHT = 10
_MARGIN_TOP = 10
_DEFAULT_SCREEN_W = 1920
_DIALOG_PANEL_WIDTH = 320
_DIALOG_PANEL_HEIGHT = 420
_DIALOG_POLL_INTERVAL = 0.25

_COLORS: dict[str, str] = {
    "IDLE":      "#3366CC",
    "COMPUTING": "#00FFCC",
    "HEALING":   "#FFB530",
    "WATCHING":  "#FF2E2E",
}

_OPACITY: dict[str, float] = {
    "IDLE":      0.60,
    "COMPUTING": 0.95,
    "HEALING":   0.80,
    "WATCHING":  0.97,
}

_SPEED: dict[str, float] = {
    "IDLE":      0.6,
    "COMPUTING": 5.5,
    "HEALING":   1.8,
    "WATCHING":  4.0,
}

_FPS: dict[str, int] = {
    "IDLE":      18,
    "COMPUTING": 60,
    "HEALING":   28,
    "WATCHING":  45,
}


def _load_position() -> tuple:
    """Carga la posición guardada de la ventana."""
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return float(data.get("left", -1)), float(data.get("top", -1))
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:86] excepción degradada (intencional): {_d2e}")
    return -1.0, -1.0


def _save_position(left: float, top: float) -> None:
    """Guarda la posición de la ventana en disco."""
    try:
        CONFIG_FILE.write_text(
            json.dumps({"left": left, "top": top}),
            encoding="utf-8"
        )
    except Exception as e:
        logger.debug(f"[Widget] No se pudo guardar posición: {e}")


def _build_shapes(state: str, angle: float, pulse: float) -> List[cv.Shape]:
    color = _COLORS.get(state, "#3366CC")
    cx = cy = _WIDGET_SIZE / 2
    scale = _WIDGET_SIZE / 120.0
    pulse_val = (math.sin(pulse) + 1.0) / 2.0
    shapes: List[cv.Shape] = []

    stroke_outer = ft.Paint(color=color, stroke_width=1.5, style=ft.PaintingStyle.STROKE)
    stroke_arc   = ft.Paint(color=color, stroke_width=2.5, style=ft.PaintingStyle.STROKE)
    stroke_thin  = ft.Paint(color=color, stroke_width=1.5, style=ft.PaintingStyle.STROKE)

    r_outer = 54 * scale
    shapes.append(cv.Circle(cx, cy, r_outer, stroke_outer))

    r_arc = 44 * scale
    sweep_main = 120 if state == "HEALING" else 90
    shapes.append(cv.Arc(
        x=cx - r_arc, y=cy - r_arc,
        width=r_arc * 2, height=r_arc * 2,
        start_angle=math.radians(angle),
        sweep_angle=math.radians(sweep_main),
        paint=stroke_arc,
    ))

    r_arc2 = 33 * scale
    shapes.append(cv.Arc(
        x=cx - r_arc2, y=cy - r_arc2,
        width=r_arc2 * 2, height=r_arc2 * 2,
        start_angle=math.radians(-angle * 0.65),
        sweep_angle=math.radians(55),
        paint=stroke_thin,
    ))

    tick_count = 8 if state != "HEALING" else 6
    tick_len = 9 * scale
    for i in range(tick_count):
        tick_a = math.radians(i * (360 / tick_count) + angle * 0.22)
        x1 = cx + (r_outer - tick_len) * math.cos(tick_a)
        y1 = cy + (r_outer - tick_len) * math.sin(tick_a)
        x2 = cx + r_outer * math.cos(tick_a)
        y2 = cy + r_outer * math.sin(tick_a)
        shapes.append(cv.Line(x1=x1, y1=y1, x2=x2, y2=y2, paint=stroke_thin))

    if state == "IDLE":
        nuc_r = 3 * scale
        nuc_color = "#0D1A2E"
    else:
        nuc_r = max(3 * scale, (4 + pulse_val * 5) * scale)
        nuc_color = color

    shapes.append(cv.Circle(
        cx, cy, nuc_r,
        ft.Paint(color=nuc_color, style=ft.PaintingStyle.FILL),
    ))
    return shapes


def main(page: ft.Page) -> None:
    page.title = "Rafael"
    page.padding = 0
    page.spacing = 0
    page.bgcolor = "#080D14"

    dialog_default_open = "--dialog" in sys.argv

    # --- Detectar pantalla real -------------------------------------
    screen_w = _DEFAULT_SCREEN_W
    try:
        if page.window.screen:
            screen_w = page.window.screen.width
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:172] excepción degradada (intencional): {_d2e}")

    # --- Tamaño inicial según modo ----------------------------------
    if dialog_default_open:
        win_w = _WIDGET_SIZE + _DIALOG_PANEL_WIDTH + 8
        win_h = max(_WIDGET_SIZE, _DIALOG_PANEL_HEIGHT)
    else:
        win_w = _WIDGET_SIZE
        win_h = _WIDGET_SIZE

    # --- Cargar posición guardada o usar esquina superior derecha ---
    saved_left, saved_top = _load_position()
    if saved_left != -1.0 and saved_top != -1.0:
        win_left = saved_left
        win_top = saved_top
    else:
        win_left = screen_w - win_w - _MARGIN_RIGHT
        win_top = _MARGIN_TOP

    # --- Configuración de ventana NO INVASIVA -----------------------
    try:
        page.window.width = win_w
        page.window.height = win_h
        page.window.min_width = _WIDGET_SIZE
        page.window.min_height = _WIDGET_SIZE
        page.window.always_on_top = True
        page.window.frameless = True
        page.window.bgcolor = "#080D14"
        page.window.opacity = _OPACITY["IDLE"]
        page.window.top = win_top
        page.window.left = win_left
        page.window.resizable = True
        page.window.minimizable = False
        page.window.maximizable = False
        page.window.skip_task_bar = True
    except AttributeError:
        try:
            page.window_width = win_w
            page.window_height = win_h
            page.window_min_width = _WIDGET_SIZE
            page.window_min_height = _WIDGET_SIZE
            page.window_always_on_top = True
            page.window_frameless = True
            page.window_bgcolor = "#080D14"
            page.window_opacity = _OPACITY["IDLE"]
            page.window_top = win_top
            page.window_left = win_left
            page.window_resizable = True
        except Exception as e:
            logger.warning(f"[Widget] Window setup error: {e}")
    page.update()

    _state: dict = {
        "current": "IDLE",
        "angle":   0.0,
        "pulse":   0.0,
        "running": True,
        "dialog_open": dialog_default_open,
        "last_out_id": None,
        "screen_w": screen_w,
        "win_left": win_left,
        "win_top": win_top,
        "moved": False,  # Bandera para distinguir click de arrastre
    }

    canvas = cv.Canvas(
        shapes=_build_shapes("IDLE", 0.0, 0.0),
        width=float(_WIDGET_SIZE),
        height=float(_WIDGET_SIZE),
    )

    def _apply_window_size(width: int, height: int, left: float, top: float) -> None:
        try:
            page.window.width = width
            page.window.height = height
            page.window.left = left
            page.window.top = top
        except AttributeError:
            try:
                page.window_width = width
                page.window_height = height
                page.window_left = left
                page.window_top = top
            except Exception as e:
                logger.warning(f"[Widget] Resize error: {e}")
        try:
            page.update()
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:260] excepción degradada (intencional): {_d2e}")

    chat_list = ft.ListView(expand=True, spacing=6, auto_scroll=True)

    def _append_message(sender: str, text: str) -> None:
        chat_list.controls.append(
            ft.Text(f"{sender}: {text}", color="#FFFFFF", size=13, selectable=True)
        )
        try:
            chat_list.update()
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:271] excepción degradada (intencional): {_d2e}")

    def _write_dialog_in(text: str) -> None:
        try:
            DIALOG_IN_FILE.write_text(
                json.dumps({"id": str(uuid.uuid4()), "text": text, "ts": time.time()}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"[Widget] No se pudo escribir dialog_in: {e}")

    text_input = ft.TextField(
        hint_text="Escribí algo...",
        color="#FFFFFF",
        bgcolor="#101826",
        border_color="#3366CC",
        text_size=13,
        expand=True,
    )

    def _send_message(e=None) -> None:
        text = (text_input.value or "").strip()
        if not text:
            return
        text_input.value = ""
        try:
            text_input.update()
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:299] excepción degradada (intencional): {_d2e}")
        _append_message("Vos", text)
        _write_dialog_in(text)

    text_input.on_submit = _send_message
    send_button = ft.Button(content="➤", width=40, on_click=_send_message)

    # --- FUNCIONES DE ARRASTRE ---------------------------------------
    # page.window.left/top seteados a mano no mueven la ventana en Wayland
    # (los clientes no pueden reposicionarse arbitrariamente por protocolo).
    # start_dragging() delega el movimiento al gestor de ventanas real —
    # funciona en X11 y Wayland por igual.
    async def _on_drag_start(e: ft.DragStartEvent) -> None:
        _state["moved"] = True
        try:
            await page.window.start_dragging()
        except Exception as ex:
            logger.warning(f"[Widget] start_dragging falló: {ex}")

    def _on_drag_end(e: ft.DragEndEvent) -> None:
        try:
            left = page.window.left
            top = page.window.top
            if left is not None and top is not None:
                _state["win_left"] = left
                _state["win_top"] = top
                _save_position(left, top)
        except Exception as ex:
            logger.debug(f"[Widget] No se pudo leer posición tras arrastre: {ex}")
        threading.Timer(0.2, lambda: _state.update({"moved": False})).start()

    def _toggle_dialog(e=None) -> None:
        # Evitar abrir/cerrar si acabamos de arrastrar la ventana
        if _state.get("moved", False):
            return

        _state["dialog_open"] = not _state["dialog_open"]
        dialog_panel.visible = _state["dialog_open"]

        shift = _DIALOG_PANEL_WIDTH + 8

        if _state["dialog_open"]:
            new_w = _WIDGET_SIZE + shift
            new_h = max(_WIDGET_SIZE, _DIALOG_PANEL_HEIGHT)
            # Mover ventana hacia la izquierda para que el anillo no se mueva de su lugar
            _state["win_left"] -= shift
        else:
            new_w = _WIDGET_SIZE
            new_h = _WIDGET_SIZE
            # Mover ventana hacia la derecha
            _state["win_left"] += shift

        _apply_window_size(new_w, new_h, _state["win_left"], _state["win_top"])
        _save_position(_state["win_left"], _state["win_top"])

        try:
            page.update()
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:357] excepción degradada (intencional): {_d2e}")

    # --- BARRA DE ARRASTRE --------------------------------------------
    drag_bar = ft.Container(
        height=20,
        bgcolor="#1A2333",
        border_radius=4,
        padding=0,
        content=ft.GestureDetector(
            content=ft.Row(
                [ft.Text("✥ Arrastrar", color="#445566", size=10, text_align=ft.TextAlign.CENTER)],
                alignment=ft.MainAxisAlignment.CENTER
            ),
            on_pan_start=_on_drag_start,
            on_pan_end=_on_drag_end,
            mouse_cursor=ft.MouseCursor.MOVE
        )
    )

    dialog_panel_content = ft.Column(
        [drag_bar, chat_list, ft.Row([text_input, send_button])],
        spacing=6,
    )

    dialog_panel = ft.Container(
        width=_DIALOG_PANEL_WIDTH,
        height=_DIALOG_PANEL_HEIGHT,
        bgcolor="#0D1420",
        border_radius=8,
        padding=8,
        visible=_state["dialog_open"],
        content=dialog_panel_content,
    )

    # El wrapper del canvas también detecta arrastre
    # ✅ plan_rafael (2026-08-31): botón derecho sobre Rafael → menú "Cerrar".
    def _show_context_menu(e=None) -> None:
        """Botón derecho (o "✕") sobre el orb → menú para cerrar Rafael."""

        def _close_action(e=None) -> None:
            try:
                dlg.open = False
                page.update()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:401] excepción degradada (intencional): {_d2e}")
            # 1) Avisar al daemon (IPC /cerrar → shutdown limpio)
            try:
                _write_dialog_in("/cerrar")
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:406] excepción degradada (intencional): {_d2e}")
            # 2) Cerrar la ventana del widget
            try:
                page.window.destroy()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:411] excepción degradada (intencional): {_d2e}")

        def _cancel_action(e=None) -> None:
            try:
                dlg.open = False
                page.update()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:418] excepción degradada (intencional): {_d2e}")

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Rafael", color="#FFFFFF"),
            content=ft.Text("¿Cerrar Rafael?", color="#CCCCCC"),
            actions=[
                ft.TextButton("Cancelar", on_click=_cancel_action),
                ft.TextButton(
                    "Cerrar", on_click=_close_action,
                    style=ft.ButtonStyle(color="#FF5555"),
                ),
            ],
            bgcolor="#0D1420",
        )
        # ✅ plan_rafael (2026-08-31): flet 0.85 usa page.show_dialog() —
        # `page.dialog = dlg` no existe en esta versión (el menú no aparecía).
        try:
            page.show_dialog(dlg)
        except Exception:
            dlg.open = True
            page.update()

    canvas_wrapper = ft.GestureDetector(
        content=canvas,
        on_tap=_toggle_dialog,
        on_secondary_tap=_show_context_menu,   # ✅ plan_rafael: botón derecho → Cerrar
        on_pan_start=_on_drag_start,
        on_pan_end=_on_drag_end,
        mouse_cursor=ft.MouseCursor.MOVE
    )

    # ✅ plan_rafael (2026-08-31): botón "✕" SIEMPRE visible sobre el orb
    # (la ventana es frameless, no hay botón nativo de cierre; el clic derecho
    # es un atajo, este es el camino visible).
    close_button = ft.Container(
        content=ft.Text("✕", color="#FFFFFF", size=12, text_align=ft.TextAlign.CENTER),
        width=24,
        height=24,
        bgcolor="#CC3344",
        border_radius=12,
        alignment=ft.alignment.Alignment(0, 0),  # centro (flet 0.85 no tiene ft.alignment.center)
        # ✅ plan_rafael (2026-08-31): posición EXPLÍCITA esquina sup. derecha
        # (left/top calculados; 'right' no posiciona bien en Stack de flet 0.85).
        left=_WIDGET_SIZE - 24 - 4,
        top=4,
        tooltip="Cerrar Rafael",
    )
    close_button.on_click = lambda e: _show_context_menu(e)

    orb_stack = ft.Stack(
        [canvas_wrapper, close_button],
        width=_WIDGET_SIZE,
        height=_WIDGET_SIZE,
    )

    def _set_opacity(state: str) -> None:
        op = _OPACITY.get(state, 0.35)
        try:
            page.window.opacity = op
            page.update()
        except Exception:
            try:
                page.window_opacity = op
                page.update()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:484] excepción degradada (intencional): {_d2e}")

    def _animation_loop() -> None:
        while _state["running"]:
            cur = _state["current"]
            speed = _SPEED.get(cur, 0.6)
            fps = _FPS.get(cur, 18)
            _state["angle"] = (_state["angle"] + speed) % 360.0
            _state["pulse"] = (_state["pulse"] + 0.11) % (2.0 * math.pi)
            try:
                canvas.shapes = _build_shapes(cur, _state["angle"], _state["pulse"])
                canvas.update()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:497] excepción degradada (intencional): {_d2e}")
            time.sleep(1.0 / fps)

    def _state_watcher() -> None:
        last_state = "IDLE"
        while _state["running"]:
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                new_state = data.get("state", "IDLE")
                if new_state in _COLORS and new_state != last_state:
                    _state["current"] = new_state
                    last_state = new_state
                    _set_opacity(new_state)
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:511] excepción degradada (intencional): {_d2e}")
            time.sleep(0.2)

    def _dialog_out_watcher() -> None:
        while _state["running"]:
            try:
                data = json.loads(DIALOG_OUT_FILE.read_text(encoding="utf-8"))
                new_id = data.get("id")
                if new_id and new_id != _state["last_out_id"]:
                    _state["last_out_id"] = new_id
                    _append_message("Rafael", data.get("text", ""))
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:523] excepción degradada (intencional): {_d2e}")
            time.sleep(_DIALOG_POLL_INTERVAL)

    def _on_window_event(e: ft.WindowEvent) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            _save_position(_state["win_left"], _state["win_top"])
            _state["running"] = False

    page.on_window_event = _on_window_event

    # Re-detectar pantalla tras un instante por si aún no estaba disponible
    def _late_screen_detect() -> None:
        time.sleep(0.5)
        try:
            if page.window.screen and page.window.screen.width:
                _state["screen_w"] = page.window.screen.width
                # No forzamos la posición si el usuario ya tiene una guardada
                if not CONFIG_FILE.exists():
                    if _state["dialog_open"]:
                        w = _WIDGET_SIZE + _DIALOG_PANEL_WIDTH + 8
                    else:
                        w = _WIDGET_SIZE
                    _state["win_left"] = _state["screen_w"] - w - _MARGIN_RIGHT
                    _state["win_top"] = _MARGIN_TOP
                    page.window.left = _state["win_left"]
                    page.window.top = _state["win_top"]
                    page.update()
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 rafael_widget.py:551] excepción degradada (intencional): {_d2e}")

    threading.Thread(target=_animation_loop, daemon=True).start()
    threading.Thread(target=_state_watcher, daemon=True).start()
    threading.Thread(target=_dialog_out_watcher, daemon=True).start()
    threading.Thread(target=_late_screen_detect, daemon=True).start()

    page.add(
        ft.Row(
            [dialog_panel, orb_stack],
            spacing=8,
            alignment=ft.MainAxisAlignment.END,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
    )
    page.update()


if __name__ == "__main__":
    try:
        ft.app(main)
    except TypeError:
        ft.app(target=main)
