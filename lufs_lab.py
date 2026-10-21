#!/usr/bin/env python3
"""
LUFS Lab
========
Offline LUFS loudness leveling for music and vocals.

* Measures loudness exactly per ITU-R BS.1770-4 / EBU R128 (gated, integrated LUFS).
* Music mode : one transparent gain change to hit the target, then a lookahead
               true-peak limiter (only acts if the gain pushes peaks past the ceiling).
               The gain is re-tuned until the *measured result* lands on the target.
* Voice mode : an adaptive leveler first rides the volume of quiet and loud
               passages toward a common level (slow, gated so breaths and silence
               are not boosted), then the same target + true-peak stage as above.
* Works on files of any length: audio is streamed in 30-second chunks.

Run with no arguments for the window, or from a terminal:
    python lufs_lab.py song1.wav song2.flac --target -14
    python lufs_lab.py ~/Music/Album --mode voice --strength medium
"""
from __future__ import annotations

import argparse
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import numpy as np
import soundfile as sf
from scipy import signal
from scipy.ndimage import minimum_filter1d, uniform_filter1d

APP_NAME = "LUFS Lab"
SUPPORTED_EXT = (".wav", ".flac", ".mp3", ".ogg", ".oga", ".aif", ".aiff")

OUT_FORMATS = {
    "WAV 24-bit": ("WAV", "PCM_24", ".wav"),
    "WAV 16-bit": ("WAV", "PCM_16", ".wav"),
    "FLAC 24-bit": ("FLAC", "PCM_24", ".flac"),
    "FLAC 16-bit": ("FLAC", "PCM_16", ".flac"),
    "MP3 128 kbps": ("MP3", "128", ".mp3"),       # for MP3 the second field is the bitrate
    "MP3 192 kbps": ("MP3", "192", ".mp3"),
    "MP3 320 kbps": ("MP3", "320", ".mp3"),
}
MP3_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)   # sample rates LAME can encode
MAX_ATTEMPTS = 3          # MP3: re-encode up to this many times to land on target after lossy encoding

# name -> (strength 0..1, max boost dB, max cut dB)
STRENGTHS = {
    "Gentle": (0.50, 6.0, 9.0),
    "Medium": (0.75, 9.0, 12.0),
    "Strong": (1.00, 12.0, 15.0),
}

OVERSAMPLE = 4            # true-peak detection oversampling
TP_MARGIN_DB = 0.1        # extra safety below the chosen ceiling (peak estimation is never perfect)
_TP_FIR = signal.firwin(385, 0.25, window=("kaiser", 7.0))   # sharp interpolation filter for peak detection
ENV_PAD = 512             # context samples around a chunk when computing peaks
TOLERANCE_LU = 0.05       # stop iterating when result is this close to target
MAX_PASSES = 6
SUB_SECONDS = 0.1         # BS.1770 400 ms blocks are built from 100 ms sub-blocks


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
#  BS.1770 loudness measurement
# --------------------------------------------------------------------------- #
def k_weighting_sos(sr: int) -> np.ndarray:
    """K-weighting (pre-filter shelf + RLB high-pass) for any sample rate.

    Uses the same bilinear-transform design as the reference libebur128 / ffmpeg
    implementations so readings match the standard meters.
    """
    # Stage 1: high shelf
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = math.tan(math.pi * f0 / sr)
    Vh = 10.0 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    s1 = [(Vh + Vb * K / Q + K * K) / a0, 2.0 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0,
          1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q + K * K) / a0]
    # Stage 2: RLB high-pass
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = math.tan(math.pi * f0 / sr)
    a0 = 1.0 + K / Q + K * K
    s2 = [1.0, -2.0, 1.0, 1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q + K * K) / a0]
    return np.array([s1, s2])


