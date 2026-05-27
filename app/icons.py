"""
app/icons.py
============
Generates PIL-based tray icons for each bot state.

States
------
  idle     — gray   (#6B7280)  market closed / no signal
  pending  — amber  (#F59E0B)  waiting for Telegram approval
  open     — green  (#10B981)  trade is active
  error    — red    (#EF4444)  exception / startup failure

Requires: Pillow  (pip install Pillow)
"""

from io import BytesIO
from typing import Optional

# Lazy import so the module loads even if Pillow is not yet installed
_PIL_AVAILABLE = False
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    pass

# =============================================================================
# COLOUR MAP
# =============================================================================

_COLOURS = {
    "idle":    (107, 114, 128),   # gray
    "pending": (245, 158,  11),   # amber
    "open":    ( 16, 185, 129),   # green
    "error":   (239,  68,  68),   # red
}

_SIZE = 64   # Icon dimensions in pixels


# =============================================================================
# ICON GENERATION
# =============================================================================

def make_icon(state: str = "idle") -> Optional[object]:
    """
    Return a PIL Image representing the given state, or None if Pillow not available.

    Parameters
    ----------
    state : "idle" | "pending" | "open" | "error"
    """
    if not _PIL_AVAILABLE:
        return None

    colour = _COLOURS.get(state, _COLOURS["idle"])
    bg     = (30, 30, 30, 0)     # transparent background

    img  = Image.new("RGBA", (_SIZE, _SIZE), bg)
    draw = ImageDraw.Draw(img)

    # Filled circle
    pad = 4
    draw.ellipse([pad, pad, _SIZE - pad, _SIZE - pad], fill=(*colour, 255))

    # "TB" label in white
    try:
        # Try to use a reasonable font — falls back to default bitmap
        font = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = ImageFont.load_default()

    try:
        bbox  = draw.textbbox((0, 0), "TB", font=font)
        tw    = bbox[2] - bbox[0]
        th    = bbox[3] - bbox[1]
    except AttributeError:
        tw, th = draw.textsize("TB", font=font)  # type: ignore[attr-defined]

    tx = (_SIZE - tw) // 2
    ty = (_SIZE - th) // 2
    draw.text((tx, ty), "TB", fill=(255, 255, 255, 230), font=font)

    return img


def make_icon_bytes(state: str = "idle") -> Optional[bytes]:
    """Return PNG bytes of the icon, or None if Pillow not available."""
    img = make_icon(state)
    if img is None:
        return None
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
