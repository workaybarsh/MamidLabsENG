"""Injects the Qwen3.5 MTP (Multi-Token Prediction) head into a GGUF.

The abliterated Qwen3.5 9B GGUF in the catalog lacks the `nextn` layers because the
converter dropped the MTP head, so it does not work with llama.cpp's
`--spec-type draft-mtp`. This module pulls the head from the source HF safetensors
(only the MTP tensors, via HTTP range requests) and appends it to the existing GGUF.
The output is lossless: the main model weights are untouched; only the missing MTP
block (blk.<N>.nextn.*) and the `nextn_predict_layers` metadata are added.

Note: llama.cpp stores Qwen normalisation weights in GGUF as (w + 1); the HF source
keeps the raw w. So every 1-D (F32) tensor in the head gets +1 applied (the same as
the main model's norms).
"""
import json
import os
import array
import struct
import sys
import urllib.request

U8, I8, U16, I16, U32, I32, F32, BOOL, STRING, ARRAY, U64, I64, F64 = range(13)
SCALAR = {U8: '<B', I8: '<b', U16: '<H', I16: '<h', U32: '<I', I32: '<i',
          F32: '<f', BOOL: '<?', U64: '<Q', I64: '<q', F64: '<d'}
GGML_F32, GGML_F16 = 0, 1
ALIGN = 32

# HF tensor name -> llama.cpp GGUF nextn tensor name
MAP = {
    'mtp.layers.0.input_layernorm.weight':          'blk.{b}.attn_norm.weight',
    'mtp.layers.0.post_attention_layernorm.weight': 'blk.{b}.post_attention_norm.weight',
    'mtp.layers.0.self_attn.q_proj.weight':         'blk.{b}.attn_q.weight',
    'mtp.layers.0.self_attn.k_proj.weight':         'blk.{b}.attn_k.weight',
    'mtp.layers.0.self_attn.v_proj.weight':         'blk.{b}.attn_v.weight',
    'mtp.layers.0.self_attn.o_proj.weight':         'blk.{b}.attn_output.weight',
    'mtp.layers.0.self_attn.q_norm.weight':         'blk.{b}.attn_q_norm.weight',
    'mtp.layers.0.self_attn.k_norm.weight':         'blk.{b}.attn_k_norm.weight',
    'mtp.layers.0.mlp.gate_proj.weight':            'blk.{b}.ffn_gate.weight',
    'mtp.layers.0.mlp.down_proj.weight':            'blk.{b}.ffn_down.weight',
    'mtp.layers.0.mlp.up_proj.weight':              'blk.{b}.ffn_up.weight',
    'mtp.fc.weight':                                'blk.{b}.nextn.eh_proj.weight',
    'mtp.pre_fc_norm_embedding.weight':             'blk.{b}.nextn.enorm.weight',
    'mtp.pre_fc_norm_hidden.weight':                'blk.{b}.nextn.hnorm.weight',
    'mtp.norm.weight':                              'blk.{b}.nextn.shared_head_norm.weight',
}


class _R:
    def __init__(self, f):
        self.f = f

    def u32(self):
        return struct.unpack('<I', self.f.read(4))[0]

    def u64(self):
        return struct.unpack('<Q', self.f.read(8))[0]

    def s(self):
        n = self.u64()
        return self.f.read(n).decode('utf-8')

    def value(self, t):
        if t == STRING:
            return self.s()
        if t == ARRAY:
            et = self.u32()
            n = self.u64()
            return (et, [self.value(et) for _ in range(n)])
        return struct.unpack(SCALAR[t], self.f.read(struct.calcsize(SCALAR[t])))[0]


class _W:
    def __init__(self, f):
        self.f = f

    def u32(self, v):
        self.f.write(struct.pack('<I', v))

    def u64(self, v):
        self.f.write(struct.pack('<Q', v))

    def s(self, v):
        b = v.encode('utf-8')
        self.u64(len(b))
        self.f.write(b)

    def value(self, t, v):
        if t == STRING:
            self.s(v)
        elif t == ARRAY:
            et, lst = v
            self.u32(et)
            self.u64(len(lst))
            for x in lst:
                self.value(et, x)
        else:
            self.f.write(struct.pack(SCALAR[t], v))


def _align_up(x, a=ALIGN):
    return (x + a - 1) // a * a


