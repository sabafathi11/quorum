"""One SAM 3.1 in VRAM, doing all three jobs.

Before this, there were two copies on the card: the nuclio function held a
tracker-only assembly (4,902 MiB, resident from deploy) and every auto run
started a container that built the *full* predictor again. Measured on the
3090 Ti: an auto run peaked the card at 23,841 of 24,564 MiB — 97% — with two
other tenants on it.

They could not simply be merged, because they were not the same model. The
nuclio handler builds `build_sam3_multiplex_video_model` and drops the text
encoder on purpose ("detector.backbone.language_backbone.* -> we skip this"),
so it *cannot* answer a text prompt; the auto pipeline builds
`build_sam3_multiplex_video_predictor` because it must. The full predictor,
though, contains the other one:

    Sam3MultiplexVideoPredictor                 .handle_request  (text, video)
      .model      Sam3MultiplexTrackingWithInteractivity
        .tracker.model  Sam3VideoTrackingMultiplexDemo          <- the interactor
                        carries interactive_sam_prompt_encoder /
                        interactive_sam_mask_decoder, image_size 1008

So one full predictor serves everything, and this process is that predictor
plus three ways to ask it something:

    POST /           the nuclio protocol, byte for byte — interactor and
                     tracker. Quorum's client is unchanged, and so is CVAT's
                     if it ever comes back.
    POST /auto       a text-prompt annotation run over a whole video. Runs the
                     vendored `annotator/main.py` *in this process*, on this
                     predictor, so it costs session state and not a second
                     copy of the weights.
    GET  /auto/<id>  progress, as the pipeline's own stdout lines. Quorum parses
                     them with the same code it used when this was a container,
                     so the contract did not change when the transport did.

**One thing at a time.** A lock serialises every GPU call. That is not a
limitation this introduces — the nuclio function ran `numWorkers: 1` and the
auto pipeline was never safe to run twice — it is the same constraint, now
stated in one place. An interactor click during an auto run waits for the
current window, which is seconds.
"""
import base64
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

GPU = threading.Lock()          # one thing on the card at a time

CKPT = os.environ.get("SAM_CKPT", "/opt/nuclio/sam/sam3.1_multiplex_fp16.safetensors")
MAX_OBJECTS = int(os.environ.get("SAM_MAX_OBJECTS", "20"))
PORT = int(os.environ.get("SAM_PORT", "8080"))
ANNOTATOR = os.environ.get("SAM_ANNOTATOR", "/annotator")

import numpy as np
import torch
from PIL import Image


# ---------------------------------------------------------------- checkpoint
# These two patches are lifted from `annotator/main.py`, which is the only
# reason the ComfyUI safetensors repack loads at all: the builder calls
# `torch.load(weights_only=True)`, which refuses a safetensors file, and the
# checkpoint's keys are prefixed `tracker.` / `detector.` while the modules are
# not. If the vendored pipeline ever changes them, this fails loudly at
# startup rather than quietly loading half a model.
_torch_load = torch.load


def _patched_torch_load(f, *a, **kw):
    fn = getattr(f, "name", str(f))
    if fn.endswith(".safetensors"):
        from safetensors.torch import load_file
        return {k: v.float() for k, v in load_file(fn).items()}
    return _torch_load(f, *a, **kw)


_load_state_dict = torch.nn.Module.load_state_dict


def _patched_load_state_dict(self, state_dict, *a, **kw):
    out = {}
    for key in self.state_dict().keys():
        for cand in (key, f"tracker.model.{key}", f"tracker.{key}", f"detector.{key}"):
            if cand in state_dict:
                out[key] = state_dict[cand]
                break
    kw["strict"] = False
    return _load_state_dict(self, out, *a, **kw)


torch.load = _patched_torch_load
torch.nn.Module.load_state_dict = _patched_load_state_dict
torch.set_grad_enabled(False)

# Autocast and grad mode are **thread-local**, and this is a threaded server.
# `annotator/main.py` can enter autocast once at the top because it is a
# single-process script; do the same here and every request — which arrives on
# some other thread — runs without it, meets fp32 weights with bf16 activations
# and dies with "mat1 and mat2 must have the same dtype". So the GPU is entered
# through one context manager that carries the lock *and* the dtype regime, and
# nothing touches the card outside it.
import contextlib


