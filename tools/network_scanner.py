#!/usr/bin/env python3
import logging
# Ruta: /ruta/a/axioma/tools/network_scanner.py
# Autor: SaintWick
"""
network_scanner.py – Escáner de puertos asíncrono con detección de servicios y análisis de vulnerabilidades.
Uso: python network_scanner.py 192.168.1.1 -r 1 1024 -w 200 --retries 2
"""
import asyncio
import socket
import time
import sys
import argparse
import csv
import json
import ipaddress
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # fallback sin barra

# ── Base de conocimiento de vulnerabilidades (ampliada) ──
VULN_KB = {
    21: {"servicio": "FTP", "vuln": "Credenciales en texto plano, acceso anónimo", "parche": "Migrar a SFTP/FTPS, deshabilitar anónimos", "cvss": 7.8, "capa": "Servicio", "estado": "Detectado"},
    22: {"servicio": "SSH", "vuln": "Fuerza bruta, versiones legacy expuestas", "parche": "Auth por clave, disable root login, actualizar daemon", "cvss": 6.5, "capa": "Servicio", "estado": "Detectado"},
    23: {"servicio": "Telnet", "vuln": "Protocolo sin cifrado, sniffeable", "parche": "Desactivar servicio, migrar a SSH", "cvss": 8.1, "capa": "Servicio", "estado": "Detectado"},
    25: {"servicio": "SMTP", "vuln": "Open relay, SPF no configurado", "parche": "Restringir relay, implementar SPF/DKIM", "cvss": 6.0, "capa": "Servicio", "estado": "Detectado"},
    53: {"servicio": "DNS", "vuln": "Transferencias de zona expuestas, amplificación", "parche": "Restringir AXFR/IXFR, rate limiting", "cvss": 6.4, "capa": "Red", "estado": "Detectado"},
    80: {"servicio": "HTTP", "vuln": "Tráfico no cifrado, web apps expuestas", "parche": "Forzar HTTPS, aplicar WAF", "cvss": 5.3, "capa": "Aplicación", "estado": "Detectado"},
    443: {"servicio": "HTTPS", "vuln": "Certificados vencidos, TLS obsoleto", "parche": "Renovar certs, forzar TLSv1.2+", "cvss": 4.0, "capa": "Aplicación", "estado": "Detectado"},
    8080: {"servicio": "HTTP-Alt/Proxy", "vuln": "Servicios de desarrollo expuestos", "parche": "Cerrar si no es productivo, autenticar acceso", "cvss": 6.1, "capa": "Aplicación", "estado": "Detectado"},
    3306: {"servicio": "MySQL", "vuln": "Exposición directa a red pública", "parche": "Bind a 127.0.0.1, firewall restrictivo", "cvss": 8.2, "capa": "Servicio", "estado": "Detectado"},
    5432: {"servicio": "PostgreSQL", "vuln": "Configuración por defecto insegura", "parche": "Ajustar pg_hba.conf, cifrar conexiones", "cvss": 8.0, "capa": "Servicio", "estado": "Detectado"},
    6379: {"servicio": "Redis", "vuln": "Sin autenticación, exposición de datos", "parche": "Configurar requirepass, bind a localhost", "cvss": 7.8, "capa": "Servicio", "estado": "Detectado"},
    27017: {"servicio": "MongoDB", "vuln": "Sin autenticación por defecto", "parche": "Habilitar autenticación, bind a localhost", "cvss": 8.0, "capa": "Servicio", "estado": "Detectado"},
}

