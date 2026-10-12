"""Runtime profile: maps the SELECTED language-model profile ('lite' / 'pro') to an audio tier.

One codebase, two profiles. The only differences are Whisper's compute_type and whether models
are held in memory at once (parallel) or loaded one at a time (sequential).

- pro (MAX/FLASH): float16, models stay resident together.
- lite: int8_float16, models run sequentially; long recordings are chunked.

The UI passes the SELECTED profile to the worker (`select_profile_for`), so LITE and PRO behave
differently. Only outside the UI (CLI), when no profile name is given, does the machine's total
VRAM decide the tier (`select_profile`). The device/swap decision is still made from the REAL
free VRAM and system RAM (`choose_plan`).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace

# Tier names now match the SELECTED profile: 'lite' and 'pro'. The old hardware names
# ('pc1'/'pc2') and the UI profile names ('max'/'flash') are equivalent aliases, so old calls
# (CLI `--profile pc2`) keep working.
PC1 = 'lite'
PC2 = 'pro'
VRAM_THRESHOLD_MIB = 14000
# UI profile name -> tier. The profile NAME decides the tier; VRAM is only the fallback when
# the name is absent.
TIER_ALIASES = {
    'lite': PC1, 'pc1': PC1,
    'pro': PC2, 'max': PC2, 'flash': PC2, 'pc2': PC2,
}

# --- Automatic device selection (CPU / GPU) ----------------------------------
# Transcribing on the GPU requires unloading the local language model from VRAM and
# restoring it afterwards (swap). That swap has a fixed cost; transcription on the CPU is
# slower than the GPU but has NO swap cost. On short recordings the swap cost outweighs the
# transcription gain:
#
#     GPU_transcribe + swap_cost > CPU_transcribe  =>  choose CPU.
#
# The threshold is the duration below; when the R&D measurement lands, only this line is
# updated (or override per profile with `Profile.cpu_max_seconds`).
# AR-GE measurement (RTX 5060 Ti 16 GB, faster-whisper large-v3):
#   CPU(d)  = 5.0  + 0.908*d   (model load 5 s; transcription ~real time)
#   GPU(d)  = 14.0 + 3.3 + 0.079*d   (swap = unload 3 + restore 11; model load 3.3)
#   Break-even: d = (14 + 3.3 - 5.0) / (0.908 - 0.079) ~= 15 s
# 20 s was chosen for a safety margin + the first-job CUDA context (pro; old name PC2). LITE's swap was
# measured shorter (unload 3 + restore 6 = ~9 s) and the 9B model loads in ~6 s, so LITE keeps
# a slightly lower threshold (Profile.cpu_max_seconds = 15; see PROFILES). Both profiles use
# the "short -> CPU, long -> GPU + swap" logic.
DEFAULT_CPU_MAX_SECONDS = 20.0

# English edition: the standard Whisper large-v3 CTranslate2 model (no Turkish fine-tune).
DEFAULT_WHISPER_MODEL = 'Systran/faster-whisper-large-v3'
# AudioSet-based audio event model (AST). Set to 'none' to disable.
DEFAULT_EVENTS_MODEL = 'MIT/ast-finetuned-audioset-10-10-0.4593'

# --- Audio pipeline memory requirements (MiB) --------------------------------
# The device/swap decision is made from the REAL free VRAM and system RAM measured
# when the audio job starts, NOT from the profile name (see choose_plan). The LITE
# profile runs on both 8 GB (target machine) and 16 GB cards, so the profile name
# alone is not enough.
#
# `audio_vram_mib`: worst-case margin for Whisper large-v3 + AST + the torch/CUDA
# context. Measured: int8_float16 large-v3 ~1.5-2.5 GB, AST (fp32) ~0.4 GB, the
# torch/CUDA context ~0.3-0.5 GB; on pro (old name PC2) float16 Whisper grows to ~2.9 GB.
DEFAULT_AUDIO_VRAM_MIB = 4096
# `audio_ram_mib`: worst-case system RAM the pipeline holds on the CPU path (no
# swap): Whisper int8 (~2 GB) + AST (fp32 ~0.4 GB) + torch/Python (~0.8 GB) + the
# chunk/decode buffers (30 min float32 ~0.15 GB; chunked transcription).
DEFAULT_AUDIO_RAM_MIB = 5120


@dataclass(frozen=True)
class Profile:
    name: str
    compute_type: str      # faster-whisper compute_type ('float16' | 'int8_float16')
    load_mode: str         # 'parallel' | 'sequential'
    chunk_minutes: int     # split long recordings; 0 = no split
    whisper_model: str
    events_model: str      # 'ast' | 'none'
    language: str
    events: bool
    diarize: bool
    auto_device: bool = True              # choose cuda/cpu automatically from the recording duration
    cpu_max_seconds: float | None = None  # recordings shorter than this use the CPU (None -> DEFAULT_CPU_MAX_SECONDS)
    audio_vram_mib: int = DEFAULT_AUDIO_VRAM_MIB  # audio pipeline needs this much free VRAM alongside the model
    audio_ram_mib: int = DEFAULT_AUDIO_RAM_MIB    # CPU path needs this much system RAM for the audio pipeline


PROFILES = {
    PC2: Profile(
        name=PC2, compute_type='float16', load_mode='parallel', chunk_minutes=0,
        whisper_model=DEFAULT_WHISPER_MODEL, events_model=DEFAULT_EVENTS_MODEL,
        language='auto', events=True, diarize=True,
    ),
    PC1: Profile(
        name=PC1, compute_type='int8_float16', load_mode='sequential', chunk_minutes=30,
        whisper_model=DEFAULT_WHISPER_MODEL, events_model=DEFAULT_EVENTS_MODEL,
        language='auto', events=True, diarize=True,
        # LITE's swap was measured shorter (9B: unload ~3 + restore ~6 = ~9 s); the threshold
        # is slightly lower than PRO's (see the DEFAULT_CPU_MAX_SECONDS comment).
        cpu_max_seconds=15.0,
    ),
}


def total_vram_mib() -> int:
    """Total VRAM (MiB) of the largest GPU via nvidia-smi. 0 when unavailable (-> lite (old name PC1))."""
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return 0
    values = [int(x) for x in out.replace(',', ' ').split() if x.strip().isdigit()]
    return max(values) if values else 0


def free_vram_mib() -> int:
    """FREE VRAM (MiB) of the largest GPU via nvidia-smi. 0 when unavailable."""
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return 0
    values = [int(x) for x in out.replace(',', ' ').split() if x.strip().isdigit()]
    return max(values) if values else 0


def available_ram_mib() -> int:
    """Available (free) system RAM in MiB. 0 when it cannot be read."""
    if os.name == 'nt':
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys // (1024 * 1024))
        except Exception:
            return 0
        return 0
    try:
        with open('/proc/meminfo', encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def select_profile(total_mib: int | None = None, **overrides) -> Profile:
    """Selects a profile by total VRAM (only when no profile name is given / CLI fallback)."""
    total = total_vram_mib() if total_mib is None else int(total_mib)
    profile = PROFILES[PC2] if total >= VRAM_THRESHOLD_MIB else PROFILES[PC1]
    clean = {k: v for k, v in overrides.items() if v is not None and hasattr(profile, k)}
    return replace(profile, **clean) if clean else profile


def resolve_tier(name: str | None) -> str | None:
    """Maps a profile name to a tier key ('lite'/'pro'); None when unknown."""
    return TIER_ALIASES.get((name or '').strip().lower())


def select_profile_for(name: str | None, **overrides) -> Profile:
    """Selects a tier from the app's SELECTED profile name; the name wins.

    'lite' -> LITE, 'pro'/'max'/'flash' -> PRO. When the name is unknown (or empty) it falls
    back to `select_profile`, which decides the tier from the machine's total VRAM.
    """
    tier = resolve_tier(name)
    if tier is None:
        return select_profile(**overrides)
    profile = PROFILES[tier]
    clean = {k: v for k, v in overrides.items() if v is not None and hasattr(profile, k)}
    return replace(profile, **clean) if clean else profile


def choose_device(duration_seconds: float | None, profile: Profile, need_swap: bool,
                  cpu_max_seconds: float | None = None) -> str:
    """Chooses 'cuda' or 'cpu' from the recording duration.

    Rule (see DEFAULT_CPU_MAX_SECONDS):
      - need_swap=False: no VRAM swap cost (e.g. an API provider, or LITE where the language
        model fits alongside the audio pipeline) -> always 'cuda'.
      - need_swap=True: on the GPU the language model is unloaded and restored (swap). If the
        recording is SHORTER than the threshold the swap cost outweighs the gain, so 'cpu';
        otherwise 'cuda'.

    Threshold: `cpu_max_seconds` if given, else `profile.cpu_max_seconds`, else
    `DEFAULT_CPU_MAX_SECONDS`. If the duration is unknown (None) the safe default is 'cuda'.

    NOTE: the app path (UI/worker) makes the device/swap decision with `choose_plan`; this
    function is kept only for the CLI's `--device auto` flow.
    """
    if not need_swap:
        return 'cuda'
    threshold = cpu_max_seconds
    if threshold is None:
        threshold = profile.cpu_max_seconds
    if threshold is None:
        threshold = DEFAULT_CPU_MAX_SECONDS
    if duration_seconds is not None and duration_seconds <= threshold:
        return 'cpu'
    return 'cuda'


def choose_plan(profile: Profile, duration_seconds: float | None = None,
                vram_free_mib: int | None = None, ram_free_mib: int | None = None,
                local_model_loaded: bool = True,
                cpu_max_seconds: float | None = None) -> tuple[str, bool]:
    """Returns the (device, need_swap) plan for the audio pipeline, in PRIORITY order.

    The tier (`profile`) comes from the SELECTED profile name (lite/pro); the device/swap
    decision is made from the tier's memory thresholds + REAL free VRAM + system RAM and the
    recording DURATION (the "short -> CPU, long -> GPU" economy):

      1) Whisper + AST fit in free VRAM ALONGSIDE the local language model
         (`audio_vram_mib`) -> ('cuda', False): GPU transcription, NO swap (fastest, free).
      2) They do not fit and the recording is SHORTER than the threshold (swap cost
         outweighs the gain), and the pipeline fits in system RAM on the CPU
         (`audio_ram_mib`) -> ('cpu', False): NO swap; slower, but the language model
         stays resident in VRAM.
      3) Otherwise (LONG recording; CPU does not fit in RAM; or duration unknown and RAM is
         insufficient) -> ('cuda', True): GPU transcription. The language model is unloaded
         and restored (swap) ONLY if required. On a long recording the GPU gain outweighs
         the swap cost; if RAM is insufficient the swap is unavoidable anyway.

    If the local language model is not loaded (API provider) the GPU is already free
    -> ('cuda', False). When `vram_free_mib`/`ram_free_mib` are omitted they are read
    from the machine (if unreadable -> 0, i.e. assumed not to fit).
    """
    if not local_model_loaded:
        return 'cuda', False
    if vram_free_mib is None:
        vram_free_mib = free_vram_mib()
    if ram_free_mib is None:
        ram_free_mib = available_ram_mib()
    if vram_free_mib >= profile.audio_vram_mib:
        return 'cuda', False
    ram_ok = ram_free_mib >= profile.audio_ram_mib
    # If the duration is unknown, decide only from memory (do not force a swap).
    if duration_seconds is None:
        return ('cpu', False) if ram_ok else ('cuda', True)
    threshold = cpu_max_seconds
    if threshold is None:
        threshold = profile.cpu_max_seconds
    if threshold is None:
        threshold = DEFAULT_CPU_MAX_SECONDS
    short = duration_seconds <= threshold
    if short and ram_ok:
        return 'cpu', False
    return 'cuda', True
