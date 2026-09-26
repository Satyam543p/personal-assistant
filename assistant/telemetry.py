"""
Hardware & System Telemetry Subsystem for Jarvis.
Provides live monitoring of CPU, RAM, battery %, and power plug status,
with an adaptive throttling guard (is_safe_for_heavy_task) to protect
the 8 GB RAM and battery budget on mobile laptops.
"""

import abc
import os
import sys
import logging
import platform

logger = logging.getLogger("jarvis.telemetry")

try:
    from assistant.tools import Tool
except ModuleNotFoundError:
    from tools import Tool


class SystemTelemetry(abc.ABC):
    @abc.abstractmethod
    def get_telemetry(self) -> dict:
        """Returns live CPU load, RAM total/used/free, battery %, and AC power status."""
        pass

    @abc.abstractmethod
    def is_safe_for_heavy_task(self, min_ram_free_mb: int = 800, min_battery_pct: int = 20) -> tuple[bool, str]:
        """Validates if system has enough RAM and battery (or plugged in) for heavy tasks."""
        pass


class WindowsSystemTelemetry(SystemTelemetry):
    def __init__(self):
        self._has_psutil = False
        try:
            import psutil
            self._has_psutil = True
        except ImportError:
            logger.warning("psutil not available; WindowsSystemTelemetry running in basic mode.")

    def get_telemetry(self) -> dict:
        telemetry = {
            "platform": platform.platform(),
            "cpu_percent": 0.0,
            "ram_total_mb": 0,
            "ram_used_mb": 0,
            "ram_available_mb": 0,
            "ram_percent": 0.0,
            "battery_percent": None,
            "power_plugged": None,
            "power_status_str": "unknown"
        }

        if self._has_psutil:
            import psutil
            try:
                cpu = psutil.cpu_percent(interval=0.05)
                mem = psutil.virtual_memory()
                battery = psutil.sensors_battery()

                telemetry["cpu_percent"] = round(cpu, 1)
                telemetry["ram_total_mb"] = round(mem.total / (1024 * 1024), 1)
                telemetry["ram_used_mb"] = round(mem.used / (1024 * 1024), 1)
                telemetry["ram_available_mb"] = round(mem.available / (1024 * 1024), 1)
                telemetry["ram_percent"] = round(mem.percent, 1)

                if battery is not None:
                    telemetry["battery_percent"] = round(battery.percent, 1)
                    telemetry["power_plugged"] = battery.power_plugged
                    telemetry["power_status_str"] = "Plugged In (AC)" if battery.power_plugged else "On Battery"
                else:
                    telemetry["power_status_str"] = "Desktop/AC (No Battery Sensor)"
            except Exception as e:
                logger.error(f"Error reading psutil telemetry: {e}")
        else:
            telemetry["power_status_str"] = "Basic Fallback Mode"

        return telemetry

    def is_safe_for_heavy_task(self, min_ram_free_mb: int = 800, min_battery_pct: int = 20) -> tuple[bool, str]:
        data = self.get_telemetry()
        available_mb = data.get("ram_available_mb", 0)
        battery_pct = data.get("battery_percent")
        plugged = data.get("power_plugged")

        # 1. Check RAM headroom
        if available_mb and available_mb < min_ram_free_mb:
            msg = (
                f"Available RAM is critically low ({available_mb} MB available, required > {min_ram_free_mb} MB). "
                "Heavy task throttled to protect 8 GB system budget."
            )
            logger.warning(msg)
            return False, msg

        # 2. Check Battery level if running on battery power
        if plugged is False and battery_pct is not None and battery_pct < min_battery_pct:
            msg = (
                f"Battery is low ({battery_pct}%) and running on battery power. "
                "Heavy background tasks are throttled to conserve power."
            )
            logger.warning(msg)
            return False, msg

        return True, "System hardware resources within safe operating thresholds."


class GetSystemTelemetryTool(Tool):
    def __init__(self, telemetry_service: SystemTelemetry = None):
        declaration = {
            "inputs": {
                "check_safety": {"type": "boolean", "default": False}
            },
            "side_effects": "Queries hardware metrics (CPU, RAM, battery, power)",
            "timeout_ms": 3000,
            "memory_limit_mb": 20
        }
        super().__init__("get_system_telemetry", "read_only", declaration)
        self.telemetry = telemetry_service or WindowsSystemTelemetry()

    async def execute(self, executor, **kwargs) -> dict:
        data = self.telemetry.get_telemetry()
        is_safe, safety_reason = self.telemetry.is_safe_for_heavy_task()
        data["is_safe_for_heavy_task"] = is_safe
        data["safety_reason"] = safety_reason

        cpu_str = f"{data['cpu_percent']}%"
        ram_str = f"{data['ram_available_mb']} MB free of {data['ram_total_mb']} MB ({data['ram_percent']}% used)"
        bat_str = f"{data['battery_percent']}% ({data['power_status_str']})" if data['battery_percent'] is not None else data['power_status_str']

        summary = f"System Telemetry: CPU: {cpu_str} | RAM: {ram_str} | Power: {bat_str}. Safe for heavy tasks: {'Yes' if is_safe else 'No (' + safety_reason + ')'}"

        return {
            "status": "success",
            "telemetry": data,
            "response": summary
        }
