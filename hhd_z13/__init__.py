from typing import Sequence

from hhd.plugins import HHDPlugin

from .plugin import Z13CpuPlugin
from .display import Z13DisplayPlugin


def autodetect(existing: Sequence[HHDPlugin]) -> Sequence[HHDPlugin]:
    if len(existing):
        return existing
    return [Z13CpuPlugin(), Z13DisplayPlugin()]
