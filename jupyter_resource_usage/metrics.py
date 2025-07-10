try:
    import psutil
except ImportError:
    psutil = None

try:
    import pynvml
    pynvml.nvmlInit()
except:
    # pynvml is not installed or the NVIDIA driver is not available
    pynvml = None 

from dataclasses import dataclass
import os
from tornado.concurrent import run_on_executor
from jupyter_server.serverapp import ServerApp


class PSUtilMetricsLoader:
    def __init__(self, server_app: ServerApp):
        self.config = server_app.web_app.settings[
            "jupyter_resource_usage_display_config"
        ]
        self.server_app = server_app

    def get_process_metric_value(self, process, name, args, kwargs, attribute=None):
        try:
            # psutil.Process methods will either return...
            metric_value = getattr(process, name)(*args, **kwargs)
            if attribute is not None:  # ... a named tuple
                return getattr(metric_value, attribute)
            else:  # ... or a number
                return metric_value
        # Avoid littering logs with stack traces
        # complaining about dead processes
        except BaseException:
            return 0

    def process_metric(self, name, args=[], kwargs={}, attribute=None):
        if psutil is None:
            return None
        else:
            current_process = psutil.Process()
            all_processes = [current_process] + current_process.children(recursive=True)

            process_metric_value = lambda process: self.get_process_metric_value(
                process, name, args, kwargs, attribute
            )

            return sum([process_metric_value(process) for process in all_processes])

    def system_metric(self, name, args=[], kwargs={}, attribute=None):
        if psutil is None:
            return None
        else:
            # psutil functions will either raise an error, or return...
            try:
                metric_value = getattr(psutil, name)(*args, **kwargs)
            except:
                return None
            if attribute is not None:  # ... a named tuple
                return getattr(metric_value, attribute)
            else:  # ... or a number
                return metric_value

    def get_metric_values(self, metrics, metric_type):
        metric_types = {"process": self.process_metric, "system": self.system_metric}
        metric_value = metric_types[metric_type]  # Switch statement

        metric_values = {}
        for metric in metrics:
            name = metric["name"]
            if metric.get("attribute", False):
                name += "_" + metric.get("attribute")
            metric_values.update({name: metric_value(**metric)})
        return metric_values

    def metrics(self, process_metrics, system_metrics):
        metric_values = {}
        if process_metrics:
            metric_values.update(self.get_metric_values(process_metrics, "process"))
        if system_metrics:
            metric_values.update(self.get_metric_values(system_metrics, "system"))

        if any(value is None for value in metric_values.values()):
            return None

        return metric_values

    def memory_metrics(self):
        return self.metrics(
            self.config.process_memory_metrics, self.config.system_memory_metrics
        )

    def cpu_metrics(self):
        return self.metrics(
            self.config.process_cpu_metrics, self.config.system_cpu_metrics
        )

    def disk_metrics(self):
        return self.metrics(
            self.config.process_disk_metrics, self.config.system_disk_metrics
        )


