"""Speech detection for quiet microphones, and gain before transcription (voice/audio, stt)."""

from __future__ import annotations

import numpy as np

from personal_os.voice.audio import BLOCK, MIC_FLOOR, endpoint, threshold
from personal_os.voice.stt import MAX_GAIN, normalize


def tone(level: float, blocks: int = 1) -> list[np.ndarray]:
    """Blocks of a 220 Hz tone with the given RMS level."""
    t = np.arange(BLOCK * blocks) / 16_000
    wave = np.sin(2 * np.pi * 220 * t) * level * np.sqrt(2)
    return list(wave.astype(np.float32).reshape(blocks, BLOCK))


def test_quiet_laptop_mic_speech_counts_as_speech():
    noise = 0.00006  # measured on a Realtek mic array: background
    blocks = tone(0.00006, 5) + tone(0.004, 8) + tone(0.00006, 5)  # speech at 0.004 RMS
    assert threshold(noise) == MIC_FLOOR
    span = endpoint(blocks, noise)
    assert span is not None and span[0] <= 5 and span[1] >= 12


def test_silence_and_hum_are_not_speech():
    assert endpoint(tone(0.0005, 20), 0.0005) is None  # steady hum at its own noise level
    assert endpoint(tone(0.001, 20), 0.00006) is None  # below the floor
    assert endpoint(tone(0.004, 10), 0.004, floor=MIC_FLOOR) is None  # loud room: 3x noise rule


def test_a_custom_floor_is_used():
    assert endpoint(tone(0.001, 10), 0.0001, floor=0.0005) is not None


def test_quiet_audio_is_raised_and_loud_audio_left_alone():
    quiet = np.concatenate(tone(0.05, 4))  # peak 0.07: needs ~13x, under the cap
    out = normalize(quiet)
    assert 0.85 < float(np.max(np.abs(out))) <= 0.9 + 1e-6
    very_quiet = np.concatenate(tone(0.0001, 4))
    gain = float(np.max(np.abs(normalize(very_quiet)))) / float(np.max(np.abs(very_quiet)))
    assert abs(gain - MAX_GAIN) < 1e-3  # capped, so hiss isn't blown up
    loud = np.concatenate(tone(0.5, 2))
    assert np.array_equal(normalize(loud), loud)
    assert normalize(np.zeros(10, dtype=np.float32)).max() == 0