def read_header(path):
    """Reads a GGUF header; returns (version, meta, tensors, data_start)."""
    with open(path, 'rb') as f:
        r = _R(f)
        if f.read(4) != b'GGUF':
            raise ValueError('not a GGUF: ' + path)
        version = r.u32()
        n_tensors = r.u64()
        n_kv = r.u64()
        meta = []
        for _ in range(n_kv):
            k = r.s()
            t = r.u32()
            meta.append([k, t, r.value(t)])
        tensors = []
        for _ in range(n_tensors):
            name = r.s()
            nd = r.u32()
            dims = [r.u64() for _ in range(nd)]
            ttype = r.u32()
            off = r.u64()
            tensors.append([name, dims, ttype, off])
        data_start = _align_up(f.tell())
    return version, meta, tensors, data_start


def has_mtp(path):
    """Does the model already carry a nextn/MTP block?"""
    try:
        _, meta, _, _ = read_header(path)
    except Exception:
        return False
    for k, _t, v in meta:
        if k.endswith('.nextn_predict_layers') and isinstance(v, int) and v > 0:
            return True
    return False


def _http_get(url, start=None, end=None, token=None):
    headers = {'User-Agent': 'MamidLabs/1.0'}
    if start is not None:
        headers['Range'] = f'bytes={start}-{end}'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    last = None
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except Exception as exc:  # retry a few times against network flakiness
            last = exc
    raise RuntimeError(f'download failed: {url} ({last})')


def _fetch_safetensors_header(base_url, shard, token):
    head = _http_get(base_url + shard, 0, 7, token)
    n = struct.unpack('<Q', head)[0]
    hdr = json.loads(_http_get(base_url + shard, 8, 8 + n - 1, token).decode('utf-8'))
    return hdr, 8 + n


def fetch_mtp_tensors(repo, revision, token=None):
    """Fetches the raw bytes/dtype/shape of the 15 MTP tensors from the source HF repo."""
    rev = revision or 'main'
    base = f'https://huggingface.co/{repo}/resolve/{rev}/'
    index = json.loads(_http_get(base + 'model.safetensors.index.json', token=token).decode('utf-8'))
    weight_map = index['weight_map']
    wanted = {n: shard for n, shard in weight_map.items() if n.startswith('mtp.')}
    if len(wanted) != len(MAP):
        raise RuntimeError(f'expected {len(MAP)} MTP tensors, got {len(wanted)}')
    out = {}
    for shard in sorted(set(wanted.values())):
        shard_url = base + shard
        hdr, data_start = _fetch_safetensors_header(base, shard, token)
        for name, sh in wanted.items():
            if sh != shard:
                continue
            info = hdr[name]
            s, e = info['data_offsets']
            raw = _http_get(shard_url, data_start + s, data_start + e - 1, token)
            if len(raw) != e - s:
                raise RuntimeError(f'{name}: short read')
            out[name] = {'dtype': info['dtype'], 'shape': info['shape'], 'raw': raw}
    return out


