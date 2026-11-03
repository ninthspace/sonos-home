"""The fake Sonos system lives in the package so `sonos service run --fake-rooms` can use it."""

from sonos_cli.fakes import *  # noqa: F401,F403
from sonos_cli.fakes import FakeSnapshot, FakeSoCo, FakeSystem  # noqa: F401
