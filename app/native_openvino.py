"""Run only InsightFace's existing ONNX detector and embedder with native OpenVINO."""
from pathlib import Path
import time

import numpy as np

PROVIDER = "OpenVINONative"
NATIVE_BACKENDS = {
    "openvino_gpu": ("GPU", "CPU"),
    "openvino_cpu": ("CPU",),
}
WARMUP_RUNS = 3  # GPU kernels finish tuning over the first few inferences.


class NativeSession:
    """Keep InsightFace's ONNX input metadata and named output order.

    With a static ``shape`` the model is compiled for exactly that input (batch
    1, as benchmarked); larger batches run row by row with identical results.
    """

    def __init__(self, core, model, device, shape, config=None):
        self.original = model.session
        self.on_failure = None
        graph = core.read_model(str(model.model_file))
        self.inputs = self.original.get_inputs()
        self.outputs = self.original.get_outputs()
        if len(self.inputs) != 1:
            raise ValueError("Expected one model input")
        if shape is not None:
            graph.reshape({self.inputs[0].name: list(shape)})
        self.compiled = core.compile_model(graph, device, {
            "PERFORMANCE_HINT": "LATENCY", "INFERENCE_PRECISION_HINT": "f32", **(config or {}),
        })
        devices = list(self.compiled.get_property("EXECUTION_DEVICES"))
        if len(devices) != 1 or devices[0].split(".")[0] != device:
            raise RuntimeError("Unexpected OpenVINO execution device")
        self.device = devices[0]
        self.input = self.compiled.input(self.inputs[0].name)
        self.output_ports = {item.name: self.compiled.output(item.name) for item in self.outputs}
        if shape is not None:
            self.fixed_single = shape[0] == 1
        else:
            batch = self.input.partial_shape[0]
            self.fixed_single = batch.is_static and batch.get_length() == 1

    def get_inputs(self):
        return self.inputs

    def get_outputs(self):
        return self.outputs

    def get_providers(self):
        return [PROVIDER]

    def run(self, output_names, input_feed):
        names = [item.name for item in self.outputs] if output_names is None else output_names
        ports = [self.output_ports[name] for name in names]
        sample = input_feed[self.inputs[0].name]

        def infer(batch):
            result = self.compiled({self.input: batch})
            return [np.asarray(result[port]).copy() for port in ports]

        try:
            if self.fixed_single and len(sample) > 1:
                rows = [infer(sample[index:index + 1]) for index in range(len(sample))]
                return [np.concatenate(parts, axis=0) for parts in zip(*rows, strict=True)]
            return infer(sample)
        except Exception:
            if self.on_failure is not None:
                self.on_failure()
            raise


def input_shapes(models, detector_size):
    width, height = models["recognition"].input_size
    return {"detection": (1, 3, detector_size, detector_size), "recognition": (1, 3, height, width)}


def prepare_sessions(models, device, detector_size, cache_dir, cpu_threads=0):
    import openvino as ov

    input_sizes = getattr(models["detection"], "input_sizes", None)
    if input_sizes and any(tuple(size) != (detector_size, detector_size) for size in input_sizes):
        raise ValueError("Expected the configured single detector resolution")
    core = ov.Core()
    if device == "GPU":
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        core.set_property({"CACHE_DIR": str(cache_dir)})
    # OpenVINO CPU would otherwise take every core from Frigate and the reader.
    config = {"INFERENCE_NUM_THREADS": int(cpu_threads)} if device == "CPU" and cpu_threads else {}
    shapes = input_shapes(models, detector_size)
    started = time.perf_counter()
    sessions = {task: NativeSession(core, models[task], device, shapes[task], config) for task in shapes}
    setup_ms = round((time.perf_counter() - started) * 1000, 2)
    started = time.perf_counter()
    for _ in range(WARMUP_RUNS):
        for task, session in sessions.items():
            values = session.run(None, {session.inputs[0].name: np.zeros(shapes[task], np.float32)})
            if not values or any(value.size == 0 or not np.isfinite(value).all() for value in values):
                raise RuntimeError("Invalid OpenVINO warmup output")
    return sessions, setup_ms, round((time.perf_counter() - started) * 1000, 2)
