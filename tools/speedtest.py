#!/usr/bin/env python3
import logging
# Ruta: /ruta/a/axioma/tools/speedtest.py
# Autor: SaintWick
"""
Internet Speed Test Pro - Enterprise Edition (v4.0 - Multi-Servidor)
Estrategia híbrida: Cloudflare (chunks pequeños) + CDN alternativos (Range requests)
Evita rate limiting distribuyendo carga entre múltiples proveedores.
"""
import asyncio
import socket
import time
import statistics
import sys
import random
import json
import os
from datetime import datetime
from typing import List, Tuple, Optional
from dataclasses import dataclass, field, asdict
import aiohttp
from aiohttp import ClientError

# ------------------------------------------------------------
# Fallback de tqdm
# ------------------------------------------------------------
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    class tqdm:
        def __init__(self, iterable=None, total=None, desc=None, unit=None, **kwargs):
            self.total = total or 0
            self.desc = desc or "Progreso"
            self.n = 0
        def update(self, n=1):
            self.n += n
            if self.total > 0:
                pct = min(100, int((self.n / self.total) * 100))
                bar_len = 30
                filled = int(bar_len * self.n / self.total)
                bar = '█' * filled + '░' * (bar_len - filled)
                print(f"\r  {self.desc}: |{bar}| {pct}% ({self.n}/{self.total})", end="", flush=True)
        def close(self):
            if self.total > 0 and self.n > 0:
                print()
        def __enter__(self): return self
        def __exit__(self, *args): pass

@dataclass
class SpeedTestResults:
    ping_ms: float
    jitter_ms: float
    packet_loss_percent: float
    download_mbps: float
    upload_mbps: float
    server_used: str
    region_pops: List[str] = field(default_factory=list)
    download_loss_percent: float = 0.0
    upload_loss_percent: float = 0.0
    bufferbloat_ms: float = 0.0
    raw_downloads: List[float] = field(default_factory=list)
    raw_uploads: List[float] = field(default_factory=list)

    def __str__(self) -> str:
        pops = ", ".join(set(self.region_pops)) if self.region_pops else "Global Anycast"
        return (
            f"\n{'='*65}\n"
            f"  RESULTADOS FINALES (MEDIANA DE PASADAS VÁLIDAS)\n"
            f"{'='*65}\n"
            f"  Servidor / Región  : {self.server_used} ({pops})\n"
            f"  Latencia (Reposo)  : {self.ping_ms:.2f} ms (Jitter: {self.jitter_ms:.2f} ms)\n"
            f"  Bufferbloat        : +{self.bufferbloat_ms:.2f} ms (Latencia bajo carga)\n"
            f"  Pérdida (Ping)     : {self.packet_loss_percent:.1f}%\n"
            f"  Descarga           : {self.download_mbps:.2f} Mbps (Pérdida: {self.download_loss_percent:.1f}%)\n"
            f"  Subida             : {self.upload_mbps:.2f} Mbps (Pérdida: {self.upload_loss_percent:.1f}%)\n"
            f"{'='*65}\n"
        )

