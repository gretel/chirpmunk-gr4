#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""
lora_wav.py -- LoRa chirp audio exporter.

Subscribes to a lora_trx UDP server. For each decoded frame, synthesises
a LoRa chirp audio clip (re-mapped to audible frequencies), saves it as
data/audio/<sha256_16hex>.wav, and optionally plays it via sounddevice.

Each payload produces a deterministic, recognisable audio signature:
  - 1 generic upchirp cue (or downchirp for CRC_FAIL) as a centred
    "attention" tone (same for all frames)
  - 3–6 payload-derived chirps with varied frequency range, sweep span,
    duration, and direction — all derived from SHA-256(payload)
  - Binaural spatialisation via ITD + ILD: each payload gets a fixed
    azimuth angle (−90° … +90°) from the hash, creating a convincing
    left/right headphone position. Interaural time delay (ITD) shifts
    samples by up to ±MAX_ITD_S; interaural level difference (ILD)
    attenuates the far ear by up to ILD_MAX_DB.

Output is 2-channel (stereo) 16-bit PCM WAV.  Works best on headphones.

Identical payloads always produce the same sound at the same spatial
position.  Playback is queued so concurrent frames play sequentially.

The WAV filename is derived from SHA-256(payload)[:16] so identical
payloads map to the same file and are not re-synthesised.  PHY metadata
is embedded in the WAV RIFF INFO chunk (ICMT / INAM).

Usage:
    lora_wav.py                           # live UDP, default 127.0.0.1:5555
    lora_wav.py --no-play                 # save WAV only, no speaker output
    lora_wav.py --audio-dir /tmp/lora

Dependencies: cbor2, sounddevice (optional -- disabled by --no-play)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import logging
import math
import queue
import struct
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any

import cbor2
from setproctitle import setproctitle

from lora.core.config import (
    load_config,
)
from lora.core.logging import (
    add_logging_args,
    setup_logging,
)
from lora.core.udp import (
    KEEPALIVE_INTERVAL,
    create_udp_subscriber,
    resolve_udp_address,
)

log = logging.getLogger("lora.tools.wav")

DEFAULT_AUDIO_DIR = "data/audio"
AUDIO_SAMPLE_RATE = 22050  # Hz — quarter CD rate, smooth phase at low frequency
CHIRP_AMPLITUDE = 0.38  # 0..1 — leave headroom for cosine fade

# Cue chirp (fixed, payload-independent — "attention, frame arrived")
CUE_F_LO = 200.0  # Hz
CUE_F_HI = 800.0  # Hz
CUE_DURATION_S = 0.091  # seconds (~70ms × 1.3)

# Payload signature chirps (range boundaries — hash bytes select within these)
SIG_F_LO_MIN = 80.0  # Hz — lowest base frequency
SIG_F_LO_MAX = 350.0  # Hz — highest base frequency
SIG_SPAN_MIN = 200.0  # Hz — minimum sweep span
SIG_SPAN_MAX = 750.0  # Hz — maximum sweep span
SIG_DUR_MIN = 0.072  # seconds — shortest chirp (~55ms × 1.3)
SIG_DUR_MAX = 0.156  # seconds — longest chirp (~120ms × 1.3)
SIG_N_MIN = 3  # fewest payload chirps
SIG_N_MAX = 6  # most payload chirps

# Playback buffering — pre-fill this many frames into the audio device before
# starting output. Larger values absorb CPU spikes (e.g. during TX bursts)
# at the cost of a small constant latency increase. 4096 ≈ 185 ms at 22050 Hz.
PLAYBACK_BLOCKSIZE = 4096  # samples

# Padding / envelope
SILENCE_PAD_S = 0.010  # 10 ms silence at start and end of each WAV
FADE_S = 0.005  # 5 ms cosine fade-in and fade-out on each chirp
INTER_CHIRP_S = 0.012  # 12 ms gap between cue and signature chirps

# Binaural spatialisation (ITD + ILD)
# ITD — interaural time delay: at 90° azimuth the far ear hears the signal
# ~650 µs later (head radius ~8.75 cm, speed of sound 343 m/s).
# ILD — interaural level difference: far ear is attenuated ~6 dB at 90°.
# Both scale with sin(azimuth), so centre (0°) is always symmetric.
# The cue chirp is always centred; only signature chirps are spatialised.
MAX_ITD_S = 0.00065  # seconds — max inter-ear delay at ±90° (~14 samples @22050 Hz)
ILD_MAX_DB = 6.0  # dB — max level difference at ±90°


