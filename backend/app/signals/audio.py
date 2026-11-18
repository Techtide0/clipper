from pathlib import Path

import numpy as np
import soundfile as sf


def loudness_per_second(wav: Path) -> np.ndarray:
    """RMS loudness (dB) for each second. Streams the file so 5h streams fit in RAM."""
    with sf.SoundFile(wav) as f:
        sr = f.samplerate
        vals = []
        for block in f.blocks(blocksize=sr, dtype="float32"):
            if block.ndim > 1:
                block = block.mean(axis=1)
            rms = float(np.sqrt(np.mean(block**2))) if len(block) else 0.0
            vals.append(20 * np.log10(rms + 1e-9))
    return np.array(vals)


def baseline_zscore(x: np.ndarray, window: int = 300) -> np.ndarray:
    """How far each value sits above its local typical level (z-score), floored at 0."""
    if len(x) == 0:
        return x
    window = max(1, min(window, len(x)))
    kernel = np.ones(window) / window
    pad = window // 2
    mean = np.convolve(np.pad(x, pad, mode="edge"), kernel, mode="valid")[: len(x)]
    sq = np.convolve(np.pad(x**2, pad, mode="edge"), kernel, mode="valid")[: len(x)]
    std = np.sqrt(np.maximum(sq - mean**2, 1e-6))
    return np.maximum((x - mean) / std, 0)