class InternetSpeedTest:
    # Cloudflare para chunks pequeños (evita rate limiting)
    CLOUDFLARE_DOWNLOAD = "https://speed.cloudflare.com/__down"
    CLOUDFLARE_UPLOAD = "https://speed.cloudflare.com/__up"

    # Servidores CDN alternativos con soporte Range requests
    CDN_SERVERS = [
        "https://speed.hetzner.de/100MB.bin",
        "https://proof.ovh.net/files/100Mb.dat",
        "https://speedtest.sjc2.linode.com/100MB-sjc2.bin",
        "https://speedtest.fra1.linode.com/100MB-fra1.bin",
    ]

    HISTORY_FILE = "speedtest_history.json"

    PING_SERVERS = [
        ("1.1.1.1", 53), ("8.8.8.8", 53), ("9.9.9.9", 53), ("208.67.222.222", 53)
    ]

    def __init__(self, timeout: int = 60, max_retries: int = 3, passes: int = 3, max_pass_retries: int = 1):
        self.timeout = timeout
        self.max_retries = max_retries
        self.passes = passes
        self.max_pass_retries = max_pass_retries
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        conn = aiohttp.TCPConnector(limit=50, limit_per_host=10, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(connector=conn)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    def _calculate_timeout(self, size_bytes: int, min_speed_mbps: float = 2.0) -> float:
        bytes_per_sec = (min_speed_mbps * 1_000_000) / 8
        time_needed = size_bytes / bytes_per_sec
        return max(15.0, time_needed * 2.0)

    # ---------- Latencia ----------
    def _tcp_ping_sync(self, host: str, port: int) -> Tuple[Optional[float], bool]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            start = time.perf_counter()
            result = sock.connect_ex((host, port))
            end = time.perf_counter()
            sock.close()
            if result == 0:
                return (end - start) * 1000, True
        except (socket.timeout, socket.error, OSError) as _d2e:
            logging.getLogger(__name__).debug(f"[D2 speedtest.py:132] excepción degradada (intencional): {_d2e}")
        return None, False

    async def _ping_server(self, host: str, port: int) -> Tuple[Optional[float], bool]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._tcp_ping_sync, host, port)

    async def measure_latency(self, iterations: int = 10) -> Tuple[float, float, float]:
        latencies, successful = [], 0
        total_attempts = iterations * len(self.PING_SERVERS)

        tasks = [self._ping_server(h, p) for h, p in self.PING_SERVERS for _ in range(iterations)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception): continue
            latency, success = res
            if success and latency is not None:
                latencies.append(latency)
                successful += 1

        if not latencies: return 0.0, 0.0, 100.0

        return (
            statistics.mean(latencies),
            statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
            ((total_attempts - successful) / total_attempts) * 100
        )

    # ---------- Descarga Cloudflare (chunks pequeños) ----------
    async def _download_cloudflare(self, size_bytes: int) -> Tuple[Optional[int], Optional[str]]:
        """Descarga chunks pequeños de Cloudflare (máximo 3MB para evitar rate limiting)."""
        url = f"{self.CLOUDFLARE_DOWNLOAD}?bytes={size_bytes}"
        req_timeout = aiohttp.ClientTimeout(total=self._calculate_timeout(size_bytes))

        for attempt in range(self.max_retries):
            try:
                async with self._session.get(url, timeout=req_timeout) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        cf_ray = resp.headers.get('cf-ray', '')
                        region = cf_ray.split('-')[-1] if '-' in cf_ray else ''
                        if len(data) >= size_bytes * 0.90:
                            return len(data), region
                    elif resp.status == 429:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
            except (ClientError, asyncio.TimeoutError, ConnectionError):
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1.0 * (2 ** attempt) + random.uniform(0, 0.5))
                    continue
            except Exception:
                break
        return None, None

    # ---------- Descarga CDN (Range requests) ----------
    async def _download_cdn_range(self, url: str, start: int, end: int) -> Optional[int]:
        """Descarga un rango de bytes de un servidor CDN usando Range requests."""
        headers = {'Range': f'bytes={start}-{end}'}
        req_timeout = aiohttp.ClientTimeout(total=self._calculate_timeout(end - start + 1))

        for attempt in range(self.max_retries):
            try:
                async with self._session.get(url, headers=headers, timeout=req_timeout) as resp:
                    if resp.status in (200, 206):
                        data = await resp.read()
                        expected = end - start + 1
                        if len(data) >= expected * 0.90:
                            return len(data)
            except (ClientError, asyncio.TimeoutError, ConnectionError):
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1.0 * (2 ** attempt) + random.uniform(0, 0.5))
                    continue
            except Exception:
                break
        return None

    async def measure_download(self) -> Tuple[float, str, float, List[str], float]:
        """Estrategia híbrida: Cloudflare + CDN alternativos."""

        # Warmup con Cloudflare (5MB)
        warmup_speeds = []
        for _ in range(2):
            start = time.perf_counter()
            chunk, _ = await self._download_cloudflare(5 * 1024 * 1024)
            if chunk:
                duration = time.perf_counter() - start
                warmup_speeds.append((chunk * 8) / (duration * 1_000_000))
            await asyncio.sleep(0.5)

        warmup = statistics.mean(warmup_speeds) if warmup_speeds else 0.0
        print(f"  Velocidad estimada (warmup): {warmup:.2f} Mbps")

        # Determinar estrategia según velocidad
        if warmup > 500:
            total_mb, num_chunks, chunk_mb = 150, 50, 3  # 50 chunks de 3MB
        elif warmup > 200:
            total_mb, num_chunks, chunk_mb = 120, 40, 3
        elif warmup > 50:
            total_mb, num_chunks, chunk_mb = 90, 30, 3
        elif warmup > 10:
            total_mb, num_chunks, chunk_mb = 60, 20, 3
        else:
            total_mb, num_chunks, chunk_mb = 30, 10, 3

        chunk_size = chunk_mb * 1024 * 1024
        print(f"  Descargando {total_mb} MB en {num_chunks} chunks de {chunk_mb} MB...")

        # ESTRATEGIA 1: Intentar Cloudflare con chunks pequeños
        start_time = time.perf_counter()
        total_bytes = 0
        successful_chunks = 0
        pops = []
        cf_success = 0

        pbar = tqdm(total=num_chunks, desc="  Cloudflare")

        for i in range(num_chunks):
            bytes_val, region = await self._download_cloudflare(chunk_size)
            if bytes_val and bytes_val > 0:
                total_bytes += bytes_val
                successful_chunks += 1
                cf_success += 1
                if region:
                    pops.append(region)
            pbar.update(1)
            await asyncio.sleep(0.05)  # Pausa mínima entre requests

        pbar.close()

        # Si Cloudflare falló en más del 50%, usar CDN alternativos
        if cf_success < num_chunks * 0.5:
            print(f"  ⚠ Cloudflare falló ({cf_success}/{num_chunks}), usando CDN alternativos...")

            # Probar servidores CDN
            cdn_server = None
            for server in self.CDN_SERVERS:
                try:
                    async with self._session.head(server, timeout=10) as resp:
                        if resp.status in (200, 206):
                            cdn_server = server
                            break
                except Exception:
                    continue

            if cdn_server:
                print(f"  Usando CDN: {cdn_server}")
                total_bytes = 0
                successful_chunks = 0

                pbar = tqdm(total=num_chunks, desc="  CDN")

                for i in range(num_chunks):
                    start_byte = i * chunk_size
                    end_byte = start_byte + chunk_size - 1
                    bytes_val = await self._download_cdn_range(cdn_server, start_byte, end_byte)
                    if bytes_val and bytes_val > 0:
                        total_bytes += bytes_val
                        successful_chunks += 1
                    pbar.update(1)
                    await asyncio.sleep(0.05)

                pbar.close()
                server_used = cdn_server
            else:
                server_used = "Ninguno disponible"
        else:
            server_used = "Cloudflare Network"

        end_time = time.perf_counter()
        duration = end_time - start_time

        # Medir ping bajo carga
        ping_during_tasks = [self._ping_server(h, p) for h, p in self.PING_SERVERS for _ in range(2)]
        ping_results = await asyncio.gather(*ping_during_tasks, return_exceptions=True)
        pings_during = []
        for res in ping_results:
            if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], bool):
                lat, ok = res
                if ok and lat is not None:
                    pings_during.append(lat)

        avg_ping_during = statistics.mean(pings_during) if pings_during else 0.0
        load_loss = ((num_chunks - successful_chunks) / num_chunks) * 100 if num_chunks > 0 else 0.0

        if duration > 0 and total_bytes > 0:
            return (total_bytes * 8) / (duration * 1_000_000), server_used, load_loss, pops, avg_ping_during
        return 0.0, server_used, load_loss, pops, avg_ping_during

    # ---------- Subida (Cloudflare funciona perfectamente) ----------
    async def _upload_bytes(self, size_bytes: int) -> Optional[int]:
        data = b'0' * size_bytes
        req_timeout = aiohttp.ClientTimeout(total=self._calculate_timeout(size_bytes))

        for attempt in range(self.max_retries):
            try:
                async with self._session.post(self.CLOUDFLARE_UPLOAD, data=data, timeout=req_timeout) as resp:
                    if resp.status == 200:
                        await resp.read()
                        return size_bytes
                    elif resp.status == 429:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
            except (ClientError, asyncio.TimeoutError, ConnectionError):
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1.0 * (2 ** attempt) + random.uniform(0, 0.5))
                    continue
            except Exception:
                break
        return None

    async def measure_upload(self) -> Tuple[float, float]:
        warmup_size = 2 * 1024 * 1024
        sent = None
        for attempt in range(self.max_retries):
            start = time.perf_counter()
            sent = await self._upload_bytes(warmup_size)
            if sent:
                warmup_speed = (sent * 8) / ((time.perf_counter() - start) * 1_000_000)
                break
            await asyncio.sleep(1.0)
        else:
            warmup_speed = 20.0
            print("  ⚠ Warmup de subida falló, usando valor por defecto (20 Mbps)")

        print(f"  Velocidad de subida estimada (warmup): {warmup_speed:.2f} Mbps")

        if warmup_speed > 100: total_mb, fragments = 60, 6
        elif warmup_speed > 50: total_mb, fragments = 40, 5
        else: total_mb, fragments = 30, 3

        fragment_bytes = max(2 * 1024 * 1024, (total_mb * 1024 * 1024) // fragments)

        print(f"  Subiendo {fragments * (fragment_bytes // (1024*1024))} MB en {fragments} chunks...")

        start_time = time.perf_counter()
        total_bytes = 0
        successful_chunks = 0

        pbar = tqdm(total=fragments, desc="  Progreso Subida")

        for i in range(fragments):
            bytes_val = await self._upload_bytes(fragment_bytes)
            if bytes_val and bytes_val > 0:
                total_bytes += bytes_val
                successful_chunks += 1
            pbar.update(1)
            if i < fragments - 1:
                await asyncio.sleep(0.1)

        pbar.close()

        end_time = time.perf_counter()
        duration = end_time - start_time
        load_loss = ((fragments - successful_chunks) / fragments) * 100 if fragments > 0 else 0.0

        if duration > 0 and total_bytes > 0:
            return (total_bytes * 8) / (duration * 1_000_000), load_loss
        return 0.0, load_loss

    # ---------- Histórico JSON ----------
    def _save_history(self, results: SpeedTestResults):
        try:
            history = []
            if os.path.exists(self.HISTORY_FILE):
                with open(self.HISTORY_FILE, 'r') as f:
                    history = json.load(f)

            record = asdict(results)
            record['timestamp'] = datetime.now().isoformat()
            record.pop('raw_downloads', None)
            record.pop('raw_uploads', None)

            history.append(record)

            with open(self.HISTORY_FILE, 'w') as f:
                json.dump(history, f, indent=2)
            print(f"  ✓ Historial guardado en {self.HISTORY_FILE}")
        except Exception as e:
            print(f"  ⚠ Error guardando histórico: {e}")

    # ---------- Ejecución completa ----------
    async def run_full_test(self) -> SpeedTestResults:
        print("\n" + "="*65)
        print(f"  TEST DE VELOCIDAD PRO v4.0 ({self.passes} PASADAS)")
        print("  Estrategia: Cloudflare (chunks 3MB) + CDN alternativos")
        print("="*65 + "\n")

        dl_speeds, ul_speeds, ping_vals, jitter_vals, ploss_vals = [], [], [], [], []
        dl_losses, ul_losses = [], []
        all_pops = []
        bufferbloat_vals = []

        for i in range(self.passes):
            pass_attempt = 0
            while pass_attempt <= self.max_pass_retries:
                print(f"\n--- Pasada {i+1}/{self.passes} (Intento {pass_attempt + 1}) ---")

                ping, jitter, ploss = await self.measure_latency(iterations=8)

                dl_speed, server, dl_loss, pops, ping_under_load = await self.measure_download()
                all_pops.extend(pops)

                ul_speed, ul_loss = await self.measure_upload()

                print(f"  Descarga: {dl_speed:.2f} Mbps | Pérdida: {dl_loss:.1f}%")
                print(f"  Subida:   {ul_speed:.2f} Mbps | Pérdida: {ul_loss:.1f}%")

                if (dl_loss > 20.0 or ul_loss > 20.0) and pass_attempt < self.max_pass_retries:
                    print(f"  ⚠ Anomalía detectada (Pérdida > 20%). Reintentando...")
                    pass_attempt += 1
                    await asyncio.sleep(3)
                    continue

                ping_vals.append(ping)
                jitter_vals.append(jitter)
                ploss_vals.append(ploss)
                dl_speeds.append(dl_speed)
                ul_speeds.append(ul_speed)
                dl_losses.append(dl_loss)
                ul_losses.append(ul_loss)

                if ping > 0 and ping_under_load > 0:
                    bufferbloat_vals.append(max(0, ping_under_load - ping))

                break

            if i < self.passes - 1:
                await asyncio.sleep(3)

        if not ping_vals:
            print("  ✗ No se pudo completar ninguna pasada válida.")
            return SpeedTestResults(0, 0, 0, 0, 0, "Error")

        results = SpeedTestResults(
            ping_ms=statistics.median(ping_vals),
            jitter_ms=statistics.median(jitter_vals),
            packet_loss_percent=statistics.median(ploss_vals),
            download_mbps=statistics.median(dl_speeds),
            upload_mbps=statistics.median(ul_speeds),
            server_used="Multi-Servidor",
            region_pops=list(set(all_pops)),
            download_loss_percent=statistics.median(dl_losses),
            upload_loss_percent=statistics.median(ul_losses),
            bufferbloat_ms=statistics.median(bufferbloat_vals) if bufferbloat_vals else 0.0,
            raw_downloads=dl_speeds,
            raw_uploads=ul_speeds
        )

        self._save_history(results)
        return results

def main():
    try:
        async def async_main():
            async with InternetSpeedTest(timeout=60, max_retries=3, passes=3, max_pass_retries=1) as tester:
                results = await tester.run_full_test()
                print(results)

                print("  ANÁLISIS DE CALIDAD:")
                if results.ping_ms < 20: print("  ✓ Latencia excelente (ideal para gaming)")
                elif results.ping_ms < 50: print("  ✓ Latencia buena")
                else: print("  ✗ Latencia alta")

                if results.bufferbloat_ms < 15: print("  ✓ Sin Bufferbloat (Ideal para gaming bajo carga)")
                elif results.bufferbloat_ms < 50: print("  ⚠ Bufferbloat moderado")
                else: print("  ✗ Bufferbloat severo (Activa QoS/SQM en tu router)")

                if results.download_loss_percent > 2.0 or results.upload_loss_percent > 2.0:
                    print("  ✗ Alta pérdida de paquetes bajo carga")
                else:
                    print("  ✓ Sin pérdida de paquetes bajo carga")

                expected_speed = 40.0
                if results.download_mbps >= expected_speed * 0.85:
                    print(f"  ✓ Velocidad de descarga correcta (Cumple con los {expected_speed} Mbps)")
                    if results.download_mbps >= expected_speed * 1.5:
                        print(f"  🚀 Tu ISP te entrega {results.download_mbps:.0f} Mbps ({results.download_mbps/expected_speed:.1f}x lo contratado)")
                else:
                    print(f"  ✗ Descarga baja. Esperado: ~{expected_speed} Mbps, Obtenido: {results.download_mbps:.2f} Mbps")

        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nTest cancelado.")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
