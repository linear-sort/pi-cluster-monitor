from __future__ import annotations

from datetime import datetime, timezone
import os
import socket
import subprocess
import time

import psutil

from app.models import MetricsResponse, ServiceStatus


def _read_temperature() -> float | None:
    temps = psutil.sensors_temperatures(fahrenheit=False)
    if temps:
        for _, entries in temps.items():
            if entries:
                current = entries[0].current
                if current is not None:
                    return float(current)

    # Raspberry Pi fallback: vcgencmd measure_temp -> "temp=45.8'C"
    try:
        output = subprocess.check_output(["vcgencmd", "measure_temp"], text=True, timeout=1).strip()
        if output.startswith("temp=") and output.endswith("'C"):
            value = output.removeprefix("temp=").removesuffix("'C")
            return float(value)
    except Exception:
        return None

    return None


def collect_metrics(agent_name: str = "") -> MetricsResponse:
    hostname = agent_name or socket.gethostname()
    cpu_percent = float(psutil.cpu_percent(interval=0.1))
    memory_percent = float(psutil.virtual_memory().percent)
    disk_percent = float(psutil.disk_usage("/").percent)

    try:
        load_1, load_5, load_15 = os.getloadavg()
    except OSError:
        load_1, load_5, load_15 = (0.0, 0.0, 0.0)

    net = psutil.net_io_counters()

    return MetricsResponse(
        hostname=hostname,
        timestamp=datetime.now(timezone.utc),
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        disk_percent=disk_percent,
        temperature_c=_read_temperature(),
        uptime_seconds=int(time.time() - psutil.boot_time()),
        load_1=float(load_1),
        load_5=float(load_5),
        load_15=float(load_15),
        rx_bytes=int(net.bytes_recv),
        tx_bytes=int(net.bytes_sent),
    )


def collect_services(services: list[str]) -> list[ServiceStatus]:
    statuses: list[ServiceStatus] = []
    for name in services:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", name],
                text=True,
                capture_output=True,
                timeout=1,
                check=False,
            )
            status = result.stdout.strip() or "unknown"
        except Exception:
            status = "unknown"
        statuses.append(ServiceStatus(name=name, status=status))
    return statuses