@contextlib.contextmanager
def gpu():
    with GPU, torch.autocast(device_type="cuda", dtype=torch.bfloat16), torch.no_grad():
        yield


# ------------------------------------------------------------------ the model
def vram_mib():
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        used, total = (int(x) for x in out.strip().splitlines()[0].split(","))
        return {"used": used, "total": total, "free": total - used}
    except Exception:
        return {}


print("building the predictor…", flush=True)
_t0 = time.time()
from sam3.model_builder import build_sam3_multiplex_video_predictor

PREDICTOR = build_sam3_multiplex_video_predictor(
    checkpoint_path=CKPT, use_fa3=False, max_num_objects=MAX_OBJECTS)

# The interactor needs the tracker assembly that lives *inside* the predictor —
# reached rather than rebuilt, which is the whole point of this file.
TRACKER_MODEL = PREDICTOR.model.tracker.model
for need in ("interactive_sam_prompt_encoder", "interactive_sam_mask_decoder"):
    if not any(n.endswith(need) for n, _ in TRACKER_MODEL.named_modules()):
        raise SystemExit(
            f"the predictor's tracker model has no {need}: this build of sam3 does not "
            f"carry the interactive head, so one model cannot serve both roles")


def _resolve(root, path):
    obj = root
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def attach_backbone():
    """Give the tracker the vision backbone it does not own.

    Standalone (how CVAT builds it) the tracker is constructed
    `with_backbone=True` and holds its own `SAM3VLBackbone`. Inside the full
    predictor it does not: the detector owns the backbone and the tracker is
    fed features, so `tracker.model.backbone` is None and `forward_image` — the
    first thing the interactor calls — dies on it.

    The weights are already on the card either way, so this is a reference, not
    a copy: attaching the detector's backbone costs nothing and is what makes
    one model able to do both jobs.
    """
    if getattr(TRACKER_MODEL, "backbone", None) is not None:
        return "the tracker already had a backbone"
    names = [n for n in dir(PREDICTOR.model) if not n.startswith("_")]
    print(f"[service] predictor.model attributes: {names}", flush=True)
    for path in ("detector.backbone", "backbone", "tracker.backbone",
                 "detector.model.backbone", "demo_model.backbone"):
        cand = _resolve(PREDICTOR.model, path)
        if cand is not None and hasattr(cand, "forward_image"):
            TRACKER_MODEL.backbone = cand
            return f"attached predictor.model.{path} ({type(cand).__name__})"
    raise SystemExit(
        "the tracker has no backbone and none was found on the predictor — the "
        "interactor cannot run without one. Candidates tried: detector.backbone, "
        "backbone, tracker.backbone, detector.model.backbone, demo_model.backbone")


print(f"[service] {attach_backbone()}", flush=True)


# ------------------------------------------------- the SAM3 head's flat keys
# The tracker and the interactor ask the same backbone for different things,
# and only one of those two questions has an answer the tracker can hold.
#
# `SAM3VLBackboneTri.forward_image` returns the *interactive* and *propagation*
# necks as named sub-dicts, but the SAM3 detection neck flat at the top level:
#
#     {"vision_features": T, "vision_mask": T, "vision_pos_enc": […],
#      "backbone_fpn": […], "interactive": {…}, "sam2_backbone_out": {…}}
#
# The tracker's own `forward_image` then finishes with a clone-for-compile pass
# that assumes every top-level key is a neck:
#
#     for out_type in backbone_out.keys():
#         for i in range(len(backbone_out[out_type]["backbone_fpn"])):
#
# so it reaches `backbone_out["vision_features"]["backbone_fpn"]`, subscripts a
# 4-D tensor with a string, and dies with `IndexError: too many indices for
# tensor of dimension 4`.
#
# The interactor never met this because `InteractiveModelWrapper.forward_image`
# leaves `need_sam3_out` at its default of False, so the flat keys are never
# produced. The tracker's `_get_image_feature` passes all three as True, which
# is why *propagation* was the one thing that broke here and clicking was fine.
#
# CVAT hit the same wall and patched `TriHeadVisionOnly.forward_image` to nest
# those four keys under `sam3_out`. That patch lives in `ModelHandler.__init__`,
# which `build_handler` deliberately never runs — and it would not have helped
# anyway: it names `TriHeadVisionOnly`, while the backbone this assembly hands
# the tracker is the detector's `SAM3VLBackboneTri`. A sibling class, same
# defect, different name.
#
# So do the nesting here, and do it on the *tracker's reference only*. Patching
# the class, or the shared instance, would nest the keys for the detector too —
# and the detector is the one component that actually reads the SAM3 neck, so
# that would trade a broken tracker for a broken auto-annotator. Nothing
# consumes `sam3_out` downstream (`neck_outs` is `["interactive",
# "sam2_backbone_out"]`); it only has to be shaped like a neck so the clone pass
# can walk over it.
SAM3_FLAT_KEYS = ("vision_features", "vision_mask", "vision_pos_enc", "backbone_fpn")