class KMeter:
    """Streaming K-weighted meter; collects per-channel 100 ms energy sums."""

    def __init__(self, sr: int, channels: int):
        self.sr, self.ch = sr, channels
        self.hop = int(round(SUB_SECONDS * sr))
        self.sos = k_weighting_sos(sr)
        self.zi = np.zeros((self.sos.shape[0], 2, channels))
        self.left = np.zeros((0, channels))
        self.subs: list[np.ndarray] = []

    def feed(self, x: np.ndarray) -> None:
        if len(x) == 0:
            return
        y, self.zi = signal.sosfilt(self.sos, x, axis=0, zi=self.zi)
        p = y * y
        if len(self.left):
            p = np.concatenate([self.left, p])
        m = (len(p) // self.hop) * self.hop
        if m:
            self.subs.append(p[:m].reshape(-1, self.hop, self.ch).sum(axis=1))
        self.left = p[m:]

    def subblocks(self) -> np.ndarray:
        return np.concatenate(self.subs) if self.subs else np.zeros((0, self.ch))


def block_powers(sub: np.ndarray, hop: int) -> np.ndarray:
    """400 ms blocks, 75 % overlap, channel-summed power (all channel weights = 1)."""
    k = sub.shape[0]
    if k < 4:
        raise ValueError("Audio is shorter than 0.4 seconds - too short to measure.")
    c = np.cumsum(np.vstack([np.zeros((1, sub.shape[1])), sub]), axis=0)
    blocks = (c[4:] - c[:-4]) / (4.0 * hop)
    return blocks.sum(axis=1)


def integrated_lufs(pw: np.ndarray) -> float:
    l = -0.691 + 10 * np.log10(pw + 1e-20)
    keep = l > -70.0
    if not keep.any():
        return -math.inf
    rel = -0.691 + 10 * math.log10(pw[keep].mean()) - 10.0
    keep2 = keep & (l > rel)
    if not keep2.any():
        return -math.inf
    return float(-0.691 + 10 * math.log10(pw[keep2].mean()))


# --------------------------------------------------------------------------- #
#  Adaptive leveler (voice mode)
# --------------------------------------------------------------------------- #
def leveler_curve_db(sub: np.ndarray, hop: int, strength: float,
                     max_boost: float, max_cut: float) -> np.ndarray:
    """Slow gain-riding curve in dB, one value per 100 ms sub-block."""
    k = sub.shape[0]
    p_sub = sub.sum(axis=1) / hop
    l_int = integrated_lufs(block_powers(sub, hop))
    p_st = uniform_filter1d(p_sub, size=10, mode="nearest")          # ~1 s window
    l_st = -0.691 + 10 * np.log10(p_st + 1e-12)
    d = np.clip(l_int - l_st, -max_cut, max_boost) * strength
    valid = l_st > (l_int - 12.0)          # don't chase silence, breaths, noise floor
    if not valid.any():
        return np.zeros(k)
    d = np.interp(np.arange(k), np.flatnonzero(valid), d[valid])      # hold across gaps
    for _ in range(2):                                                # ~1.2 s triangular smoothing
        d = uniform_filter1d(d, size=6, mode="nearest")
    return d


# --------------------------------------------------------------------------- #
#  True-peak limiter
# --------------------------------------------------------------------------- #
def true_peak_env(y: np.ndarray) -> np.ndarray:
    """Per-sample true-peak estimate (4x oversampled), max over channels."""
    ov = signal.resample_poly(y, OVERSAMPLE, 1, axis=0, window=_TP_FIR.copy())
    env = np.abs(ov).max(axis=1).reshape(-1, OVERSAMPLE).max(axis=1)
    return np.maximum(env, np.abs(y).max(axis=1))


def _odd(n: int) -> int:
    n = max(3, int(n))
    return n if n % 2 else n + 1


def iter_with_context(src: str, n: int, chunk: int, pad: int):
    """Yield (audio_with_context, ctx_start, core_start, core_end) sequentially."""
    with sf.SoundFile(src) as f:
        buf = np.zeros((0, f.channels))
        buf_start = 0
        pos = 0
        s = 0
        while s < n:
            e = min(n, s + chunk)
            want = min(n, e + pad)
            while pos < want:
                blk = f.read(chunk, dtype="float64", always_2d=True)
                if len(blk) == 0:
                    break
                buf = np.concatenate([buf, blk]) if len(buf) else blk
                pos += len(blk)
            lo = max(0, s - pad)
            if lo > buf_start:
                buf = buf[lo - buf_start:]
                buf_start = lo
            hi = min(pos, e + pad)
            e_eff = min(e, pos)
            if e_eff <= s:
                break
            yield buf[: hi - buf_start], buf_start, s, e_eff
            s = e


def analyze(src, progress, cancelled):
    """Pass 1: read the whole file once and collect 100 ms energy sub-blocks."""
    with sf.SoundFile(src) as f:
        sr, ch, total = f.samplerate, f.channels, f.frames
        meter = KMeter(sr, ch)
        n = 0
        step = int(round(SUB_SECONDS * sr)) * 300       # 30 s
        while True:
            if cancelled():
                raise Cancelled()
            blk = f.read(step, dtype="float64", always_2d=True)
            if len(blk) == 0:
                break
            meter.feed(blk)
            n += len(blk)
            if total > 0:
                progress(min(1.0, n / total))
    return sr, ch, n, meter.subblocks()


def compute_peak_env(src, n, sr, env_path, progress, cancelled):
    """Pass 2: per-sample true-peak envelope of the *input*, cached in a temp file.

    The (slow) oversampling is done once here. Gain changes are smooth and scalar,
    so the envelope of the processed signal is just this envelope times the gain.
    """
    hop = int(round(SUB_SECONDS * sr))
    with open(env_path, "wb") as fh:
        for xc, lo, s, e in iter_with_context(src, n, hop * 300, ENV_PAD):
            if cancelled():
                raise Cancelled()
            env = true_peak_env(xc)[s - lo:e - lo].astype(np.float32)
            fh.write(env.tobytes())
            progress(e / n)


class Mp3Writer:
    """Writes float audio of shape (n, channels) as a constant-bitrate MP3 using LAME."""

    def __init__(self, path, sr, ch, kbps):
        import lameenc
        self.enc = lameenc.Encoder()
        self.enc.set_bit_rate(kbps)
        self.enc.set_in_sample_rate(sr)
        self.enc.set_out_sample_rate(sr)
        self.enc.set_channels(ch)
        self.enc.set_quality(2)                      # LAME "high quality" (0 best/slowest ... 9 worst)
        self.fh = open(path, "wb")

    def write(self, x):
        pcm = np.round(np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
        self.fh.write(self.enc.encode(pcm.tobytes()))

    def close(self):
        try:
            self.fh.write(self.enc.flush())
        finally:
            self.fh.close()


def open_writer(path, sr, ch, fmt):
    if fmt[0] == "MP3":
        return Mp3Writer(path, sr, ch, int(fmt[1]))
    return sf.SoundFile(path, "w", samplerate=sr, channels=ch, format=fmt[0], subtype=fmt[1])


def run_pass(src, dst_part, env_path, sr, ch, n, centers, curve_db, gain_db, ceiling_db,
             fmt, progress, cancelled):
    """Apply leveler curve + global gain + true-peak limiter; measure the result."""
    hop = int(round(SUB_SECONDS * sr))
    W = _odd(0.020 * sr)                 # limiter hold window (+/-10 ms)
    B = max(1, int(0.008 * sr))          # two 8 ms boxes -> 16 ms triangular smoothing
    pad = W + 2 * B + 128
    chunk = hop * 300
    ceil_lin = 10 ** ((ceiling_db - TP_MARGIN_DB) / 20.0)
    meter = KMeter(sr, ch)
    min_gs = 1.0
    rng = np.random.default_rng(12345)
    dither = fmt[1] == "PCM_16" or fmt[0] == "MP3"
    out = None
    if dst_part:
        out = open_writer(dst_part, sr, ch, fmt)
    try:
        for xc, lo, s, e in iter_with_context(src, n, chunk, pad):
            if cancelled():
                raise Cancelled()
            idx = np.arange(lo, lo + len(xc), dtype=np.float64)
            g = 10.0 ** ((np.interp(idx, centers, curve_db) + gain_db) / 20.0)
            y = xc * g[:, None]
            env = np.fromfile(env_path, dtype=np.float32, count=len(xc), offset=lo * 4).astype(np.float64) * g
            need = np.minimum(1.0, ceil_lin / np.maximum(env, 1e-9))
            gmin = minimum_filter1d(need, size=W, mode="nearest")
            gs = uniform_filter1d(uniform_filter1d(gmin, B, mode="nearest"), B, mode="nearest")
            core = slice(s - lo, e - lo)
            o = y[core] * gs[core, None]
            min_gs = min(min_gs, float(gs[core].min()))
            meter.feed(o)
            if out is not None:
                if dither:
                    o = o + (rng.random(o.shape) - rng.random(o.shape)) / 32768.0
                out.write(np.clip(o, -1.0, 1.0))
            progress(e / n)
    finally:
        if out is not None:
            out.close()
    lufs = integrated_lufs(block_powers(meter.subblocks(), hop))
    return lufs, 20 * math.log10(min_gs)


def measure_file(path, progress=lambda f: None, cancelled=lambda: False):
    """Independent re-measurement of a finished file: (LUFS, true peak dBTP)."""
    with sf.SoundFile(path) as f:
        sr, ch, n = f.samplerate, f.channels, f.frames
    hop = int(round(SUB_SECONDS * sr))
    meter = KMeter(sr, ch)
    tp = 0.0
    for xc, lo, s, e in iter_with_context(path, n, hop * 300, ENV_PAD):
        if cancelled():
            raise Cancelled()
        core = slice(s - lo, e - lo)
        tp = max(tp, float(true_peak_env(xc)[core].max()))
        meter.feed(xc[core])
        progress(e / n)
    lufs = integrated_lufs(block_powers(meter.subblocks(), hop))
    return lufs, (20 * math.log10(tp) if tp > 0 else -math.inf)


# --------------------------------------------------------------------------- #
#  One file, start to finish
# --------------------------------------------------------------------------- #
def _resample_for_mp3(src, sr0):
    """LAME only handles sample rates up to 48 kHz: make a 48/44.1 kHz temporary copy.

    Streams in ~20 s pieces (with overlap context at each edge) so long files do not fill memory.
    """
    out_sr = 48000 if sr0 % 48000 == 0 else 44100 if sr0 % 44100 == 0 else 48000
    g = math.gcd(sr0, out_sr)
    up, down = out_sr // g, sr0 // g
    chunk = down * max(1, (sr0 * 20) // down)            # multiples of `down` keep the pieces aligned
    pad = down * max(1, -(-2048 // down))                 # context samples each side (also a multiple)
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with sf.SoundFile(src) as f, sf.SoundFile(path, "w", samplerate=out_sr, channels=f.channels,
                                                  format="WAV", subtype="FLOAT") as o:
            buf = np.zeros((0, f.channels), dtype="float32")
            left = buf
            eof = False
            while True:
                while len(buf) < chunk + pad and not eof:
                    blk = f.read(chunk, dtype="float32", always_2d=True)
                    if len(blk) == 0:
                        eof = True
                    else:
                        buf = np.concatenate([buf, blk])
                if len(buf) == 0:
                    break
                n_emit = len(buf) if eof else chunk
                core, right = buf[:n_emit], buf[n_emit:n_emit + pad]
                y = signal.resample_poly(np.concatenate([left, core, right]), up, down, axis=0)
                start = len(left) * up // down
                o.write(y[start:start + (n_emit * up + down - 1) // down].astype("float32"))
                left = np.concatenate([left, core])[-pad:]
                buf = buf[n_emit:]
                if eof:
                    break
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def level_file(src, dst, target=-14.0, ceiling=-1.0, mode="music", strength="Medium",
               out_format="WAV 24-bit", progress=lambda f: None, log=print,
               cancelled=lambda: False):
    """Level one file. (Prepares MP3 output limits, then does the work.)"""
    tmp = None
    try:
        work = src
        if OUT_FORMATS[out_format][0] == "MP3":
            with sf.SoundFile(src) as f0:
                sr0, ch0 = f0.samplerate, f0.channels
            if ch0 > 2:
                raise ValueError("MP3 only supports mono or stereo files - choose WAV or FLAC for this one.")
            if sr0 not in MP3_RATES:
                log(f"  MP3 cannot use {sr0} Hz - converting to a lower sample rate first")
                tmp = work = _resample_for_mp3(src, sr0)
        r = _level_file(work, dst, target, ceiling, mode, strength, out_format, progress, log, cancelled)
        r["src"] = src
        return r
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _level_file(src, dst, target, ceiling, mode, strength, out_format, progress, log, cancelled):
    t0 = time.time()
    fmt = OUT_FORMATS[out_format]
    lossy = fmt[0] == "MP3"
    root_, ext_ = os.path.splitext(dst)
    part = root_ + ".part" + ext_
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

    def stage(lo, hi):
        return lambda fr: progress(lo + (hi - lo) * min(1.0, max(0.0, fr)))

    sr, ch, n, sub = analyze(src, stage(0.0, 0.15), cancelled)
    hop = int(round(SUB_SECONDS * sr))
    l_in = integrated_lufs(block_powers(sub, hop))
    if not math.isfinite(l_in):
        raise ValueError("The audio is silent or too quiet to measure.")
    log(f"  Input loudness: {l_in:.1f} LUFS")

    k = sub.shape[0]
    centers = (np.arange(k) + 0.5) * hop
    if mode == "voice":
        s_val, boost, cut = STRENGTHS[strength]
        curve = leveler_curve_db(sub, hop, s_val, boost, cut)
        log(f"  Voice leveler ({strength}): gain rides between {curve.min():+.1f} and {curve.max():+.1f} dB")
    else:
        curve = np.zeros(k)

    predicted = integrated_lufs(block_powers(sub * (10.0 ** (curve / 10.0))[:, None], hop))
    gain = target - predicted

    env_fd, env_path = tempfile.mkstemp(suffix=".env")
    os.close(env_fd)
    l_out = None
    min_gr = 0.0
    eff_ceiling = ceiling          # MP3: lowered if the encoded file overshoots the ceiling
    pre_target = target            # MP3: nudged if encoding shifts the loudness
    n_pass = 0
    try:
        compute_peak_env(src, n, sr, env_path, stage(0.15, 0.40), cancelled)
        for attempt in range(1, (MAX_ATTEMPTS if lossy else 1) + 1):
            hist = []
            for it in range(1, MAX_PASSES + 1):
                n_pass += 1
                lo_p = min(0.92, 0.40 + 0.55 * (n_pass - 1) / 3.0)
                hi_p = min(0.95, 0.40 + 0.55 * n_pass / 3.0)
                l_out, min_gr = run_pass(src, part, env_path, sr, ch, n, centers, curve, gain, eff_ceiling,
                                         fmt, stage(lo_p, hi_p), cancelled)
                hist.append((gain, l_out))
                err = pre_target - l_out
                if abs(err) <= TOLERANCE_LU or it == MAX_PASSES:
                    break
                slope = 1.0                      # how much loudness changes per dB of gain (<1 when limiting)
                if len(hist) >= 2 and abs(hist[-1][0] - hist[-2][0]) > 1e-6:
                    slope = (hist[-1][1] - hist[-2][1]) / (hist[-1][0] - hist[-2][0])
                    slope = min(1.0, max(0.05, slope))
                if err > 0 and slope <= 0.15:
                    break                        # limiter is saturated: target is out of reach at this ceiling
                step = max(-12.0, min(6.0, err / slope))
                log(f"  Pass {it}: {l_out:.2f} LUFS - limiter took some loudness, adjusting {step:+.2f} dB")
                gain += step
            if not lossy:
                break
            # MP3 is lossy: check what the *encoded* file really measures, and correct if needed
            lufs_d, tp_d = measure_file(part, lambda fr: None, cancelled)
            d_loud, over = target - lufs_d, tp_d - ceiling
            if attempt == MAX_ATTEMPTS or (abs(d_loud) <= 0.08 and over <= 0.05):
                break
            if over > 0.05:
                eff_ceiling -= over + 0.05
            if abs(d_loud) > 0.08:
                pre_target += d_loud
                gain += d_loud
            log(f"  After MP3 encoding: {lufs_d:.2f} LUFS, {tp_d:.2f} dBTP - adjusting and re-encoding")
        os.replace(part, dst)
    except BaseException:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        raise
    finally:
        try:
            os.remove(env_path)
        except OSError:
            pass

    lufs, tp = measure_file(dst, stage(0.95, 1.0), cancelled)
    warn = []
    if abs(lufs - target) > 0.15:
        warn.append(f"Could not reach {target:.1f} LUFS without exceeding the {ceiling:.1f} dBTP ceiling "
                    f"(result {lufs:.1f} LUFS).")
    if tp > ceiling + 0.1:
        warn.append(f"True peak {tp:.2f} dBTP is slightly above the {ceiling:.1f} ceiling.")
    return {
        "src": src, "dst": dst, "input_lufs": l_in, "output_lufs": lufs, "true_peak": tp,
        "max_gain_reduction": min_gr, "seconds": time.time() - t0, "warnings": warn,
    }


# --------------------------------------------------------------------------- #
#  File helpers
# --------------------------------------------------------------------------- #
def collect_audio_files(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                full = os.path.join(p, name)
                if os.path.isfile(full) and name.lower().endswith(SUPPORTED_EXT):
                    out.append(full)
        elif os.path.isfile(p):
            out.append(p)
    return out


LEVELED_DIRNAME = "leveled"


def _norm_path(p):
    """Comparable form of a path: Windows and macOS file names are case-insensitive, Linux's are not."""
    q = os.path.normcase(os.path.abspath(p))
    return q.lower() if sys.platform == "darwin" else q


def make_output_path(src, ext, used, where="leveled", outdir=None):
    """Where a finished file goes.

    where = "leveled": <source folder>/leveled/<same name>
            "beside" : <source folder>/<name>_leveled
            "folder" : <outdir>/<name>_leveled
    """
    src_dir = os.path.dirname(os.path.abspath(src))
    stem = os.path.splitext(os.path.basename(src))[0]
    if where == "leveled":
        folder, base = os.path.join(src_dir, LEVELED_DIRNAME), stem
    elif where == "folder":
        folder, base = outdir, stem + "_leveled"
    else:
        folder, base = src_dir, stem + "_leveled"
    cand = os.path.join(folder, base + ext)
    k = 2
    while _norm_path(cand) in used or _norm_path(cand) == _norm_path(src):
        cand = os.path.join(folder, f"{base}_{k}{ext}")
        k += 1
    used.add(_norm_path(cand))
    return cand


def split_dnd_paths(data):
    """Split the text a drag-and-drop gives us: paths with spaces arrive in {braces}."""
    import re
    return [a or b for a, b in re.findall(r"\{([^}]*)\}|(\S+)", data)]


def batch_average_lufs(files, log=print, progress=lambda f: None, cancelled=lambda: False):
    """Measure every file; return their average loudness (None if nothing measurable)."""
    vals = []
    for i, p in enumerate(files):
        if cancelled():
            raise Cancelled()
        try:
            _, _, _, sub = analyze(p, lambda f: None, cancelled)
            hop = int(round(SUB_SECONDS * _sr_of(p)))
            lufs = integrated_lufs(block_powers(sub, hop))
            if math.isfinite(lufs):
                vals.append(lufs)
                log(f"  {os.path.basename(p)}: {lufs:.1f} LUFS")
        except Cancelled:
            raise
        except Exception as exc:
            log(f"  {os.path.basename(p)}: could not measure ({exc})")
        progress((i + 1) / len(files))
    return float(np.mean(vals)) if vals else None


def _sr_of(path):
    with sf.SoundFile(path) as f:
        return f.samplerate


# --------------------------------------------------------------------------- #
#  Whole batch (shared by the window and the command line)
# --------------------------------------------------------------------------- #
KNOWN_ERRORS = (ValueError, sf.SoundFileError, PermissionError, FileNotFoundError, MemoryError, ImportError)


def friendly_reason(exc, src=None):
    """Plain-English explanation of why a file failed."""
    msg = str(exc).strip()
    low = msg.lower()
    if src and not os.path.exists(src):
        return "The file could not be found - it may have been moved, renamed or deleted."
    if isinstance(exc, ValueError):                      # our own messages are already readable
        return msg
    if isinstance(exc, PermissionError) or "permission denied" in low:
        return ("Windows would not let the program read this file or save the result - it may be open "
                "in another program, or the folder is protected.")
    if isinstance(exc, FileNotFoundError):
        return "The file could not be found - it may have been moved, renamed or deleted."
    if (isinstance(exc, OSError) and getattr(exc, "errno", None) == 28) or "no space" in low:
        return "The disk is full - free up some space and try again."
    if isinstance(exc, MemoryError):
        return "The computer ran out of memory while processing this file."
    if isinstance(exc, ImportError) and "lameenc" in low:
        return ("MP3 output needs the 'lameenc' package - run the launcher again (or: python -m pip install lameenc), "
                "or choose WAV or FLAC.")
    if isinstance(exc, sf.SoundFileError) or "format not recognised" in low or "error opening" in low:
        return ("The file could not be read. It may be damaged, or in a format this program cannot open "
                "(M4A/AAC, for example - convert it to WAV or FLAC first).")
    return f"Unexpected problem: {msg or type(exc).__name__}"


def failed_lines(res, limit=8):
    """The 'which files failed and why' block, or an empty string."""
    items = res["failed"]
    if not items:
        return ""
    lines = []
    for src, reason in items[:limit]:
        lines.append(f"\u2022 {os.path.basename(src)} - {reason}" if src else f"\u2022 {reason}")
    if len(items) > limit:
        lines.append(f"...and {len(items) - limit} more (see the log).")
    return "Could not be leveled:\n" + "\n".join(lines)


def run_batch(files, target=-14.0, ceiling=-1.0, mode="music", strength="Medium",
              out_format="WAV 24-bit", where="leveled", outdir=None, match=False,
              on_status=lambda s: None, on_file=lambda f: None, on_overall=lambda f: None,
              log=print, cancelled=lambda: False):
    """Level every file in `files`. Returns a dict describing what happened.

    match=True (2+ files): measure everything first, then bring every file to the
    batch's average loudness so they all sit at the same volume.
    """
    ext = OUT_FORMATS[out_format][2]
    res = {"results": [], "failed": [], "out_dirs": [], "cancelled": False,
           "match": False, "reference": None}
    try:
        if match and len(files) > 1:
            on_status("Measuring all files...")
            log("Measuring all files to find the batch average...")
            avg = batch_average_lufs(files, log=log, progress=on_file, cancelled=cancelled)
            if avg is None:
                log("None of the files could be measured.")
                res["failed"].append(("", "None of the files could be measured."))
                return res
            target = round(avg, 1)
            res["match"], res["reference"] = True, target
            log(f"Batch average: {target:.1f} LUFS - every file will be matched to it.")
        elif match:
            log("Only one file in the list, so there is nothing to match - using your target loudness.")

        used: set = set()
        total = len(files)
        for i, src in enumerate(files):
            if cancelled():
                raise Cancelled()
            name = os.path.basename(src)
            on_status(f"File {i + 1} of {total}: {name}")
            on_overall(i / total)
            on_file(0.0)
            log(f"[{i + 1}/{total}] {name}")
            try:
                dst = make_output_path(src, ext, used, where, outdir)
                r = level_file(src, dst, target, ceiling, mode, strength, out_format,
                               progress=on_file, log=log, cancelled=cancelled)
                res["results"].append(r)
                folder = os.path.dirname(os.path.abspath(dst))
                if folder not in res["out_dirs"]:
                    res["out_dirs"].append(folder)
                log(f"  Done: {r['output_lufs']:.1f} LUFS, {r['true_peak']:.2f} dBTP "
                    f"(limiter max {abs(r['max_gain_reduction']):.1f} dB)  ->  {os.path.basename(dst)}")
                for w in r["warnings"]:
                    log("  WARNING: " + w)
            except Cancelled:
                raise
            except Exception as exc:
                reason = friendly_reason(exc, src)
                res["failed"].append((src, reason))
                log(f"  FAILED: {reason}")
                if not isinstance(exc, KNOWN_ERRORS):
                    log(traceback.format_exc())
        on_overall(1.0)
    except Cancelled:
        res["cancelled"] = True
        log("Cancelled.")
    return res


def summarize(res):
    ok, bad = len(res["results"]), len(res["failed"])
    text = f"{ok} file{'s' if ok != 1 else ''} leveled" + (f", {bad} failed" if bad else "") + "."
    lv = [r["output_lufs"] for r in res["results"]]
    if res["match"] and ok > 1:
        text += (f"\nAll files are within {max(lv) - min(lv):.1f} LU of each other "
                 f"({min(lv):.1f} to {max(lv):.1f} LUFS).")
    elif ok == 1:
        r = res["results"][0]
        text += f"\nResult: {r['output_lufs']:.1f} LUFS, {r['true_peak']:.1f} dBTP."
    warned = sum(1 for r in res["results"] if r["warnings"])
    if warned:
        text += f"\n{warned} file{'s' if warned != 1 else ''} had a warning - see the log."
    block = failed_lines(res)
    if block:
        text += "\n\n" + block
    return text


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                       # noqa: Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Command line
# --------------------------------------------------------------------------- #
def cli(argv):
    ap = argparse.ArgumentParser(description="Level audio files to a target LUFS with a true-peak limiter.")
    ap.add_argument("inputs", nargs="+", help="audio files and/or folders")
    ap.add_argument("-t", "--target", type=float, default=-14.0, help="target LUFS (default -14)")
    ap.add_argument("--match", action="store_true",
                    help="match all files to each other at the batch's average loudness (ignores --target)")
    ap.add_argument("--ceiling", type=float, default=-1.0, help="true-peak ceiling in dBTP (default -1)")
    ap.add_argument("--mode", choices=["music", "voice"], default="music")
    ap.add_argument("--strength", choices=["gentle", "medium", "strong"], default="medium",
                    help="voice leveler strength (voice mode only)")
    ap.add_argument("--format", choices=list(OUT_FORMATS), default="WAV 24-bit")
    ap.add_argument("-o", "--outdir", default=None,
                    help="save into this folder (default: a \"leveled\" folder inside each source folder)")
    ap.add_argument("--beside", action="store_true",
                    help="save next to each original with _leveled added to the name")
    args = ap.parse_args(argv)

    files = collect_audio_files(args.inputs)
    if not files:
        print("No audio files found.")
        return 1
    where = "folder" if args.outdir else ("beside" if args.beside else "leveled")
    res = run_batch(files, args.target, args.ceiling, args.mode, args.strength.capitalize(),
                    args.format, where, args.outdir, args.match, log=print)
    print("\n" + summarize(res))
    if res["out_dirs"]:
        print("Saved in: " + "; ".join(res["out_dirs"]))
    return 1 if (res["failed"] or res["cancelled"]) else 0


# --------------------------------------------------------------------------- #
#  Window
# --------------------------------------------------------------------------- #
def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:                                    # crisp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:                                    # own taskbar identity so our icon shows (not Python's)
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LUFSLab.App")
    except Exception:
        pass

    dnd_ok, DND_FILES = False, None
    root = None
    try:                                    # drag-and-drop needs the optional tkinterdnd2 package
        from tkinterdnd2 import TkinterDnD, DND_FILES
        root = TkinterDnD.Tk()
        dnd_ok = True
    except Exception:
        root = tk.Tk()

    class App:
        def __init__(self, root):
            self.root = root
            root.title(APP_NAME)
            try:
                st = ttk.Style(root)
                if sys.platform.startswith("linux") and "clam" in st.theme_names():
                    st.theme_use("clam")
            except Exception:
                pass
            self._icons = []
            try:                            # window / taskbar icon (kept in self so it is not garbage-collected)
                from app_icon import ICON_PNGS_B64
                self._icons = [tk.PhotoImage(data=b) for b in ICON_PNGS_B64]
                root.iconphoto(True, *self._icons)
            except Exception:
                pass
            root.geometry("800x780")
            root.minsize(740, 700)
            self.files: list[str] = []
            self.q: queue.Queue = queue.Queue()
            self.cancel_flag = threading.Event()
            self.worker = None

            self.mode = tk.StringVar(value="music")
            self.target = tk.StringVar(value="-14")
            self.ceiling = tk.StringVar(value="-1.0")
            self.strength = tk.StringVar(value="Medium")
            self.fmt = tk.StringVar(value="WAV 24-bit")
            self.match = tk.BooleanVar(value=False)
            self.outmode = tk.StringVar(value="leveled")
            self.outdir = tk.StringVar(value="")

            pad = {"padx": 10, "pady": 5}

            # files
            lf = ttk.LabelFrame(root, text="1. Files (add one or many)")
            lf.pack(fill="both", expand=False, **pad)
            hint = ("Drag audio files or folders anywhere into this window, or use the buttons."
                    if dnd_ok else
                    "Use the buttons to add files. (Drag-and-drop needs the tkinterdnd2 package - run the launcher again to install it.)")
            ttk.Label(lf, text=hint, foreground="#555555").pack(anchor="w", padx=8, pady=(6, 0))
            inner = ttk.Frame(lf)
            inner.pack(fill="both", expand=True, padx=8, pady=8)
            self.listbox = tk.Listbox(inner, height=7, selectmode="extended", activestyle="none")
            sb = ttk.Scrollbar(inner, orient="vertical", command=self.listbox.yview)
            self.listbox.config(yscrollcommand=sb.set)
            self.listbox.pack(side="left", fill="both", expand=True)
            sb.pack(side="left", fill="y")
            btns = ttk.Frame(inner)
            btns.pack(side="left", fill="y", padx=(8, 0))
            for text, cmd in [("Add files...", self.add_files), ("Add folder...", self.add_folder),
                              ("Remove selected", self.remove_sel), ("Clear list", self.clear)]:
                ttk.Button(btns, text=text, command=cmd).pack(fill="x", pady=2)

            # settings
            sf_ = ttk.LabelFrame(root, text="2. Settings")
            sf_.pack(fill="x", **pad)
            r1 = ttk.Frame(sf_)
            r1.pack(fill="x", padx=8, pady=(8, 2))
            ttk.Radiobutton(r1, text="Music", variable=self.mode, value="music",
                            command=self._mode_changed).pack(side="left")
            ttk.Radiobutton(r1, text="Voice / vocals", variable=self.mode, value="voice",
                            command=self._mode_changed).pack(side="left", padx=12)
            ttk.Label(r1, text="Voice leveling:").pack(side="left", padx=(24, 4))
            self.strength_box = ttk.Combobox(r1, textvariable=self.strength, values=list(STRENGTHS),
                                             state="disabled", width=8)
            self.strength_box.pack(side="left")
            r2 = ttk.Frame(sf_)
            r2.pack(fill="x", padx=8, pady=(2, 2))
            ttk.Label(r2, text="Target loudness (LUFS):").pack(side="left")
            self.target_entry = ttk.Entry(r2, textvariable=self.target, width=6)
            self.target_entry.pack(side="left", padx=(4, 16))
            ttk.Label(r2, text="True-peak ceiling (dBTP):").pack(side="left")
            ttk.Entry(r2, textvariable=self.ceiling, width=6).pack(side="left", padx=(4, 16))
            ttk.Label(r2, text="Save as:").pack(side="left")
            ttk.Combobox(r2, textvariable=self.fmt, values=list(OUT_FORMATS), state="readonly",
                         width=14).pack(side="left", padx=4)
            r3 = ttk.Frame(sf_)
            r3.pack(fill="x", padx=8, pady=(2, 8))
            ttk.Checkbutton(r3, text="Match the volume of all files to each other (2 or more files)",
                            variable=self.match, command=self._match_changed).pack(anchor="w")
            ttk.Label(r3, text="Uses the batch's average loudness instead of the target above.",
                      foreground="#555555").pack(anchor="w", padx=(24, 0))

            # output
            of = ttk.LabelFrame(root, text="3. Where to save (The original files will remain untouched)")
            of.pack(fill="x", **pad)
            ttk.Radiobutton(of, text=f"In a new \"{LEVELED_DIRNAME}\" folder inside each file's own folder "
                                     "(keeps the original file names)",
                            variable=self.outmode, value="leveled").pack(anchor="w", padx=8, pady=(6, 0))
            ttk.Radiobutton(of, text="Next to each original file (adds \"_leveled\" to the name)",
                            variable=self.outmode, value="beside").pack(anchor="w", padx=8)
            row = ttk.Frame(of)
            row.pack(fill="x", padx=8, pady=(0, 8))
            ttk.Radiobutton(row, text="This folder:", variable=self.outmode, value="folder").pack(side="left")
            ttk.Entry(row, textvariable=self.outdir).pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(row, text="Browse...", command=self.pick_outdir).pack(side="left")

            # run
            run = ttk.Frame(root)
            run.pack(fill="x", **pad)
            self.start_btn = ttk.Button(run, text="Start", command=self.start)
            self.start_btn.pack(side="left")
            self.cancel_btn = ttk.Button(run, text="Cancel", command=self.cancel, state="disabled")
            self.cancel_btn.pack(side="left", padx=8)
            self.status = ttk.Label(run, text="Ready.")
            self.status.pack(side="left", padx=10)

            self.file_bar = ttk.Progressbar(root, maximum=1.0)
            self.file_bar.pack(fill="x", padx=10, pady=(0, 2))
            self.all_bar = ttk.Progressbar(root, maximum=1.0)
            self.all_bar.pack(fill="x", padx=10, pady=(0, 6))

            lg = ttk.Frame(root)
            lg.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            self.log = tk.Text(lg, height=9, state="disabled", wrap="word")
            lsb = ttk.Scrollbar(lg, orient="vertical", command=self.log.yview)
            self.log.config(yscrollcommand=lsb.set)
            self.log.pack(side="left", fill="both", expand=True)
            lsb.pack(side="left", fill="y")

            if dnd_ok:
                self._enable_dnd()
            if sys.platform == "darwin":            # bring the window to the front on macOS
                try:
                    root.lift()
                    root.attributes("-topmost", True)
                    root.after(400, lambda: root.attributes("-topmost", False))
                except Exception:
                    pass
            root.after(100, self._poll)

        # ---- drag and drop ----
        def _enable_dnd(self):
            def register(w):
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    pass
                for child in w.winfo_children():
                    register(child)
            register(self.root)

        def _on_drop(self, event):
            try:
                raw = list(self.root.tk.splitlist(event.data))
            except Exception:
                raw = split_dnd_paths(event.data)
            found = collect_audio_files(raw)
            good = [p for p in found if p.lower().endswith(SUPPORTED_EXT)]
            skipped = len(found) - len(good)
            if good:
                self._add(good)
            if skipped:
                self._log(f"Skipped {skipped} dropped file(s) that are not supported audio "
                          f"({', '.join(e.lstrip('.') for e in SUPPORTED_EXT[:5])}...).")
            if not good and not skipped:
                self._log("No supported audio files found in what you dropped "
                          "(folders are searched one level only, not their subfolders).")
            return event.action

        # ---- file list ----
        def _add(self, paths):
            have = {_norm_path(p) for p in self.files}
            for p in paths:
                if _norm_path(p) not in have:
                    self.files.append(p)
                    self.listbox.insert("end", f"{os.path.basename(p)}      \u2014  {os.path.dirname(p)}")
                    have.add(_norm_path(p))

        def add_files(self):
            pats = " ".join("*" + e for e in SUPPORTED_EXT)
            paths = filedialog.askopenfilenames(title="Choose audio files",
                                                filetypes=[("Audio files", pats), ("All files", "*.*")])
            self._add(paths)

        def add_folder(self):
            d = filedialog.askdirectory(title="Choose a folder of audio files")
            if d:
                found = collect_audio_files([d])
                if not found:
                    messagebox.showinfo(APP_NAME, "No supported audio files in that folder.")
                self._add(found)

        def remove_sel(self):
            for i in reversed(self.listbox.curselection()):
                self.listbox.delete(i)
                del self.files[i]

        def clear(self):
            self.listbox.delete(0, "end")
            self.files.clear()

        def pick_outdir(self):
            d = filedialog.askdirectory(title="Choose output folder")
            if d:
                self.outdir.set(d)
                self.outmode.set("folder")

        def _mode_changed(self):
            self.strength_box.config(state="readonly" if self.mode.get() == "voice" else "disabled")

        def _match_changed(self):
            self.target_entry.config(state="disabled" if self.match.get() else "normal")

        # ---- logging / progress ----
        def _log(self, text):
            self.log.config(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.config(state="disabled")

        def _poll(self):
            try:
                while True:
                    kind, val = self.q.get_nowait()
                    if kind == "log":
                        self._log(val)
                    elif kind == "status":
                        self.status.config(text=val)
                    elif kind == "file":
                        self.file_bar["value"] = val
                    elif kind == "overall":
                        self.all_bar["value"] = val
                    elif kind == "done":
                        self._finish(val)
            except queue.Empty:
                pass
            self.root.after(100, self._poll)

        def _finish(self, res):
            self.start_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")
            self.file_bar["value"] = 0
            if res["cancelled"]:
                self.status.config(text="Cancelled.")
                return
            text = summarize(res)
            self.status.config(text=text.splitlines()[0])
            self.root.after(50, lambda: self._popup(res, text))

        def _popup(self, res, text):
            if not res["results"]:
                messagebox.showerror(APP_NAME, text + "\n\nSee the log for details.")
                return
            icon = "warning" if (res["failed"] or any(r["warnings"] for r in res["results"])) else "info"
            dirs = res["out_dirs"]
            where = (f"Saved in:\n{dirs[0]}" if len(dirs) == 1
                     else f"Saved in {len(dirs)} folders, for example:\n{dirs[0]}")
            if messagebox.askyesno("Finished", f"{text}\n\n{where}\n\nOpen the folder now?", icon=icon):
                open_folder(dirs[0])

        # ---- run ----
        def start(self):
            if not self.files:
                messagebox.showwarning(APP_NAME, "Add at least one audio file first.")
                return
            match = bool(self.match.get())
            target = -14.0
            try:
                ceiling = float(self.ceiling.get())
                if not match:
                    target = float(self.target.get())
            except ValueError:
                messagebox.showerror(APP_NAME, "Target loudness and true-peak ceiling must be numbers.")
                return
            if not match and not -40 <= target <= -5:
                messagebox.showerror(APP_NAME, "Target loudness should be between -40 and -5 LUFS.")
                return
            if not -6 <= ceiling <= 0:
                messagebox.showerror(APP_NAME, "True-peak ceiling should be between -6 and 0 dBTP.")
                return
            where = self.outmode.get()
            outdir = None
            if where == "folder":
                outdir = self.outdir.get().strip()
                if not outdir:
                    messagebox.showerror(APP_NAME, "Choose an output folder, or pick one of the other save options.")
                    return
            settings = dict(target=target, ceiling=ceiling, mode=self.mode.get(), strength=self.strength.get(),
                            fmt=self.fmt.get(), where=where, outdir=outdir, match=match)
            self.cancel_flag.clear()
            self.start_btn.config(state="disabled")
            self.cancel_btn.config(state="normal")
            self.all_bar["value"] = 0
            self.file_bar["value"] = 0
            self.worker = threading.Thread(target=self._work, args=(list(self.files), settings), daemon=True)
            self.worker.start()

        def cancel(self):
            self.cancel_flag.set()
            self.status.config(text="Cancelling...")

        def _work(self, files, st):
            q = self.q
            try:
                res = run_batch(files, st["target"], st["ceiling"], st["mode"], st["strength"], st["fmt"],
                                st["where"], st["outdir"], st["match"],
                                on_status=lambda s: q.put(("status", s)),
                                on_file=lambda f: q.put(("file", f)),
                                on_overall=lambda f: q.put(("overall", f)),
                                log=lambda m: q.put(("log", m)),
                                cancelled=self.cancel_flag.is_set)
            except Exception as exc:
                q.put(("log", traceback.format_exc()))
                res = {"results": [], "failed": [("", str(exc))], "out_dirs": [], "cancelled": False,
                       "match": False, "reference": None}
            q.put(("done", res))

    App(root)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    gui()