def _to_f32_list(raw, dtype):
    """Turns raw bytes into an F32 list (no numpy). Only for the small norm tensors."""
    if dtype == 'F32':
        return list(struct.unpack('<%df' % (len(raw) // 4), raw))
    if dtype == 'F16':
        return list(struct.unpack('<%de' % (len(raw) // 2), raw))
    if dtype == 'BF16':
        n = len(raw) // 2
        u = struct.unpack('<%dH' % n, raw)
        return [struct.unpack('<f', struct.pack('<I', x << 16))[0] for x in u]
    raise RuntimeError('unsupported dtype: ' + dtype)


_BF16_F16_LUT = None


def _bf16_f16_lut():
    """A 65536-entry table mapping a BF16 16-bit pattern to an F16 16-bit pattern."""
    global _BF16_F16_LUT
    if _BF16_F16_LUT is None:
        lut = array.array('H', bytes(2 * 65536))
        for x in range(65536):
            f = struct.unpack('<f', struct.pack('<I', x << 16))[0]
            try:
                lut[x] = struct.unpack('<H', struct.pack('<e', f))[0]
            except (OverflowError, struct.error):
                if f != f:
                    lut[x] = 0x7e00  # NaN
                elif f > 0:
                    lut[x] = 0x7c00  # +inf
                else:
                    lut[x] = 0xfc00  # -inf
        _BF16_F16_LUT = lut
    return _BF16_F16_LUT


def _to_f16_bytes(raw, dtype):
    """Turns raw bytes into GGML F16 bytes (efficient for the large matrix tensors)."""
    if dtype == 'F16':
        return raw
    if dtype == 'BF16':
        lut = _bf16_f16_lut()
        src = array.array('H')
        src.frombytes(raw)
        return array.array('H', (lut[x] for x in src)).tobytes()
    if dtype == 'F32':
        vals = struct.unpack('<%df' % (len(raw) // 4), raw)
        return struct.pack('<%de' % len(vals), *vals)
    raise RuntimeError('unsupported dtype: ' + dtype)


def inject_into_gguf(target, tensors, emit=print):
    """Appends the MTP block to an existing GGUF, updating it in place."""
    version, meta, base_tensors, data_start = read_header(target)
    arch = next((v for k, _t, v in meta if k == 'general.architecture'), 'qwen35')
    if arch != 'qwen35':
        raise RuntimeError(f'unexpected architecture: {arch}')
    block = next((v for k, _t, v in meta if k == 'qwen35.block_count'), None)
    if not isinstance(block, int):
        raise RuntimeError('qwen35.block_count not found')
    if any(k == 'qwen35.nextn_predict_layers' for k, _t, _v in meta):
        emit('MTP head already present, skipped.')
        return

    for m in meta:
        if m[0] == 'qwen35.block_count':
            m[2] = block + 1
    meta.append(['qwen35.nextn_predict_layers', U32, 1])

    old_size = os.path.getsize(target)
    old_data_size = old_size - data_start
    cursor = old_data_size
    new_tensors, blobs = [], []
    for hf_name, gg_tmpl in MAP.items():
        info = tensors[hf_name]
        shape = info['shape']
        dims = list(reversed(shape))  # safetensors [cols,rows] -> ggml ne order
        gtype = GGML_F32 if len(dims) == 1 else GGML_F16
        esize = 4 if gtype == GGML_F32 else 2
        nbytes = 1
        for d in dims:
            nbytes *= d
        nbytes *= esize
        cursor = _align_up(cursor)
        off = cursor
        cursor += nbytes
        new_tensors.append([gg_tmpl.format(b=block), dims, gtype, off])
        blobs.append((off, info, gtype, nbytes))

    tmp = target + '.mtp-tmp'
    emit('Writing the MTP head…')
    with open(target, 'rb') as fin, open(tmp, 'wb') as fout:
        w = _W(fout)
        fout.write(b'GGUF')
        w.u32(version)
        w.u64(len(base_tensors) + len(new_tensors))
        w.u64(len(meta))
        for k, t, v in meta:
            w.s(k)
            w.u32(t)
            w.value(t, v)
        for name, dims, ttype, off in (base_tensors + new_tensors):
            w.s(name)
            w.u32(len(dims))
            for d in dims:
                w.u64(d)
            w.u32(ttype)
            w.u64(off)
        pad = (ALIGN - fout.tell() % ALIGN) % ALIGN
        fout.write(b'\x00' * pad)
        new_data_start = fout.tell()
        # Copy the main data block untouched.
        fin.seek(data_start)
        remaining = old_data_size
        chunk = 16 * 1024 * 1024
        while remaining > 0:
            data = fin.read(min(chunk, remaining))
            if not data:
                break
            fout.write(data)
            remaining -= len(data)
        # Write the MTP tensors.
        for off, info, gtype, nbytes in blobs:
            want = new_data_start + off
            cur = fout.tell()
            if cur < want:
                fout.write(b'\x00' * (want - cur))
            if gtype == GGML_F32:
                vals = _to_f32_list(info['raw'], info['dtype'])
                data = struct.pack('<%df' % len(vals), *[v + 1.0 for v in vals])
            else:
                data = _to_f16_bytes(info['raw'], info['dtype'])
            if len(data) != nbytes:
                raise RuntimeError('tensor size mismatch')
            fout.write(data)
    os.replace(tmp, target)
    emit(f'MTP head added ({len(new_tensors)} tensors).')


def ensure_mtp(target, cfg, token=None, emit=print):
    """Adds the MTP head to the target GGUF if needed. Idempotent."""
    if not os.path.exists(target):
        raise FileNotFoundError(target)
    if has_mtp(target):
        emit('MTP head already present.')
        return False
    emit('MTP head missing; fetching from the source…')
    tensors = fetch_mtp_tensors(cfg['repo'], cfg.get('revision'), token)
    inject_into_gguf(target, tensors, emit)
    return True


if __name__ == '__main__':
    target = sys.argv[1]
    repo = sys.argv[2] if len(sys.argv) > 2 else 'huihui-ai/Huihui-Qwen3.5-9B-abliterated'
    rev = sys.argv[3] if len(sys.argv) > 3 else None
    ensure_mtp(target, {'repo': repo, 'revision': rev})