class _NestSam3Out:
    """The detector's backbone, seen by the tracker with its SAM3 neck named.

    Delegates everything else by reference — this holds no weights and copies
    none, exactly like the attachment it wraps.
    """

    def __init__(self, backbone):
        self._backbone = backbone

    def __getattr__(self, name):
        return getattr(self._backbone, name)

    def forward_image(self, *args, **kwargs):
        out = self._backbone.forward_image(*args, **kwargs)
        if isinstance(out, dict) and all(k in out for k in SAM3_FLAT_KEYS):
            out["sam3_out"] = {k: out.pop(k) for k in SAM3_FLAT_KEYS}
        return out


def nest_sam3_out():
    real = getattr(TRACKER_MODEL, "backbone", None)
    if real is None:
        raise SystemExit("nest_sam3_out must run after attach_backbone")
    if isinstance(real, _NestSam3Out):
        return "the tracker's backbone was already wrapped"
    # `backbone` is a registered child module, and nn.Module.__setattr__ refuses
    # to put a non-Module in a slot that holds one. Drop the registration first:
    # the detector owns this module and keeps it on the card, in eval, and in
    # its own `named_modules()` — the tracker only ever needed the reference.
    TRACKER_MODEL._modules.pop("backbone", None)
    object.__setattr__(TRACKER_MODEL, "backbone", _NestSam3Out(real))
    return f"wrapped the tracker's {type(real).__name__} to name its SAM3 neck"


print(f"[service] {nest_sam3_out()}", flush=True)
print(f"predictor ready in {time.time() - _t0:.0f}s  {vram_mib()}", flush=True)


# ------------------------------------------------------- the nuclio handlers
# `model_handler.py` is CVAT's, copied verbatim. Its `__init__` builds a model,
# which is exactly what must not happen twice — so the object is constructed
# without it and given the four attributes its methods actually use. The
# wrapper below is the class CVAT defines *inside* that `__init__`, lifted out
# unchanged because a local class cannot be imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_handler import ModelHandler
from sam3.model.sam1_task_predictor import SAM3InteractiveImagePredictor
import torchvision.transforms


class InteractiveModelWrapper:
    def __init__(self, model):
        self.model = model
        self._cached_prompt_encoder = None
        self._cached_mask_decoder = None

    def forward_image(self, input_image):
        return self.model.forward_image(input_image, need_interactive_out=True)

    def _prepare_backbone_features(self, backbone_out):
        feats = self.model._prepare_backbone_features(backbone_out)
        inter = feats["interactive"]
        return None, inter["vision_feats"], None, None

    @property
    def image_size(self):
        return self.model.image_size

    @property
    def no_mem_embed(self):
        # Fallback to 0.0 broadcast scalar for models lacking this param
        if hasattr(self.model, "no_mem_embed"):
            return self.model.no_mem_embed
        return 0.0

    @property
    def sam_prompt_encoder(self):
        if self._cached_prompt_encoder is not None:
            return self._cached_prompt_encoder

        # Dynamically search the module tree for the prompt encoder
        modules = dict(self.model.named_modules())
        # ADDED: 'interactive_sam_prompt_encoder' to match SAM 3.1 Multiplex internals
        for target in ["interactive_sam_prompt_encoder", "sam_prompt_encoder", "prompt_encoder"]:
            for name, module in modules.items():
                if name == target or name.endswith("." + target):
                    self._cached_prompt_encoder = module
                    return module
        raise AttributeError("Could not find prompt encoder in model tree")

    @property
    def sam_mask_decoder(self):
        if self._cached_mask_decoder is not None:
            return self._cached_mask_decoder

        # Dynamically search prioritizing the interactive mask decoder
        modules = dict(self.model.named_modules())
        for target in ["interactive_sam_mask_decoder", "sam_mask_decoder", "mask_decoder"]:
            for name, module in modules.items():
                if name == target or name.endswith("." + target):
                    self._cached_mask_decoder = module
                    return module
        raise AttributeError("Could not find mask decoder in model tree")

    @property
    def device(self):
        return self.model.device