class ContainerMetricsLoader:
    @staticmethod
    def cpu_count():
        v2_cpu_max = "/sys/fs/cgroup/cpu.max"
        v1_cpu_quota = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
        v1_cpu_period = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"

        if os.path.exists(v2_cpu_max):
            with open(v2_cpu_max) as cpu:
                cpu_quota, cpu_period = cpu.read().strip().split(" ")
        elif os.path.exists(v1_cpu_quota) and os.path.exists(v1_cpu_period):
            with open(v1_cpu_quota) as cpu:
                cpu_quota = cpu.read().strip()
            with open(v1_cpu_period) as period:
                cpu_period = period.read().strip()
        else:
            return psutil.cpu_count()
        
        if cpu_quota == "max" or cpu_quota == "-1":
            return psutil.cpu_count()

        return int(cpu_quota) // int(cpu_period) if int(cpu_period) > 0 else 1
    
    @classmethod
    def cpu_percent(cls):
        if psutil is None:
            return 0
        cur_process = psutil.Process()
        all_processes = [cur_process] + cur_process.children(recursive=True)

        def get_cpu_percent(p):
            try:
                return p.cpu_percent(interval=0.05)
            except:
                return 0

        percent_sum = sum([get_cpu_percent(p) for p in all_processes])
        cpu_count = cls.cpu_count()

        return percent_sum / cpu_count if cpu_count > 0 else percent_sum

    @staticmethod
    def physical_memory():
        v2_mem_limit = '/sys/fs/cgroup/memory.max'
        v1_mem_limit = '/sys/fs/cgroup/memory/memory.limit_in_bytes'

        if os.path.exists(v2_mem_limit):
            with open(v2_mem_limit) as limit:
                mem_limit_str = limit.read().strip()
            mem_limit = int(mem_limit_str) if mem_limit_str != "max" else psutil.virtual_memory().total
        elif os.path.exists(v1_mem_limit):
            with open(v1_mem_limit) as limit:
                mem_limit = int(limit.read().strip())
        else:
            return psutil.virtual_memory().total
        
        return mem_limit

    @staticmethod
    def memory_pss():
        v2_mem_usage = "/sys/fs/cgroup/memory.current"
        v1_mem_usage = "/sys/fs/cgroup/memory/memory.usage_in_bytes"

        if os.path.exists(v2_mem_usage):
            with open(v2_mem_usage) as usage:
                mem_usage = int(usage.read().strip())
        elif os.path.exists(v1_mem_usage):
            with open(v1_mem_usage) as usage:
                mem_usage = int(usage.read().strip())
        else:
            return psutil.virtual_memory().used

        return mem_usage

    @staticmethod
    def memory_rss():
        v2_mem_stat = "/sys/fs/cgroup/memory.stat"
        v1_mem_stat = "/sys/fs/cgroup/memory/memory.stat"

        if os.path.exists(v2_mem_stat):
            stat_path = v2_mem_stat
        elif os.path.exists(v1_mem_stat):
            stat_path = v1_mem_stat
        else:
            return psutil.virtual_memory().used

        real_rss = 0
        with open(stat_path) as stats:
            for line in stats:
                stat = line.split()
                if stat[0] in ['rss', 'inactive_file', 'active_file']:
                    real_rss += int(stat[1])
        return real_rss

    @staticmethod
    def memory_stat():
        v2_mem_stat = "/sys/fs/cgroup/memory.stat"

        if os.path.exists(v2_mem_stat):
            stat_path = v2_mem_stat

        stats = {}
        with open(stat_path, "r") as f:
            for line in f:
                k, v = line.split()
                stats[k] = int(v)

        active = stats.get("active_anon", 0) + stats.get("active_file", 0)
        inactive = stats.get("inactive_anon", 0) + stats.get("inactive_file", 0)
        slab = stats.get("slab", 0)
        shared = stats.get("shmem", 0)
        buffers = stats.get("buffers", 0)
        cached = stats.get("cache", stats.get("file", 0))

        result = {
            "active": active,
            "inactive": inactive,
            "buffers": buffers,
            "cached": cached,
            "shared": shared,
            "slab": slab,
        }

        return result

@dataclass
class GPUMetrics:
    power: int
    temperature: int
    gpu_clock: int
    gpu_clock_max: int
    sm_clock: int
    sm_clock_max: int
    mem_clock: int
    mem_clock_max: int
    mem_total: int
    mem_used: int
    mem_free: int
    is_mig: bool = False

class GPUMetricsLoader:
    @staticmethod
    def get_gpu_metrics() -> GPUMetrics | None:
        if pynvml is None:
            return None

        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)  # Get the first GPU
        except pynvml.NVMLError as e:
            print(f"Error getting GPU handle: {e}")
            return None

        power = pynvml.nvmlDeviceGetPowerUsage(handle) // 1000
        temperature = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        gpu_clock = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
        gpu_clock_max = pynvml.nvmlDeviceGetMaxClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
        sm_clock = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
        sm_clock_max = pynvml.nvmlDeviceGetMaxClockInfo(handle, pynvml.NVML_CLOCK_SM)
        mem_clock = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
        mem_clock_max = pynvml.nvmlDeviceGetMaxClockInfo(handle, pynvml.NVML_CLOCK_MEM)
        mem_total = 0
        mem_used = 0
        mem_free = 0

        mig_mode, _ = pynvml.nvmlDeviceGetMigMode(handle)
        if mig_mode != pynvml.NVML_DEVICE_MIG_ENABLE:
            is_mig = False
            try:
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                mem_total = mem_info.total
                mem_used = mem_info.used
                mem_free = mem_info.free
            except pynvml.NVMLError as e:
                print(f"Error getting memory info: {e}")
        else:
            is_mig = True
            try:
                mig_handle = pynvml.nvmlDeviceGetMigDeviceHandleByIndex(handle, 0)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(mig_handle)
                mem_total = mem_info.total
                mem_used = mem_info.used
                mem_free = mem_info.free
            except pynvml.NVMLError as e:
                print(f"Error getting MIG info: {e}")

        return GPUMetrics(
            power=power,
            temperature=temperature,
            gpu_clock=gpu_clock,
            gpu_clock_max=gpu_clock_max,
            sm_clock=sm_clock,
            sm_clock_max=sm_clock_max,
            mem_clock=mem_clock,
            mem_clock_max=mem_clock_max,
            mem_total=mem_total,
            mem_used=mem_used,
            mem_free=mem_free,
            is_mig=is_mig
        )

        