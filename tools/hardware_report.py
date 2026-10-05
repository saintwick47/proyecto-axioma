#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Ruta: /ruta/a/axioma/tools/hardware_report.py
# Autor: SaintWick
"""
Generador de informe de hardware y SO en formato Markdown.
Cross-platform: Windows, Linux, macOS.
Requiere: psutil (pip install psutil)
"""

import platform
import psutil
import subprocess
import sys
import os
import getpass
import socket
from datetime import datetime
from pathlib import Path

def run_cmd(cmd: str, timeout: int = 8) -> str:
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return res.stdout.strip() if res.returncode == 0 else "No disponible o sin permisos suficientes"
    except subprocess.TimeoutExpired:
        return "Tiempo de espera excedido"
    except Exception as e:
        return f"Error: {e}"

def get_gpu_info() -> str:
    system = platform.system().lower()
    if system == "windows":
        cmd = r'powershell -NoProfile -Command "Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, AdapterRAM | Format-List"'
    elif system == "linux":
        cmd = r'lspci 2>/dev/null | grep -iE "vga|3d|display" || echo "lspci no disponible"'
    elif system == "darwin":
        cmd = r'system_profiler SPDisplaysDataType 2>/dev/null | grep -A5 "Chipset Model\|VRAM" || echo "No disponible"'
    else:
        return "Sistema no soportado"
    return run_cmd(cmd)

def get_motherboard_bios() -> str:
    system = platform.system().lower()
    if system == "windows":
        cmd = r'powershell -NoProfile -Command "Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product, SerialNumber | Format-List; Get-CimInstance Win32_BIOS | Select-Object Name, Version, SerialNumber | Format-List"'
    elif system == "linux":
        cmd = r'sudo dmidecode -t baseboard,bios 2>/dev/null | grep -E "Manufacturer|Product Name|Version|Serial Number" || echo "Requiere sudo o dmidecode"'
    elif system == "darwin":
        cmd = r'system_profiler SPHardwareDataType | grep -E "Model Identifier|Model Name|System Version|Boot ROM Version" || echo "No disponible"'
    else:
        return "Sistema no soportado"
    return run_cmd(cmd)

def format_bytes(b: int) -> str:
    if b == 0: return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"

def generate_report() -> str:
    lines = []
    lines.append(f"# 🖥️ Informe de Hardware y Sistema Operativo\n")
    lines.append(f"**Generado:** `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n")
    lines.append(f"**Equipo:** `{platform.node()}`\n")

    # SO
    lines.append("## 🖥️ Sistema Operativo")
    lines.append(f"- **Plataforma:** `{platform.system()} {platform.release()} {platform.version()}`")
    lines.append(f"- **Arquitectura:** `{platform.machine()}`")
    lines.append(f"- **Host:** `{platform.node()}`")
    lines.append(f"- **Usuario:** `{getpass.getuser()}`")
    lines.append(f"- **Python:** `{sys.version.split()[0]}`\n")

    # CPU
    lines.append("## 🧠 Procesador")
    lines.append(f"- **Modelo:** `{platform.processor() or 'No detectado'}`")
    lines.append(f"- **Núcleos físicos:** `{psutil.cpu_count(logical=False) or 'N/A'}`")
    lines.append(f"- **Núcleos lógicos:** `{psutil.cpu_count(logical=True) or 'N/A'}`")
    freq = psutil.cpu_freq()
    if freq:
        lines.append(f"- **Frecuencia:** `{freq.current:.0f} MHz` (Min: `{freq.min:.0f}` | Max: `{freq.max:.0f}`)")
    lines.append(f"- **Carga actual:** `{psutil.cpu_percent(interval=1)}%`\n")

    # RAM
    lines.append("## 💾 Memoria RAM")
    mem = psutil.virtual_memory()
    lines.append(f"- **Total:** `{format_bytes(mem.total)}`")
    lines.append(f"- **Disponible:** `{format_bytes(mem.available)}`")
    lines.append(f"- **En uso:** `{format_bytes(mem.used)}` (`{mem.percent}%`)")
    swap = psutil.swap_memory()
    lines.append(f"- **Swap total:** `{format_bytes(swap.total)}` (`{swap.percent}%` en uso)\n")

    # Discos
    lines.append("## 💽 Almacenamiento")
    for disk in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(disk.mountpoint)
            lines.append(f"- **`{disk.device}`** (`{disk.fstype}`)")
            lines.append(f"  - Montado: `{disk.mountpoint}` | Total: `{format_bytes(u.total)}` | Libre: `{format_bytes(u.free)}` | Uso: `{u.percent}%`")
        except PermissionError:
            lines.append(f"- **`{disk.device}`**: Sin permisos de lectura")
    lines.append("")

    # Red
    lines.append("## 🌐 Interfaces de Red")
    for iface, addrs in psutil.net_if_addrs().items():
        ipv4 = ipv6 = mac = ""
        for a in addrs:
            if a.family == socket.AF_INET:
                ipv4 = a.address
            elif a.family == socket.AF_INET6:
                ipv6 = a.address.split("%")[0]
            elif a.family == getattr(psutil, 'AF_LINK', -1):
                mac = a.address
        lines.append(f"- **`{iface}`**: IPv4: `{ipv4 or '-'}` | IPv6: `{ipv6 or '-'}` | MAC: `{mac or '-'}`")
    lines.append("")

    # GPU
    lines.append("## 🎮 GPU")
    lines.append("```\n" + get_gpu_info() + "\n```\n")

    # Placa/BIOS
    lines.append("## 📟 Placa Base & BIOS")
    lines.append("```\n" + get_motherboard_bios() + "\n```\n")

    return "\n".join(lines)

def main():
    print("🔍 Analizando sistema... (puede requerir permisos de administrador para datos completos)")
    report = generate_report()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    hostname = platform.node().replace(" ", "_").replace(":", "_")
    filename = f"PC_Informe_{hostname}_{ts}.md"
    Path(filename).write_text(report, encoding="utf-8")
    print(f"✅ Informe generado correctamente: `{filename}`")

if __name__ == "__main__":
    main()