def build_handler():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    h = ModelHandler.__new__(ModelHandler)          # deliberately not __init__
    h.device = device
    h.checkpoint_path = CKPT
    h.video_predictor = TRACKER_MODEL
    h.video_predictor.device = device
    h.image_predictor = SAM3InteractiveImagePredictor(InteractiveModelWrapper(TRACKER_MODEL))
    h.transform = torchvision.transforms.Compose([
        torchvision.transforms.Resize((TRACKER_MODEL.image_size, TRACKER_MODEL.image_size)),
        torchvision.transforms.ToTensor(),
        torchvision.transforms.Normalize(mean=(0.485, 0.456, 0.406),
                                         std=(0.229, 0.224, 0.225)),
    ])
    return h


HANDLER = build_handler()


# ----------------------------------------------------------------- auto jobs
class Cancelled(Exception):
    pass


class Job:
    """One auto-annotation run of the vendored pipeline, in this process."""

    def __init__(self, params):
        self.id = uuid.uuid4().hex[:12]
        self.params = params
        self.state = "queued"
        self.lines: list[str] = []
        self.error = ""
        self.started = time.time()
        self.finished = None
        self.cancel = threading.Event()

    # tqdm redraws a progress bar with a carriage return several times a
    # second; captured as stdout that is ~900 lines of noise per window, all of
    # it transport for a poller. The pipeline's *own* progress lines — the ones
    # `plugins/sam/auto.py` parses — are what matter, so the bars are dropped
    # here rather than shipped and filtered at the far end.
    BAR = re.compile(r"\d+%\|[#| ]|it/s\]|s/it\]")

    def write(self, text):                 # stands in for sys.stdout
        for line in text.splitlines():
            line = line.strip()
            if line and not self.BAR.search(line):
                self.lines.append(line)
                if len(self.lines) > 4000:      # a long run must not grow forever
                    del self.lines[:1000]

    def flush(self):
        pass

    def as_dict(self, since=0):
        return {"id": self.id, "state": self.state, "error": self.error,
                "lines": self.lines[since:], "n_lines": len(self.lines),
                "seconds": round((self.finished or time.time()) - self.started, 1)}


JOBS: dict[str, Job] = {}
_annotator_ready = False