# ---------------------------------------------------------------------------
# Chirp synthesis helpers
# ---------------------------------------------------------------------------


def _silence(n_samples: int) -> list[float]:
    return [0.0] * n_samples


def _cosine_fade(samples: list[float], fade_n: int) -> list[float]:
    """Apply cosine fade-in and fade-out to *samples*. Returns a new list."""
    s = list(samples)
    n = len(s)
    fade_n = min(fade_n, n // 2)
    for i in range(fade_n):
        t = i / fade_n
        w = 0.5 - 0.5 * math.cos(math.pi * t)  # 0 → 1 over fade_n samples
        s[i] *= w
        s[n - 1 - i] *= w
    return s


def _chirp(
    f_lo: float, f_hi: float, duration_s: float, *, upchirp: bool = True
) -> list[float]:
    """Synthesise one chirp.  Returns samples with cosine fade applied."""
    n = int(duration_s * AUDIO_SAMPLE_RATE)
    f_span = f_hi - f_lo
    fade_n = int(FADE_S * AUDIO_SAMPLE_RATE)
    samples: list[float] = []
    for k in range(n):
        frac = k / n
        t = k / AUDIO_SAMPLE_RATE
        if upchirp:
            phi = 2 * math.pi * (f_lo * t + 0.5 * f_span * t * frac)
        else:
            phi = 2 * math.pi * (f_hi * t - 0.5 * f_span * t * frac)
        samples.append(CHIRP_AMPLITUDE * math.sin(phi))
    return _cosine_fade(samples, fade_n)


def _lerp(lo: float, hi: float, t: float) -> float:
    """Linear interpolate: t ∈ [0, 1] → [lo, hi]."""
    return lo + (hi - lo) * t


def _apply_itd_ild(
    mono: list[float],
    azimuth_deg: float,
) -> tuple[list[float], list[float]]:
    """Apply interaural time delay (ITD) and level difference (ILD) to *mono*.

    Returns *(left, right)* stereo channels.

    ITD: the near ear receives the signal early; the far ear receives it
    delayed by MAX_ITD_S * |sin(az)|.  Both channels are shifted by ±half
    the total delay so the sum stays in sync with the original timeline.

    ILD: the far ear is attenuated by ILD_MAX_DB * |sin(az)| dB.  This
    models head-shadowing — effective mainly above ~1 kHz but adds a
    subtle level cue even for lower frequencies.

    azimuth_deg: −90 = hard left, 0 = centre, +90 = hard right.
    """
    az_rad = math.radians(azimuth_deg)
    sin_az = math.sin(az_rad)

    # ITD: total delay in samples, split symmetrically between ears
    total_delay = MAX_ITD_S * AUDIO_SAMPLE_RATE  # float samples
    half_delay = int(round(abs(total_delay * sin_az) / 2))

    n = len(mono)

    def _shift(samples: list[float], delay: int) -> list[float]:
        """Shift samples right by *delay* (positive = later)."""
        if delay <= 0:
            return list(samples)
        pad = [0.0] * delay
        return pad + samples[: n - delay]

    if sin_az >= 0:
        # Source to the right: right ear is near (early), left is far (late)
        left = _shift(mono, half_delay)  # delayed
        right = _shift(mono, 0)  # early — no shift needed after split
        # Remove the artificial asymmetry: just delay left by full amount
        # (simpler and perceptually equivalent for headphones)
        left = _shift(mono, 2 * half_delay)
        right = list(mono)
    else:
        # Source to the left: left ear is near, right is far
        left = list(mono)
        right = _shift(mono, 2 * half_delay)

    # ILD: attenuate far ear
    ild_linear = 10.0 ** (-ILD_MAX_DB * abs(sin_az) / 20.0)
    if sin_az >= 0:
        left = [s * ild_linear for s in left]  # far = left
    else:
        right = [s * ild_linear for s in right]  # far = right

    return left, right


# ---------------------------------------------------------------------------
# Signature synthesis
# ---------------------------------------------------------------------------


def _signature_params(payload: bytes) -> dict[str, Any]:
    """Derive chirp signature parameters from SHA-256(payload).

    Consumes 8 bytes of the hash deterministically:
      [0,1]   → base frequency (f_lo)
      [2,3]   → sweep span
      [4]     → symbol duration
      [5]     → number of payload chirps (3–6)
      [6]     → direction bitmask (bit i = upchirp for chirp i)
      [7]     → azimuth: 0x00 → −90°, 0x80 → 0° (centre), 0xFF → +90°
    """
    h = hashlib.sha256(payload).digest()

    f_lo = _lerp(SIG_F_LO_MIN, SIG_F_LO_MAX, int.from_bytes(h[0:2], "big") / 65535)
    span = _lerp(SIG_SPAN_MIN, SIG_SPAN_MAX, int.from_bytes(h[2:4], "big") / 65535)
    duration_s = _lerp(SIG_DUR_MIN, SIG_DUR_MAX, h[4] / 255)
    n_chirps = SIG_N_MIN + (h[5] % (SIG_N_MAX - SIG_N_MIN + 1))
    dir_mask = h[6]  # bit i: 1 = upchirp, 0 = downchirp
    azimuth_deg = _lerp(-90.0, 90.0, h[7] / 255)  # −90° … +90°

    return {
        "f_lo": f_lo,
        "f_hi": f_lo + span,
        "duration_s": duration_s,
        "n_chirps": n_chirps,
        "dir_mask": dir_mask,
        "azimuth_deg": azimuth_deg,
    }


def synthesise_frame(
    *, payload: bytes, crc_ok: bool
) -> tuple[list[float], list[float]]:
    """Synthesise stereo audio for a full LoRa frame.

    Returns *(left, right)* sample lists of equal length.

    Structure (mono before spatialisation):
      [silence pad]
      [cue chirp: 1 upchirp (CRC OK) or 1 downchirp (CRC FAIL)] — centred
      [inter-chirp gap]
      [payload signature: 3–6 chirps derived from payload hash]  — spatialised
      [silence pad]

    The cue chirp is always centred (azimuth 0°) — it is payload-independent
    and serves as a neutral "frame arrived" signal.  The signature chirps are
    spatialised at the azimuth derived from the payload hash so each distinct
    payload has a consistent left/right headphone position.
    """
    pad_n = int(SILENCE_PAD_S * AUDIO_SAMPLE_RATE)
    gap_n = int(INTER_CHIRP_S * AUDIO_SAMPLE_RATE)

    # Cue — centred (ITD=0, ILD=0)
    cue_mono = _chirp(CUE_F_LO, CUE_F_HI, CUE_DURATION_S, upchirp=crc_ok)

    # Payload signature (mono, then spatialise as a whole)
    p = _signature_params(payload)
    sig_mono: list[float] = []
    for i in range(p["n_chirps"]):
        upchirp = bool((p["dir_mask"] >> i) & 1)
        sig_mono += _chirp(p["f_lo"], p["f_hi"], p["duration_s"], upchirp=upchirp)

    # Spatialise signature
    sig_l, sig_r = _apply_itd_ild(sig_mono, p["azimuth_deg"])

    # Assemble: cue is identical on both channels; signature is spatialised
    silence = _silence(pad_n)
    gap = _silence(gap_n)
    left = silence + cue_mono + gap + sig_l + silence
    right = silence + cue_mono + gap + sig_r + silence

    # Ensure equal length (ITD shift can cause ±1 sample difference)
    n = min(len(left), len(right))
    return left[:n], right[:n]


# ---------------------------------------------------------------------------
# WAV I/O with RIFF INFO metadata
# ---------------------------------------------------------------------------


def _pack_riff_info(comment: str, name: str, keywords: str) -> bytes:
    """Pack a RIFF LIST INFO chunk with ICMT, INAM, and IKEY sub-chunks.

    ICMT — human-readable PHY summary (SF, BW, CRC, SNR, seq, sync)
    INAM — 16-hex filename stem (SHA-256 of payload)
    IKEY — machine-readable key=value pairs for resynthesis:
             payload_hex=<hex> crc_ok=<0|1>
    """

    def subchunk(fcc: bytes, text: str) -> bytes:
        data = text.encode("utf-8", errors="replace")
        # RIFF chunks must be WORD-aligned; pad with a NUL byte if needed
        if len(data) % 2:
            data += b"\x00"
        return fcc + struct.pack("<I", len(data)) + data

    body = (
        subchunk(b"ICMT", comment)
        + subchunk(b"INAM", name)
        + subchunk(b"IKEY", keywords)
    )
    return b"LIST" + struct.pack("<I", 4 + len(body)) + b"INFO" + body


def read_wav_keywords(path: Path) -> dict[str, str]:
    """Parse the IKEY sub-chunk from a WAV file written by save_wav().

    Returns a dict of key=value pairs, e.g.
      {"payload_hex": "48656c6c6f", "crc_ok": "1"}
    Returns an empty dict if the chunk is absent or malformed.
    """
    raw = path.read_bytes()
    # Scan for the b"IKEY" four-character code
    pos = raw.find(b"IKEY")
    if pos < 0:
        return {}
    chunk_size = struct.unpack_from("<I", raw, pos + 4)[0]
    text = (
        raw[pos + 8 : pos + 8 + chunk_size]
        .rstrip(b"\x00")
        .decode("utf-8", errors="replace")
    )
    result: dict[str, str] = {}
    for token in text.split():
        if "=" in token:
            k, _, v = token.partition("=")
            result[k] = v
    return result


def save_wav(
    path: Path,
    stereo: tuple[list[float], list[float]],
    comment: str,
    name: str,
    keywords: str,
) -> None:
    """Write stereo samples to *path* as 2-channel 16-bit PCM WAV with INFO metadata.

    *stereo* is a *(left, right)* tuple of equal-length float lists.
    Frames are interleaved as L0 R0 L1 R1 … per the WAV specification.

    Three RIFF INFO sub-chunks are embedded:
      ICMT — human-readable PHY summary
      INAM — 16-hex filename stem (SHA-256 of payload)
      IKEY — machine-readable key=value pairs for resynthesis
    """
    left, right = stereo
    if len(left) != len(right):
        raise ValueError(
            f"left/right channels must be same length ({len(left)} vs {len(right)})"
        )

    # Interleave L/R into a flat 16-bit PCM byte string
    n_frames = len(left)
    pcm = struct.pack(
        f"<{n_frames * 2}h",
        *(
            v
            for lr in zip(left, right)
            for v in (int(lr[0] * 32767), int(lr[1] * 32767))
        ),
    )

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(AUDIO_SAMPLE_RATE)
        wf.writeframes(pcm)

    wav_bytes = buf.getvalue()

    # Append RIFF INFO chunk after the data chunk and fix RIFF size
    info_chunk = _pack_riff_info(comment, name, keywords)
    new_body = wav_bytes[8:] + info_chunk  # strip "RIFF????WAVE" header
    riff_size = 4 + len(new_body)
    final = b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + new_body[4:]

    path.write_bytes(final)


# ---------------------------------------------------------------------------
# Frame key
# ---------------------------------------------------------------------------


def frame_key(payload: bytes) -> str:
    """Return 16-hex-char SHA-256 digest of *payload* — used as filename stem."""
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Playback thread
# ---------------------------------------------------------------------------


def _playback_worker(
    play_queue: "queue.Queue[tuple[list[float], list[float]] | None]",
) -> None:
    """Background thread: dequeues stereo audio and plays sequentially.

    Each item is a *(left, right)* float-list pair.  Sending *None* terminates
    the thread.  Each clip plays to completion before the next is dequeued.
    """
    try:
        import numpy as np  # type: ignore[import]
        import sounddevice as sd  # type: ignore[import]
    except ImportError:
        log.warning("sounddevice/numpy unavailable — playback thread exiting")
        return

    while True:
        item = play_queue.get()
        if item is None:
            break
        left, right = item
        # shape (n_frames, 2) — sounddevice interprets axis-1 as channels
        arr = np.column_stack(
            [
                np.array(left, dtype=np.float32),
                np.array(right, dtype=np.float32),
            ]
        )
        try:
            sd.play(arr, samplerate=AUDIO_SAMPLE_RATE, blocksize=PLAYBACK_BLOCKSIZE)
            sd.wait()
        except Exception as exc:  # noqa: BLE001
            log.debug("playback error: %s", exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run(
    host: str,
    port: int,
    audio_dir: Path,
    *,
    play: bool,
) -> None:
    audio_dir.mkdir(parents=True, exist_ok=True)

    sock, sub_msg, addr = create_udp_subscriber(host, port)
    local_port = sock.getsockname()[1]
    log.info("subscribed to lora_trx at %s:%d (local :%d)", host, port, local_port)

    play_queue: queue.Queue[tuple[list[float], list[float]] | None] = queue.Queue()
    player_thread: threading.Thread | None = None

    if play:
        try:
            import numpy  # type: ignore[import]  # noqa: F401
            import sounddevice  # type: ignore[import]  # noqa: F401

            player_thread = threading.Thread(
                target=_playback_worker,
                args=(play_queue,),
                daemon=True,
                name="lora-wav-player",
            )
            player_thread.start()
        except ImportError:
            log.warning("sounddevice/numpy not available — disabling playback")
            play = False

    last_keepalive = time.monotonic()
    waiting = False

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(65536)
                waiting = False
            except TimeoutError:
                sock.sendto(sub_msg, addr)
                last_keepalive = time.monotonic()
                if not waiting:
                    log.info("waiting for frames from %s:%d", host, port)
                    waiting = True
                continue

            now = time.monotonic()
            if now - last_keepalive >= KEEPALIVE_INTERVAL:
                sock.sendto(sub_msg, addr)
                last_keepalive = now

            try:
                msg = cbor2.loads(data)
            except Exception:
                continue

            if not isinstance(msg, dict) or msg.get("type") != "lora_frame":
                continue

            payload: bytes = msg.get("payload", b"")
            phy: dict[str, Any] = msg.get("phy", {})
            crc_ok: bool = msg.get("crc_valid", False)
            sf: int = phy.get("sf", 8)
            bw: int = phy.get("bw", 62500)
            snr: float | None = phy.get("snr_db")
            sync_word: int = phy.get("sync_word", 0)
            seq: int = msg.get("seq", 0)

            key = frame_key(payload)
            wav_path = audio_dir / f"{key}.wav"

            if not wav_path.exists():
                samples = synthesise_frame(payload=payload, crc_ok=crc_ok)
                snr_str = f"{snr:.1f}dB" if snr is not None else "n/a"
                crc_str = "OK" if crc_ok else "FAIL"
                comment = (
                    f"SF={sf} BW={bw} CRC={crc_str} "
                    f"SNR={snr_str} seq={seq} sync=0x{sync_word:02X}"
                )
                keywords = f"payload_hex={payload.hex()} crc_ok={1 if crc_ok else 0}"
                save_wav(
                    wav_path, samples, comment=comment, name=key, keywords=keywords
                )
                log.info(
                    "saved %s (%dB payload, CRC_%s)",
                    wav_path.name,
                    len(payload),
                    crc_str,
                )
            else:
                log.debug("skip existing %s", wav_path.name)

            if play:
                # Load from WAV (so cached files are played identically)
                with wave.open(str(wav_path)) as wf:
                    raw = wf.readframes(wf.getnframes())
                # Deinterleave 2-ch 16-bit PCM → (left, right) float lists
                n_samples = len(raw) // 2  # total int16 values (2 bytes each)
                flat = [
                    struct.unpack_from("<h", raw, i * 2)[0] / 32768.0
                    for i in range(n_samples)
                ]
                left_ch = flat[0::2]
                right_ch = flat[1::2]
                play_queue.put((left_ch, right_ch))

    except KeyboardInterrupt:
        pass
    finally:
        if player_thread is not None:
            play_queue.put(None)  # signal shutdown
            player_thread.join(timeout=2.0)
        sock.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    setproctitle("lora-wav")

    parser = argparse.ArgumentParser(
        prog="lora wav",
        description="LoRa chirp audio exporter — saves a WAV per decoded frame",
    )
    parser.add_argument(
        "--connect",
        metavar="HOST:PORT",
        default=None,
        help="lora-core UDP address (default from config.toml or 127.0.0.1:5555)",
    )
    parser.add_argument(
        "--audio-dir",
        metavar="PATH",
        default=DEFAULT_AUDIO_DIR,
        help=f"Directory for WAV output (default: {DEFAULT_AUDIO_DIR})",
    )
    parser.add_argument(
        "--no-play",
        action="store_true",
        default=False,
        help="Save WAV files without playing audio",
    )
    add_logging_args(parser)
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging("gr4.wav", log_level=args.log_level, no_color=args.no_color)

    try:
        host, port = resolve_udp_address(args.connect, cfg)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # parser.error already exits; for type-checker

    run(host, port, Path(args.audio_dir), play=not args.no_play)
    return 0


if __name__ == "__main__":
    sys.exit(main())
