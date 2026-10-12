"""Shared data types: settings, thresholds, and the CaptureData container."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np


@dataclass
class ChannelConfig:
    n: int
    label: str
    scale: float            # V/div
    offset: float = 0.0     # V
    enabled: bool = True


def default_channels() -> list[ChannelConfig]:
    return [
        ChannelConfig(1, "Crank @ PCM", 2.0, 0.0),
        ChannelConfig(2, "Crank @ sensor", 2.0, 0.0),
        ChannelConfig(3, "5V ref @ sensor", 0.5, -5.0),
        ChannelConfig(4, "PCM low ref vs batt -", 0.2, 0.0),
    ]


@dataclass
class CaptureSettings:
    seconds: float = 6.0
    pretrig: float = 0.05
    depth: str = "10M"
    trig_level: float = 2.5
    wait: float = 120.0
    channels: list[ChannelConfig] = field(default_factory=default_channels)


@dataclass
class Thresholds:
    hi: float = 3.5               # Schmitt high threshold (V)
    lo: float = 1.5               # Schmitt low threshold (V)
    runt_hi: float = 4.0          # a high pulse peaking below this is a runt
    runt_lo: float = 1.0          # a low pulse bottoming above this is weak
    teeth: int = 58               # rising edges per crank revolution
    min_edges: int = 12           # fewer rising edges than this = no usable signal
    skip_first_edges: int = 4     # ignore the starter spin-up transient
    ref_nominal: float = 5.0
    ref_sag_mv: float = 300.0     # 5V-ref sag that counts as a finding
    ground_mv: float = 150.0      # low-ref deviation that counts as a finding
    event_window_s: float = 0.0005


@dataclass
class CaptureData:
    t: np.ndarray                         # seconds relative to trigger
    channels: dict[int, np.ndarray]       # channel number -> volts
    labels: dict[int, str]
    meta: dict = field(default_factory=dict)

    def save(self, path: str) -> None:
        """Atomic write: temp file then rename."""
        tmp = path + ".tmp"
        arrays = {f"ch{n}": v for n, v in self.channels.items()}
        with open(tmp, "wb") as f:
            np.savez_compressed(
                f, t=self.t,
                meta=json.dumps(self.meta),
                labels=json.dumps({str(k): v for k, v in self.labels.items()}),
                **arrays)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "CaptureData":
        with np.load(path, allow_pickle=False) as d:
            t = d["t"]
            channels = {int(k[2:]): d[k] for k in d.files
                        if k.startswith("ch") and k[2:].isdigit()}
            if "labels" in d.files:
                labels = {int(k): v for k, v in json.loads(str(d["labels"])).items()}
                meta = json.loads(str(d["meta"]))
            else:  # files written by the original crank_capture.py: meta held the labels
                labels = {int(k): v for k, v in json.loads(str(d["meta"])).items()}
                meta = {}
        return cls(t, channels, labels, meta)
