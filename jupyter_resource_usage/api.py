import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor
from inspect import isawaitable

from jupyter_resource_usage.metrics import ContainerMetricsLoader, GPUMetricsLoader
import psutil
import zmq.asyncio
from jupyter_client.jsonutil import json_default
from jupyter_server.base.handlers import APIHandler
from packaging import version
from tornado import web
from tornado.concurrent import run_on_executor


try:
    import ipykernel

    IPYKERNEL_VERSION = ipykernel.__version__
    USAGE_IS_SUPPORTED = version.parse("6.9.0") <= version.parse(IPYKERNEL_VERSION)
except ImportError:
    USAGE_IS_SUPPORTED = False
    IPYKERNEL_VERSION = None

class ApiHandler(APIHandler):
    executor = ThreadPoolExecutor(max_workers=5)

    @web.authenticated
    async def get(self):
        """
        Calculate and return current resource usage metrics
        """
        config = self.settings["jupyter_resource_usage_display_config"]
        container_metrics_loader = ContainerMetricsLoader()
        gpu_metrics_loader = GPUMetricsLoader()

        cur_process = psutil.Process()
        all_processes = [cur_process] + cur_process.children(recursive=True)

        if not config.is_container:
            # Get memory information
            rss = 0
            pss = None
            for p in all_processes:
                try:
                    info = p.memory_full_info()
                    if hasattr(info, "pss"):
                        pss = (pss or 0) + info.pss
                    rss += info.rss
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    pass

            if callable(config.mem_limit):
                mem_limit = config.mem_limit(rss=rss, pss=pss)
            else:  # mem_limit is an Int
                mem_limit = config.mem_limit
        else:
            # Get memory information from container cgroup
            mem_limit = container_metrics_loader.physical_memory()
            rss = container_metrics_loader.memory_rss()
            pss = container_metrics_loader.memory_pss()

        limits = {"memory": {"rss": mem_limit, "pss": mem_limit}}
        if config.mem_limit and config.mem_warning_threshold != 0:
            limits["memory"]["warn"] = (mem_limit - rss) < (
                mem_limit * config.mem_warning_threshold
            )

        metrics = {"rss": rss, "limits": limits}
        if pss is not None:
            metrics["pss"] = pss

        # Optionally get CPU information
        if config.track_cpu_percent:
            
            if not config.is_container:
                cpu_count = psutil.cpu_count()
            else:
                cpu_count = container_metrics_loader.cpu_count()
            
            cpu_percent = await self._get_cpu_percent(all_processes) / cpu_count

            if config.cpu_limit != 0:
                limits["cpu"] = {"cpu": config.cpu_limit}
                if config.cpu_warning_threshold != 0:
                    limits["cpu"]["warn"] = (config.cpu_limit * 100 - cpu_percent) < (
                        config.cpu_limit * 100 * config.cpu_warning_threshold
                    )

            metrics.update(cpu_percent=cpu_percent, cpu_count=cpu_count)

        # Optionally get Disk information
        if config.track_disk_usage:
            try:
                disk_info = psutil.disk_usage(config.disk_path)
            except Exception:
                pass
            else:
                metrics.update(disk_used=disk_info.used, disk_total=disk_info.total)
                limits["disk"] = {"disk": disk_info.total}
                if config.disk_warning_threshold != 0:
                    limits["disk"]["warn"] = (disk_info.total - disk_info.used) < (
                        disk_info.total * config.disk_warning_threshold
                    )

        if config.track_gpu_usage:
            gpu_metrics = gpu_metrics_loader.get_gpu_metrics()
            if gpu_metrics is not None:
                # to MB
                mem_used = gpu_metrics.mem_used // 1024**2
                mem_total = gpu_metrics.mem_total // 1024**2
                metrics.update(gpu_mem_used=mem_used, gpu_mem_total=mem_total)
                limits["gpu_mem"] = {"gpu_mem": mem_total}
                if config.gpu_mem_warning_threshold != 0:
                    limits["gpu_mem"]["warn"] = (mem_total - mem_used) < (
                        mem_total * config.gpu_mem_warning_threshold
                    )

        self.write(json.dumps(metrics))

    @run_on_executor
    def _get_cpu_percent(self, all_processes):
        def get_cpu_percent(p):
            try:
                return p.cpu_percent(interval=0.05)
            # Avoid littering logs with stack traces complaining
            # about dead processes having no CPU usage
            except:
                return 0

        return sum([get_cpu_percent(p) for p in all_processes])


class KernelUsageHandler(APIHandler):
    @web.authenticated
    async def get(self, matched_part=None, *args, **kwargs):
        if not USAGE_IS_SUPPORTED:
            self.write(
                json.dumps(
                    {
                        "content": {
                            "reason": "not_supported",
                            "kernel_version": IPYKERNEL_VERSION,
                        }
                    }
                )
            )
            return

        config = self.settings["jupyter_resource_usage_display_config"]

        kernel_id = matched_part
        km = self.kernel_manager
        lkm = km.pinned_superclass.get_kernel(km, kernel_id)
        session = lkm.session
        client = lkm.client()

        control_channel = client.control_channel
        usage_request = session.msg("usage_request", {})
        control_channel.send(usage_request)
        poller = zmq.asyncio.Poller()
        control_socket = control_channel.socket
        poller.register(control_socket, zmq.POLLIN)
        timeout_ms = 10_000
        events = dict(await poller.poll(timeout_ms))
        if control_socket not in events:
            out = json.dumps(
                {
                    "content": {"reason": "timeout", "timeout_ms": timeout_ms},
                    "kernel_id": kernel_id,
                }
            )

        else:
            res = client.control_channel.get_msg(timeout=0)
            if isawaitable(res):
                # control_channel.get_msg may return a Future,
                # depending on configured KernelManager class
                res = await res
            if res:
                res["kernel_id"] = kernel_id

            if config.is_container:
                container_metrics = ContainerMetricsLoader()
                res["content"].update({
                    "cpu_count": container_metrics.cpu_count(),
                    "host_cpu_percent": container_metrics.cpu_percent(),
                })

                total_memory = container_metrics.physical_memory()
                used_memory = container_metrics.memory_pss()
                memory_stat = container_metrics.memory_stat()

                res["content"]["host_virtual_memory"].update({
                    "total": total_memory,
                    "used": used_memory,
                    "percent": (used_memory / total_memory) * 100 if total_memory > 0 else 0,
                    "free": total_memory - used_memory,
                    "available": (total_memory - used_memory) + memory_stat.get("cache", 0) + memory_stat.get("buffers", 0),
                } | memory_stat)


            if config.track_gpu_usage:
                gpu_metrics_loader = GPUMetricsLoader()
                gpu_metrics = gpu_metrics_loader.get_gpu_metrics()

                if gpu_metrics is not None:
                    res["content"]["gpu"] =  dataclasses.asdict(gpu_metrics)

            res["content"].update({"host_usage_flag": config.show_host_usage})
            out = json.dumps(res, default=json_default)
        client.stop_channels()
        self.write(out)
