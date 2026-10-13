"""Voicebox MLX adapter. No unconditioned/generic-voice fallback is permitted."""
import inspect
import sys

from .core import ROOT, run, probe

ENERGETIC_FILTER = (
    'highpass=f=75,equalizer=f=3000:t=q:w=1:g=2,'
    'acompressor=threshold=0.125:ratio=2:attack=8:release=100:makeup=1.2,'
    'atempo=1.06,alimiter=limit=0.95:latency=1'
)


def apply_energetic_delivery(output):
    """Pitch-preserving pace and presence; not unsupported emotion prompting."""
    processed = output.with_name(output.stem+'-energetic.wav')
    try:
        run([ROOT/'bin/ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', output,
             '-af', ENERGETIC_FILTER, '-ar', '24000', '-ac', '1', processed])
        duration = probe(processed)[0]
        processed.replace(output)
        return duration
    finally:
        processed.unlink(missing_ok=True)


class Voice:
    def __init__(self, sample, transcript):
        if not sample.is_file() or not transcript.strip():
            raise ValueError("Upload your voice and its exact transcript first")
        self.sample, self.transcript = sample, transcript
        self.backend = None

    async def load(self):
        sys.path.insert(0, str(ROOT/'tools/voicebox'))
        from backend import config
        config.set_data_dir(ROOT/'data/narrated/voicebox')
        from backend.backends.mlx_backend import MLXTTSBackend
        self.backend = MLXTTSBackend(model_size='0.6B')
        # This adapter runs in a dedicated worker process. Keep model loading
        # and inference on its main thread: upstream's asyncio.to_thread loader
        # creates MLX GPU streams that cannot be reused here after its executor
        # exits ("There is no Stream(gpu, 1) in current thread").
        self.backend._load_model_sync('0.6B')
        if 'ref_audio' not in inspect.signature(self.backend.model.generate).parameters:
            raise RuntimeError("This Voicebox model does not support voice cloning")

    def speak(self, text, output):
        import numpy as np
        import soundfile as sf
        if not self.backend or not self.sample.is_file():
            raise RuntimeError("Cloned voice is unavailable")
        chunks, rate = [], None
        # Use the Voicebox-loaded model directly because upstream generate()
        # silently falls back to an unrelated voice when conditioning fails.
        for result in self.backend.model.generate(
            text, ref_audio=str(self.sample), ref_text=self.transcript, lang_code='English'
        ):
            if rate is not None and rate != result.sample_rate:
                raise RuntimeError("Inconsistent speech sample rate")
            rate = result.sample_rate
            chunks.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
        if not chunks:
            raise RuntimeError("Voicebox returned no audio")
        audio = np.concatenate(chunks)
        if not len(audio) or not np.isfinite(audio).all() or np.max(np.abs(audio)) < .0001:
            raise RuntimeError("Voicebox returned invalid or silent audio")
        sf.write(str(output), audio, rate)
        return apply_energetic_delivery(output)
