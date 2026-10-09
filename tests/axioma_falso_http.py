#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA falso — servidor HTTP mínimo para probar clientes externos
# ═══════════════════════════════════════════════════════════════
# Para qué: probar, en un Windows de verdad (o acá), que un cliente externo **encuentra el endpoint de
# chat** de AXIOMA y le habla. Lo usa el trabajo `instaladores-windows` del CI con
# `instalar/rafael_windows.ps1 -Ensayo`: en el runner no hay AXIOMA, así que este falso contesta lo mismo
# (200 con un JSON mínimo) y el demonio tiene que reconocerlo.
#
# Uso:  python tests/axioma_falso_http.py [puerto]     (por defecto 8098)
# Se apaga con Ctrl+C. No necesita dependencias: biblioteca estándar.
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO = int(sys.argv[1]) if len(sys.argv) > 1 else 8098


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):        # sin ruido
        pass

    def _responder(self, datos):
        cuerpo = json.dumps(datos).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        if largo:
            self.rfile.read(largo)
        self._responder({"success": True, "content": "respuesta del AXIOMA falso",
                         "model_used": "falso", "session_id": None})

    def do_GET(self):
        self._responder({"status": "ok", "falso": True})


if __name__ == "__main__":
    print(f"AXIOMA falso escuchando en http://127.0.0.1:{PUERTO}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PUERTO), Handler).serve_forever()