def _load_annotator():
    """Import the vendored pipeline and make it use *our* predictor.

    Two monkey-patches, both aimed at the same thing — the pipeline must not
    build a second model:

      · its builder returns the one this process already holds;
      · `apply_memory_fixes` wraps methods on that model, so running it once
        per job would stack a new wrapper layer every time. It runs once.

    And one that the container version got for free: `track_window` is the
    per-window unit of work, so checking a flag there is where a cancel lands —
    within seconds, at a boundary the pipeline already treats as safe.
    """
    global _annotator_ready
    if _annotator_ready:
        return sys.modules["annotator_main"]
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "annotator_main", os.path.join(ANNOTATOR, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["annotator_main"] = mod
    spec.loader.exec_module(mod)

    mod.build_sam3_multiplex_video_predictor = lambda **kw: PREDICTOR

    fixes = mod.apply_memory_fixes
    applied = []

    def once(*a, **kw):
        if applied:
            print("[service] memory fixes already applied to this predictor", flush=True)
            return
        applied.append(True)
        return fixes(*a, **kw)
    mod.apply_memory_fixes = once

    inner = mod.track_window

    def guarded(*a, **kw):
        job = CURRENT.get("job")
        if job is not None and job.cancel.is_set():
            raise Cancelled()
        return inner(*a, **kw)
    mod.track_window = guarded

    _annotator_ready = True
    return mod


CURRENT: dict = {}


def run_job(job: Job):
    mod = _load_annotator()
    argv = ["main.py", "--video", job.params["video"], "--output", job.params["output"]]
    for p in job.params.get("prompts", []):
        argv += ["--text_prompt", p]
    for l in job.params.get("labels", []):
        argv += ["--label_name", l]
    argv += list(job.params.get("flags", []))

    # "running" only once the card is actually ours. Saying it while queued
    # behind another job makes a waiting job look like a stalled one.
    job.state = "waiting" if GPU.locked() else "running"
    old_argv, old_out, old_err = sys.argv, sys.stdout, sys.stderr
    with gpu():
        job.state = "running"
        CURRENT["job"] = job
        sys.argv, sys.stdout, sys.stderr = argv, job, job
        try:
            mod.main()
            job.state = "done"
        except Cancelled:
            job.state = "cancelled"
        except SystemExit as e:
            job.state = "done" if not e.code else "failed"
            job.error = "" if not e.code else f"the pipeline exited {e.code}"
        except BaseException as e:
            job.state = "failed"
            job.error = f"{type(e).__name__}: {e}"
            job.write(traceback.format_exc())
        finally:
            sys.argv, sys.stdout, sys.stderr = old_argv, old_out, old_err
            CURRENT.pop("job", None)
            job.finished = time.time()
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
    print(f"[service] job {job.id} {job.state} in {job.finished - job.started:.0f}s",
          flush=True)


# ---------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, *a):
        pass

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/"):
            job = CURRENT.get("job")
            return self._send({"ok": True, "vram": vram_mib(),
                               "busy": GPU.locked(), "job": job.id if job else None,
                               "max_objects": MAX_OBJECTS})
        if path.startswith("/auto/"):
            jid = path[len("/auto/"):].strip("/")
            job = JOBS.get(jid)
            if job is None:
                return self._send({"error": f"no job {jid}"}, 404)
            since = 0
            if "?" in self.path:
                from urllib.parse import parse_qs
                since = int(parse_qs(self.path.split("?", 1)[1]).get("since", ["0"])[0])
            return self._send(job.as_dict(since))
        self._send({"error": "not found"}, 404)

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path.startswith("/auto/") and path.endswith("/cancel"):
                jid = path[len("/auto/"):-len("/cancel")]
                job = JOBS.get(jid)
                if job is None:
                    return self._send({"error": f"no job {jid}"}, 404)
                job.cancel.set()
                return self._send({"id": job.id, "state": job.state, "cancelling": True})

            if path == "/auto":
                body = self._body()
                for need in ("video", "output", "prompts"):
                    if not body.get(need):
                        return self._send({"error": f"{need} is required"}, 400)
                job = Job(body)
                JOBS[job.id] = job
                threading.Thread(target=run_job, args=(job,), daemon=True).start()
                return self._send({"id": job.id, "state": job.state})

            # Anything else is the nuclio protocol, unchanged.
            return self._nuclio(self._body())
        except Exception as e:
            traceback.print_exc()
            self._send({"error": f"{type(e).__name__}: {e}"}, 500)

    def _nuclio(self, data):
        """CVAT's `main:handler`, reproduced. Same request and response shapes,
        so `plugins/sam/client.py` did not change when this replaced nuclio."""
        if not data:
            return self._send({"error": "empty request"}, 400)

        if "shapes" in data:
            shapes = data.get("shapes")
            states = data.get("states") or []
            images = []
            if "images" in data:
                for b64 in data["images"]:
                    images.append(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"))
            else:
                images.append(Image.open(io.BytesIO(base64.b64decode(data["image"]))).convert("RGB"))
            st = [states[i] if i < len(states) else None for i in range(len(shapes))]
            with gpu():
                out, new_states = HANDLER.handle_track_batch(images, shapes, st)
            if len(images) == 1:
                out = [s[0] for s in out]
            return self._send({"shapes": out, "states": new_states})

        image = Image.open(io.BytesIO(base64.b64decode(data["image"])))
        with gpu():
            rle = HANDLER.handle_interact(image, data.get("pos_points", []),
                                          data.get("neg_points", []), data.get("obj_bbox"))
        if not rle:
            return self._send({"shapes": []})
        return self._send({"shapes": [{"points": rle, "type": "mask"}]})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"sam-service on :{PORT}  one predictor, {vram_mib()}", flush=True)
    srv.serve_forever()
