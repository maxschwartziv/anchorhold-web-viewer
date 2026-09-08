"""The AnchorHold mark, on every window this project opens.

Two things make this less obvious than one call.

Tk holds only a weak claim on a PhotoImage: as soon as nothing in Python
refers to it the image is collected and the icon silently reverts to the
default feather. The image is therefore parked on the window it was set on.

The `default=True` argument makes it the icon for every window opened
afterwards as well, which is what covers the progress and picker dialogs
without each of them repeating this.

Nothing here raises. A missing or unreadable icon file is a cosmetic problem,
and a program that refuses to open a recording over one would be worse.
"""

from __future__ import annotations

import os
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
# Beside the icons the browser app already serves, so there is one place the
# mark lives rather than two.
ICON = os.path.join(os.path.dirname(HERE), "web", "icons", "icon_source.png")


def apply(window) -> bool:
    """Set the window icon. Returns whether it took."""
    if not os.path.isfile(ICON):
        return False
    try:
        image = tk.PhotoImage(file=ICON, master=window)
        window.iconphoto(True, image)
        # Kept alive for the life of the window, not the life of this call.
        window._anchorhold_icon = image
        return True
    except Exception:
        return False
