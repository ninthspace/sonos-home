"""Left/right balance (docs/quick/07): one number per room from -100 (all left) to +100 (all right), 0 centred.

Sonos keeps balance as the speaker's left and right channel volumes (RenderingControl `LF`/`RF`, 0-100 each, 100/100 centred),
which SoCo reads and writes as `device.balance = (left, right)`. Balance is offered on stereo-capable products and on stereo
pairs; a single mono speaker accepts the setting but it changes nothing, so it isn't offered there.
"""

# Products Sonos lists as having a balance setting (support article "Adjust the bass, treble, balance, and loudness").
STEREO_MODELS = ("five", "port", "amp", "play:5", "play:3", "connect", "connect:amp")


def to_channels(balance: int) -> tuple[int, int]:
    """-100..100 to (left, right): the far side stays at 100 and the near side comes down."""
    return 100 - max(0, balance), 100 + min(0, balance)


def from_channels(left: int, right: int) -> int:
    """(left, right) to -100..100; a pair where neither side is 100 still reads as how much louder the right is."""
    return max(-100, min(100, int(right) - int(left)))


def model_has_balance(model: str) -> bool:
    name = (model or "").casefold().removeprefix("sonos").strip()
    return name in STEREO_MODELS


def room_has_balance(room: dict) -> bool:
    """A stereo-capable product, or one half of a stereo pair: the room's own speaker plays one channel, "LF" or "RF".
    A speaker with a bonded Sub or home-theatre surrounds plays "LF,RF" and its satellites "SW", "LR" or "RR", none of which
    Sonos offers balance on. Matched by uid, so a rename (which reaches the pair's halves at different times) can't change it."""
    if model_has_balance(room.get("model", "")):
        return True
    own = next((m for m in room.get("members") or [] if m.get("uid") == room.get("uid")), None)
    return bool(own) and own.get("channel") in ("LF", "RF")