# ── Clase Scanner asíncrona ──
class AsyncNetworkScanner:
    def __init__(self, target_ip: str, timeout: float = 1.0, max_concurrent: int = 200,
                 retries: int = 2, service_detect: bool = True):
        self.target_ip = target_ip
        self.timeout = max(0.2, min(timeout, 10.0))
        self.max_concurrent = max(1, min(max_concurrent, 1000))
        self.retries = max(0, retries)
        self.service_detect = service_detect
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._results = {}

    async def _check_port(self, port: int) -> Optional[Dict[str, Any]]:
        """Intenta conectar al puerto y detectar servicio si está abierto."""
        for attempt in range(self.retries + 1):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.target_ip, port),
                    timeout=self.timeout
                )
                # Puerto abierto
                service = None
                banner = ""
                if self.service_detect:
                    try:
                        writer.write(b"\n")
                        await writer.drain()
                        banner = (await asyncio.wait_for(reader.read(1024), timeout=0.5)).decode(errors='ignore').strip()
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 network_scanner.py:70] excepción degradada (intencional): {_d2e}")
                    service = self._guess_service(port, banner)
                writer.close()
                await writer.wait_closed()
                return {"port": port, "open": True, "service": service, "banner": banner[:200]}
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                if attempt == self.retries:
                    return None
                await asyncio.sleep(0.2 * (2 ** attempt))
            except Exception:
                return None
        return None

    def _guess_service(self, port: int, banner: str) -> str:
        """Deduce servicio a partir del puerto y/o banner."""
        if "SSH" in banner.upper():
            return "SSH"
        if "HTTP" in banner.upper() or "HTML" in banner.upper():
            return "HTTP"
        if "FTP" in banner.upper():
            return "FTP"
        if "MySQL" in banner.upper():
            return "MySQL"
        if "PostgreSQL" in banner.upper():
            return "PostgreSQL"
        if "Redis" in banner.upper():
            return "Redis"
        if "MongoDB" in banner.upper():
            return "MongoDB"
        kb = VULN_KB.get(port)
        if kb:
            return kb["servicio"]
        common = {
            21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP",
            53: "DNS", 80: "HTTP", 110: "POP3", 143: "IMAP",
            443: "HTTPS", 993: "IMAPS", 995: "POP3S", 3306: "MySQL",
            5432: "PostgreSQL", 6379: "Redis", 27017: "MongoDB"
        }
        return common.get(port, f"Desconocido (Port {port})")

    async def scan_ports(self, port_range: range) -> Dict[int, Dict[str, Any]]:
        """Escanea el rango de puertos de forma asíncrona."""
        total = len(port_range)
        if total == 0:
            return {}

        pbar = None
        if tqdm is not None:
            pbar = tqdm(total=total, desc="Escaneando", unit="puertos", ncols=80)

        sem = self._sem
        async def scan_one(port):
            async with sem:
                result = await self._check_port(port)
                if pbar:
                    pbar.update(1)
                return result

        tasks = [scan_one(p) for p in port_range]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        if pbar:
            pbar.close()

        open_ports = {}
        for result in results:
            if isinstance(result, dict) and result.get("open"):
                port = result["port"]
                kb = VULN_KB.get(port)
                if kb:
                    result.update(kb)
                else:
                    result.update({
                        "vuln": "Servicio no identificado en la base de conocimiento",
                        "parche": "Investigar el servicio y aplicar hardening específico",
                        "cvss": 0.0,
                        "capa": "Servicio",
                        "estado": "Pendiente de análisis"
                    })
                open_ports[port] = result
            elif isinstance(result, Exception):
                sys.stderr.write(f"\n[!] Error inesperado: {result}\n")
        return open_ports

# ── Generador de informes ──
class PatchGenerator:
    def __init__(self, vulnerabilities: Dict[int, Dict[str, Any]]):
        self.vulns = vulnerabilities

    def generate_report(self, target_ip: str, duration: float) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("INFORME DE ESCANEO Y ANÁLISIS DE VULNERABILIDADES")
        lines.append("=" * 60)
        lines.append(f"Objetivo: {target_ip}")
        lines.append(f"Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Duración: {duration:.2f}s")
        lines.append(f"Hallazgos críticos: {len(self.vulns)}")
        lines.append("-" * 60)
        if not self.vulns:
            lines.append("No se detectaron puertos abiertos en el rango escaneado.")
        else:
            for port, meta in sorted(self.vulns.items()):
                service = meta.get("service", meta.get("servicio", f"Port {port}"))
                banner = meta.get("banner", "")
                vuln = meta.get("vuln", "Sin información")
                patch = meta.get("parche", "Sin remediación")
                cvss = meta.get("cvss", 0.0)
                capa = meta.get("capa", "Desconocida")
                lines.append(
                    f"| PORT: {port} | SERVICIO: {service} | CAPA: {capa} | CVSS: {cvss:.1f} | "
                    f"VULN: {vuln} | REMEDIACIÓN: {patch}"
                )
                if banner:
                    lines.append(f"| BANNER: {banner[:100]}")
        lines.append("=" * 60)
        return "\n".join(lines)

