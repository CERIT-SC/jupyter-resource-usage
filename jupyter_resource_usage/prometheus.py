import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from prometheus_client import Gauge

from jupyter_resource_usage.metrics import PSUtilMetricsLoader

try:
    # Traitlets >= 4.3.3
    from traitlets import Callable
except ImportError:
    from .utils import Callable


class PrometheusHandler(Callable):
    def __init__(self, metricsloader: PSUtilMetricsLoader):
        super().__init__()
        self.metricsloader = metricsloader
        self.config = metricsloader.config
        self.session_manager = metricsloader.server_app.session_manager
        self.log = getattr(metricsloader.server_app, "log", logging.getLogger(__name__))
        # psutil sampling blocks: cpu_percent(interval=0.05) waits 50 ms per
        # process, so a tree of 9 processes takes ~0.45 s. Run on the server's
        # event loop (as before), that froze every terminal, kernel message and
        # request once per second. One worker thread keeps updates in order.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="resource-usage-metrics"
        )
        self._pending = None

        gauge_names = [
            "total_memory",
            "max_memory",
            "total_cpu",
            "max_cpu",
            "max_disk",
            "current_disk",
        ]
        for name in gauge_names:
            phrase = name + "_usage"
            gauge = Gauge(phrase, "counter for " + phrase.replace("_", " "), [])
            setattr(self, phrase.upper(), gauge)

    def __call__(self, *args, **kwargs):
        """Called by a PeriodicCallback on the event loop: only hand off the work."""
        if self._pending is not None and not self._pending.done():
            return  # previous update still running; skip this tick
        self._pending = self._executor.submit(self._update)
        self._pending.add_done_callback(self._log_failure)

    def _log_failure(self, future):
        if not future.cancelled() and future.exception() is not None:
            self.log.error(
                "jupyter_resource_usage: updating Prometheus metrics failed",
                exc_info=future.exception(),
            )

    def _update(self):
        memory_metric_values = self.metricsloader.memory_metrics()
        if memory_metric_values is not None:
            self.TOTAL_MEMORY_USAGE.set(memory_metric_values["memory_info_rss"])
            self.MAX_MEMORY_USAGE.set(self.apply_memory_limit(memory_metric_values))
        if self.config.track_cpu_percent:
            cpu_metric_values = self.metricsloader.cpu_metrics()
            if cpu_metric_values is not None:
                self.TOTAL_CPU_USAGE.set(cpu_metric_values["cpu_percent"])
                self.MAX_CPU_USAGE.set(self.apply_cpu_limit(cpu_metric_values))
        if self.config.track_disk_usage:
            disk_metric_values = self.metricsloader.disk_metrics()
            if disk_metric_values is not None:
                self.CURRENT_DISK_USAGE.set(disk_metric_values["disk_usage_used"])
                self.MAX_DISK_USAGE.set(disk_metric_values["disk_usage_total"])

    def apply_memory_limit(self, memory_metric_values) -> Optional[int]:
        if memory_metric_values is None:
            return None
        else:
            if callable(self.config.mem_limit):
                return self.config.mem_limit(
                    rss=memory_metric_values["memory_info_rss"]
                )
            elif self.config.mem_limit > 0:  # mem_limit is an Int
                return self.config.mem_limit
            else:
                return memory_metric_values["virtual_memory_total"]

    def apply_cpu_limit(self, cpu_metric_values) -> Optional[float]:
        if cpu_metric_values is None:
            return None
        else:
            if callable(self.config.cpu_limit):
                return self.config.cpu_limit(
                    cpu_percent=cpu_metric_values["cpu_percent"]
                )
            elif self.config.cpu_limit > 0.0:  # cpu_limit is a Float
                return self.config.cpu_limit
            else:
                return 100.0 * cpu_metric_values["cpu_count"]
