"""agent/mtp_inject.py: contracts of the Qwen3.5 MTP head injection."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import mtp_inject  # noqa: E402


def test_mtp_mapping_has_fifteen_tensors():
    """The Qwen3.5 MTP block has exactly 15 tensors (one full-attention block + nextn proj)."""
    assert len(mtp_inject.MAP) == 15
    gg = set(mtp_inject.MAP.values())
    assert "blk.{b}.nextn.eh_proj.weight" in gg
    assert "blk.{b}.nextn.enorm.weight" in gg
    assert "blk.{b}.nextn.hnorm.weight" in gg
    assert "blk.{b}.nextn.shared_head_norm.weight" in gg
    for name in gg:
        assert name.startswith("blk.{b}.")
        assert name.endswith(".weight")


def test_has_mtp_is_false_for_missing_or_garbage_file(tmp_path):
    assert mtp_inject.has_mtp(str(tmp_path / "missing.gguf")) is False
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"not a gguf" * 64)
    assert mtp_inject.has_mtp(str(garbage)) is False


def test_mtp_norm_weights_get_plus_one_offset():
    """GGUF stores Qwen norms as (w + 1); the HF source keeps raw w. 1-D tensors are F32 and get +1."""
    import struct
    raw = struct.pack("<3f", 0.5, -0.25, 2.0)
    assert mtp_inject._to_f32_list(raw, "F32") == [0.5, -0.25, 2.0]


def test_bf16_to_f16_matches_struct():
    """BF16 -> F16 conversion must match the pure-Python path."""
    import struct
    vals = [0.0, 1.0, -2.5, 0.3333, 123.75]
    for v in vals:
        bf16 = struct.pack("<f", v)[2:]  # top 16 bits = BF16
        out = mtp_inject._to_f16_bytes(bf16, "BF16")
        f16 = struct.unpack("<e", out)[0]
        assert abs(f16 - v) <= abs(v) * 0.01 + 1e-3
