"""Seekable WAV playback for the recordings tab (winsound cannot seek).

Plays 16-bit PCM WAV files on the default output device through sounddevice,
reading the file block by block in the audio callback. The file is open only
while it plays, so a selected recording can still be deleted on Windows.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

log = logging.getLogger(__name__)


class Player:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wav: wave.Wave_read | None = None
        self._stream = None
        self.path: Path | None = None
        self.samplerate = 0
        self.channels = 1
        self.frames = 0
        self._pos = 0          # next frame to play

    def load(self, path: Path) -> None:
        """Select a file: read its format and length, position at the start."""
        self.stop()
        with wave.open(str(path)) as w:
            if w.getsampwidth() != 2:
                raise ValueError("поддържат се само 16-битови WAV файлове")
            self.samplerate, self.channels, self.frames = w.getframerate(), w.getnchannels(), w.getnframes()
        self.path = Path(path)
        self._pos = 0

    def unload(self) -> None:
        self.stop()
        self.path = None
        self.frames = self._pos = 0

    @property
    def duration(self) -> float:
        return self.frames / self.samplerate if self.samplerate else 0.0

    @property
    def position(self) -> float:
        return self._pos / self.samplerate if self.samplerate else 0.0

    @property
    def playing(self) -> bool:
        s = self._stream
        return s is not None and s.active

    def seek(self, seconds: float) -> None:
        with self._lock:
            self._pos = max(0, min(self.frames, int(seconds * self.samplerate)))

    def play(self, start: float | None = None) -> None:
        if self.path is None:
            return
        self.stop()
        if start is not None:
            self.seek(start)
        if self._pos >= self.frames:
            self._pos = 0
        import sounddevice as sd

        def cb(outdata, frames, _t, _status):
            with self._lock:
                w = self._wav
                data = b""
                if w is not None:
                    w.setpos(min(self._pos, self.frames))
                    data = w.readframes(frames)
                    self._pos += len(data) // (2 * self.channels)
            outdata[:len(data)] = data
            if len(data) < len(outdata):
                outdata[len(data):] = b"\x00" * (len(outdata) - len(data))
                raise sd.CallbackStop

        self._wav = wave.open(str(self.path))
        try:
            self._stream = sd.RawOutputStream(samplerate=self.samplerate, channels=self.channels,
                                              dtype="int16", callback=cb)
            self._stream.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Pause: stop the sound and release the file, keep the position."""
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception as e:  # device unplugged etc.
                log.debug("player stop: %s", e)
        with self._lock:
            if self._wav is not None:
                self._wav.close()
                self._wav = None
