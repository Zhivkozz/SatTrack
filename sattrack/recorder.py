"""Запис на аудио от USB аудио устройството на радиото във WAV (FR-40, FR-41).

Модулът `wave` обновява заглавката след всеки запис на блок, така че при
срив файлът остава почти валиден; `repair_wav()` поправя размерите изцяло (FR-34).
"""

from __future__ import annotations

import logging
import queue
import struct
import threading
import wave
from pathlib import Path

log = logging.getLogger(__name__)


class RecorderError(RuntimeError):
    pass


def list_input_devices() -> list[tuple[int, str, str, int]]:
    import sounddevice as sd
    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, d["name"], apis[d["hostapi"]]["name"], int(d["default_samplerate"])))
    return out


def find_device(name_part: str) -> int:
    """Първото входно устройство, чието име съдържа зададения текст (предпочита MME)."""
    return input_candidates(name_part, "mme")[0][0]


# Пътищата през Windows (MME, DirectSound, споделен WASAPI) минават през звуковите
# ефекти на системата. Шумопотискането им изтрива стабилния FM шум след ~1 s и
# поврежда слабите сигнали (проверено на живо 2026-11-20: −28 dBFS -> −95 dBFS).
# Изключителният WASAPI и WDM-KS ги заобикалят, затова са първи.
HOST_API_ORDER = {
    "auto": ("wasapi-exclusive", "wdm-ks", "mme"),
    "wasapi-exclusive": ("wasapi-exclusive",),
    "wdm-ks": ("wdm-ks",),
    "mme": ("mme",),
}
_API_NAMES = {"wasapi-exclusive": "Windows WASAPI", "wdm-ks": "Windows WDM-KS", "mme": "MME"}


def input_candidates(name_part: str, host_api: str = "auto") -> list[tuple[int, str, int]]:
    """[(индекс, път, честота на дискретизация)] по реда на предпочитание."""
    devs = [d for d in list_input_devices() if name_part.lower() in d[1].lower()]
    if not devs:
        raise RecorderError(f"Няма аудио вход, съдържащ {name_part!r}. "
                            "Вижте `sattrack audio-devices` и recording.device в конфигурацията.")
    out = []
    for kind in HOST_API_ORDER.get(host_api, HOST_API_ORDER["auto"]):
        for idx, _name, api, sr in devs:
            if api == _API_NAMES[kind]:
                out.append((idx, kind, sr))
                break
    if not out:
        raise RecorderError(f"Входът {name_part!r} няма аудио път {host_api!r}.")
    return out


class WavRecorder:
    def __init__(self, device: str, samplerate: int = 48000, channels: int = 1,
                 host_api: str = "auto"):
        self.device_name = device
        self.samplerate = samplerate
        self.channels = channels
        self.host_api = host_api
        self.used_path: str | None = None   # кой аудио път е отворен (за дневника)
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._thread: threading.Thread | None = None
        self._wav: wave.Wave_write | None = None
        self.path: Path | None = None
        self.overflows = 0

    def _open_stream(self, cb):
        """Опитва пътищата по ред; връща (поток, път, честота)."""
        import sounddevice as sd
        errors = []
        for idx, kind, dev_sr in input_candidates(self.device_name, self.host_api):
            for sr in dict.fromkeys((self.samplerate, dev_sr)):
                extra = sd.WasapiSettings(exclusive=True) if kind == "wasapi-exclusive" else None
                try:
                    s = sd.RawInputStream(device=idx, samplerate=sr, channels=self.channels,
                                          dtype="int16", blocksize=sr // 10, callback=cb,
                                          extra_settings=extra)
                    return s, kind, sr
                except Exception as e:  # заето устройство, неподдържан формат
                    errors.append(f"{kind} {sr} Hz: {e}")
        raise RecorderError("Аудио входът не може да се отвори: " + "; ".join(errors))

    def start(self, path: Path) -> None:
        def cb(indata, frames, t, status):
            if status.input_overflow:
                self.overflows += 1
            self._q.put(bytes(indata))

        self._stream, self.used_path, sr = self._open_stream(cb)
        if self.used_path == "mme":
            log.warning("Аудиото се записва през MME: звуковите ефекти на Windows може да "
                        "изтриват шума и слабите сигнали. Затворете програмите, които ползват "
                        "входа на радиото, или изключете „Audio enhancements“ за него.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(self.channels)
        self._wav.setsampwidth(2)
        self._wav.setframerate(sr)
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()
        self._stream.start()
        log.info("Запис (%s, %d Hz): %s", self.used_path, sr, path)

    def _writer(self) -> None:
        while True:
            chunk = self._q.get()
            if chunk is None:
                break
            try:
                self._wav.writeframes(chunk)
            except Exception as e:  # пълен диск и др.
                log.error("Грешка при запис на аудио: %s", e)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        if self._thread is not None:
            self._q.put(None)
            self._thread.join(timeout=10)
            self._thread = None
        if self._wav is not None:
            self._wav.close()
            self._wav = None
        if self.overflows:
            log.warning("Аудио: %d препълвания на буфера", self.overflows)


class NullRecorder:
    """За dry-run: не записва нищо."""
    path: Path | None = None

    def start(self, path: Path) -> None:
        self.path = None
        log.info("[dry-run] без аудио запис (би бил %s)", path)

    def stop(self) -> None:
        pass


def repair_wav(path: Path) -> bool:
    """Поправя RIFF и data размерите по действителния размер на файла."""
    try:
        with open(path, "r+b") as f:
            head = f.read(4096)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False
            pos = 12
            while pos + 8 <= len(head):
                cid, size = head[pos:pos + 4], struct.unpack("<I", head[pos + 4:pos + 8])[0]
                if cid == b"data":
                    f.seek(0, 2)
                    total = f.tell()
                    data_len = total - (pos + 8)
                    data_len -= data_len % 2
                    f.seek(4)
                    f.write(struct.pack("<I", pos + data_len))
                    f.seek(pos + 4)
                    f.write(struct.pack("<I", data_len))
                    return True
                pos += 8 + size + (size & 1)
    except OSError as e:
        log.warning("Не може да се поправи %s: %s", path, e)
    return False
