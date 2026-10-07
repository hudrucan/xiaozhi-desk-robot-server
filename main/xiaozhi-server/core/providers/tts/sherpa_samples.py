"""Shared Sherpa normalization and mastering; no server runtime imports."""
import numpy as np


class SherpaSampleProcessing:
    def _normalize_numbers(self, text: str) -> str:
        if self._num2words is None:
            return text
        return self._INTEGER_PATTERN.sub(
            lambda match: self._num2words(
                int(match.group(0)), lang=self.number_language
            ),
            text,
        )

    @staticmethod
    def _dbfs_to_amplitude(dbfs: float) -> float:
        return 10.0 ** (dbfs / 20.0)

    def _master_samples(self, samples: np.ndarray) -> np.ndarray:
        samples = np.asarray(samples, dtype=np.float32)
        if not self.mastering:
            return np.clip(samples * self.volume_gain, -1.0, 1.0)

        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
        active = np.abs(samples) >= self._dbfs_to_amplitude(-50.0)
        if np.any(active):
            active_rms = float(
                np.sqrt(np.mean(np.square(samples[active], dtype=np.float64)))
            )
            if active_rms > 0:
                target_rms = self._dbfs_to_amplitude(
                    self.target_active_rms_dbfs
                )
                samples = samples * (target_rms / active_rms)

        samples = samples * self.volume_gain
        ceiling = self._dbfs_to_amplitude(self.peak_ceiling_dbfs)
        if ceiling == 0.0:
            return np.zeros_like(samples)
        knee = ceiling * self._dbfs_to_amplitude(-6.0)
        magnitude = np.abs(samples)
        above_knee = magnitude > knee
        if np.any(above_knee):
            span = ceiling - knee
            magnitude[above_knee] = knee + span * np.tanh(
                (magnitude[above_knee] - knee) / span
            )
            samples = np.copysign(magnitude, samples)
        return np.clip(samples, -ceiling, ceiling)