# ── Parser y exportador (mantiene funcionalidad original) ──
class ReportParser:
    @staticmethod
    def parse_txt(filepath: str) -> Dict[str, Any]:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read().replace("\r\n", "\n").replace("\r", "\n")
        except FileNotFoundError:
            raise FileNotFoundError(f"No se encontró el archivo: {filepath}")
        except Exception as e:
            raise Exception(f"Error al leer el archivo: {e}")

        metadata = {"Objetivo": "", "Fecha": "", "Duracion": 0.0, "Hallazgos": 0}
        findings = []

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("=") or line.startswith("-"):
                continue

            if ":" in line and not line.startswith("|"):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    key, val = parts[0].strip(), parts[1].strip()
                    if key == "Duracion":
                        try:
                            metadata[key] = float(val.replace("s", "").strip())
                        except ValueError:
                            metadata[key] = 0.0
                    elif key == "Hallazgos críticos/puertos abiertos":
                        try:
                            metadata["Hallazgos"] = int(val)
                        except ValueError:
                            metadata["Hallazgos"] = 0
                    else:
                        metadata[key] = val
                continue

            if line.startswith("| PORT:"):
                try:
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    finding = {}
                    for part in parts:
                        if ": " in part:
                            key, val = part.split(": ", 1)
                            key = key.strip().lower()
                            val = val.strip()
                            if key == "port":
                                finding["puerto"] = int(val)
                            elif key == "capa":
                                finding["capa"] = val
                            elif key == "cvss":
                                finding["cvss"] = float(val)
                            elif key == "estado":
                                finding["estado"] = val
                            elif key == "servicio":
                                finding["servicio"] = val
                            elif key == "vuln":
                                finding["vulnerabilidad"] = val
                            elif key == "remediacion":
                                finding["remediacion"] = val
                    if "puerto" in finding:
                        findings.append(finding)
                except (ValueError, IndexError):
                    continue

        clean_metadata = {k.strip(): v.strip() if isinstance(v, str) else v for k, v in metadata.items()}
        return {"metadata": clean_metadata, "hallazgos": findings}

    @staticmethod
    def export(parsed_data: Dict[str, Any], base_filename: str) -> Tuple[str, str]:
        json_path = f"{base_filename}.json"
        csv_path = f"{base_filename}.csv"

        def sanitize(obj):
            if isinstance(obj, dict):
                return {str(k).strip(): sanitize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [sanitize(item) for item in obj]
            elif isinstance(obj, str):
                return obj.strip()
            else:
                return obj

        cleaned = sanitize(parsed_data)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2, ensure_ascii=False)

        fields = ["puerto", "capa", "cvss", "estado", "servicio", "vulnerabilidad", "remediacion"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            if cleaned.get("hallazgos"):
                writer.writerows([sanitize(h) for h in cleaned["hallazgos"]])

        return json_path, csv_path

# ── Función principal asíncrona ──
async def main():
    parser = argparse.ArgumentParser(description="Escáner de red asíncrono mejorado")
    parser.add_argument("target_ip", nargs="?", help="Dirección IP objetivo")
    parser.add_argument("-t", "--timeout", type=float, default=1.0, help="Timeout por intento (segundos)")
    parser.add_argument("-r", "--port-range", nargs=2, type=int, metavar=("INICIO", "FIN"),
                       default=[1, 1024], help="Rango de puertos (max 10000)")
    parser.add_argument("-w", "--workers", type=int, default=200, help="Máximo de conexiones simultáneas")
    parser.add_argument("--retries", type=int, default=2, help="Número de reintentos por puerto")
    parser.add_argument("--no-service", action="store_true", help="Desactivar detección de servicios (más rápido)")
    parser.add_argument("-f", "--parse", type=str, help="Parsear informe TXT existente y exportar")
    parser.add_argument("--db", type=str, help="Guardar resultados en SQLite (ruta a archivo .db)")
    args = parser.parse_args()

    if args.parse:
        print(f"[*] Parseando informe: {args.parse}")
        try:
            parsed = ReportParser.parse_txt(args.parse)
            json_out, csv_out = ReportParser.export(parsed, args.parse.rsplit(".", 1)[0])
            print(f"[+] JSON: {json_out}")
            print(f"[+] CSV: {csv_out}")
            print(f"[+] Hallazgos: {len(parsed.get('hallazgos', []))}")
        except Exception as e:
            print(f"[-] Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if not args.target_ip:
        print("[-] Debes especificar una IP objetivo", file=sys.stderr)
        sys.exit(1)

    try:
        ipaddress.ip_address(args.target_ip)
    except ValueError:
        print("[-] IP inválida", file=sys.stderr)
        sys.exit(1)

    start_port, end_port = args.port_range
    if start_port < 1 or end_port > 65535 or start_port > end_port:
        print("[-] Rango inválido (1-65535, inicio <= fin)", file=sys.stderr)
        sys.exit(1)
    if end_port - start_port > 10000:
        print("[-] Rango demasiado grande (>10000 puertos)", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Escaneando {args.target_ip} (puertos {start_port}-{end_port})")
    scanner = AsyncNetworkScanner(
        target_ip=args.target_ip,
        timeout=args.timeout,
        max_concurrent=args.workers,
        retries=args.retries,
        service_detect=not args.no_service
    )

    start_time = time.time()
    results = await scanner.scan_ports(range(start_port, end_port + 1))
    duration = time.time() - start_time

    print(f"\n[+] Escaneo completado en {duration:.2f}s")
    print(f"[+] Puertos abiertos: {len(results)}")
    if results:
        print(f"    Puertos: {', '.join(str(p) for p in sorted(results.keys()))}")

    patch_gen = PatchGenerator(results)
    report = patch_gen.generate_report(args.target_ip, duration)
    print("\n" + report)

    safe_ip = args.target_ip.replace('.', '_')
    report_file = f"report_{safe_ip}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n[+] Informe TXT guardado en: {report_file}")

    if args.db:
        conn = sqlite3.connect(args.db)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS scans
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      target TEXT, port INTEGER, service TEXT, banner TEXT,
                      vuln TEXT, patch TEXT, cvss REAL, capa TEXT, scanned_at TIMESTAMP)''')
        for port, meta in results.items():
            c.execute('''INSERT INTO scans (target, port, service, banner, vuln, patch, cvss, capa, scanned_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))''',
                      (args.target_ip, port, meta.get('service', ''), meta.get('banner', ''),
                       meta.get('vuln', ''), meta.get('parche', ''), meta.get('cvss', 0.0), meta.get('capa', '')))
        conn.commit()
        conn.close()
        print(f"[+] Resultados guardados en DB: {args.db}")

if __name__ == "__main__":
    asyncio.run(main())
