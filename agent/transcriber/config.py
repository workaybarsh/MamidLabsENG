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

# LITE (8 GB card) CPU/GPU threshold. LITE's swap was measured shorter (~9-14 s); the
# break-even is d = (swap - 1.7) / 0.831 ~= 9-15 s. 15 s was chosen for a safety margin.
# When the measurement agent's numeric verdict is unavailable the existing R&D value is used;
# the safe band is 15-20 s.
LITE_CPU_MAX_SECONDS = 15.0

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

# LITE (8 GB card) CPU co-resident audio-pipeline RAM threshold. TWO measurements conflicted;
# the more DEFINITIVE one (measurement-2, which actually ran the LM and the ASR CPU pipeline
# TOGETHER) concludes: co-resident **DOES NOT FIT**. Physical RAM 15.25 GB; with the LM loaded
# the requirement is ~16.1 GB (zero/negative margin; peak ~15127/15611 MB, only ~489 MB free).
# There is no OOM, but the LM's ~6.1 GB model pages get evicted from the reclaimable cache and
# are re-read from disk on the NEXT chat turn (slowdown). So LITE uses the SAME safe gate as
# PRO: **5120 MiB** (the real ASR need is ~3.8 GB, but since it must coexist with the language
# model ~16 GB is required, so this is the safe gate). With the LM loaded (this machine)
# choose_plan does NOT pick the CPU branch -> GPU + swap; on a RAM-rich machine a short
# recording can still use the CPU co-resident path.
LITE_AUDIO_RAM_MIB = DEFAULT_AUDIO_RAM_MIB


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
        # LITE ASR policy: the CPU co-resident path is chosen ONLY when the recording is
        # SHORTER than `cpu_max_seconds` AND system RAM suffices (the language model stays in
        # VRAM, the audio model runs on the CPU/system RAM; NO swap). For a long recording or
        # insufficient RAM it is GPU + mandatory swap (the swap costs ~9-14 s but GPU
        # transcription is much faster, so the total time is shorter; see choose_plan).
        # The threshold is LITE_CPU_MAX_SECONDS (15 s); the CPU branch requires
        # LITE_AUDIO_RAM_MIB (5120 MiB; measurement-2: co-resident DOES NOT FIT alongside the
        # LM -> in practice GPU + swap on this machine).
        cpu_max_seconds=LITE_CPU_MAX_SECONDS,
        audio_ram_mib=LITE_AUDIO_RAM_MIB,
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
                local_model_loaded: bool = True, local_model_required: bool = True,
                cpu_max_seconds: float | None = None) -> tuple[str, bool]:
    """Returns the (device, need_swap) plan for the audio pipeline, in PRIORITY order.

    The tier (`profile`) comes from the SELECTED profile name (lite/pro); the device/swap
    decision is made from the tier's memory thresholds + REAL free VRAM + system RAM and the
    recording DURATION:

      1) Whisper + AST fit in free VRAM ALONGSIDE the local language model
         (`audio_vram_mib`) -> ('cuda', False): GPU transcription, NO swap (fastest, free).
      2) They do not fit and the CPU co-resident condition holds -> ('cpu', False): the
         language model STAYS in VRAM and the audio model runs on the CPU/system RAM (NO swap;
         slower). Condition: the recording is SHORTER than the threshold **AND** the pipeline
         fits in system RAM (`audio_ram_mib`; LITE and PRO 5120 MiB). On a short
         recording the swap cost outweighs
         the gain. Threshold: `cpu_max_seconds` / `profile.cpu_max_seconds` /
         `DEFAULT_CPU_MAX_SECONDS` (LITE 15 s, PRO 20 s).
      3) Otherwise (LONG recording or insufficient RAM) -> ('cuda', True): GPU transcription.
         The language model is unloaded and restored (swap) ONLY if required. On a long
         recording the GPU gain outweighs the swap cost; if RAM is insufficient the swap is
         unavoidable anyway.

    When the duration is unknown (duration_seconds=None) no swap is FORCED; only the memory
    rule applies (CPU when RAM suffices, otherwise a mandatory swap). This avoids an
    unnecessary language-model unload/reload for a single recording.

    There are TWO separate flags and they must NOT be confused:
      - `local_model_required`: must the local language model be in VRAM at the END of this
        turn? True on the local provider; False on an API provider (GPU free, no swap).
      - `local_model_loaded`: is the local language model ACTUALLY in VRAM right now? The caller
        MEASURES this with a health probe (`/health`). The name does NOT mean "is the provider
        local".
    When the local model is REQUIRED but NOT currently loaded (e.g. an audio turn cancelled the
    model load, `Stop-ModelSwitchForAudio`) free VRAM looks large; still, saying "there is room"
    is WRONG because the ASR models (~3.9 GB) take the language model's place and, if the model
    is not restored after the turn, the next load is rejected with "not enough free VRAM". That
    case therefore returns `('cuda', True)`: ASR runs on the GPU, then after the transcription the
    audio models are dropped, the worker is stopped and the language model is restored.
    The CPU path is never a FORCED choice: if the chosen device fails the
    worker layer falls back to the CPU; if there is no GPU at all (no CUDA/nvidia-smi) the CPU
    remains as a last resort. When `vram_free_mib`/`ram_free_mib` are omitted they are read
    from the machine (if unreadable -> 0, i.e. assumed not to fit).
    """
    if not local_model_required:
        # API provider: the local language model is neither present nor needed -> GPU free, no swap.
        return 'cuda', False
    if not local_model_loaded:
        # The local model is REQUIRED this turn but is NOT currently in VRAM (e.g. the load was
        # cancelled for an audio turn). Free VRAM looks large, but saying "there is room" to the
        # ASR would be wrong: the models hold ~3.9 GB and, if the model is not restored after the
        # turn, the next load is rejected. Swap is MANDATORY -> drop + stop + restore the model.
        return 'cuda', True
    if vram_free_mib is None:
        vram_free_mib = free_vram_mib()
    if ram_free_mib is None:
        ram_free_mib = available_ram_mib()
    if vram_free_mib >= profile.audio_vram_mib:
        return 'cuda', False
    ram_ok = ram_free_mib >= profile.audio_ram_mib
    threshold = cpu_max_seconds
    if threshold is None:
        threshold = profile.cpu_max_seconds
    if threshold is None:
        threshold = DEFAULT_CPU_MAX_SECONDS
    # CPU co-resident ONLY when the recording is SHORTER than the threshold AND RAM suffices.
    # If the duration is unknown no swap is forced (memory rule): no unnecessary language-model
    # swap for a single recording.
    if ram_ok and (duration_seconds is None or duration_seconds <= threshold):
        return 'cpu', False
    return 'cuda', True
