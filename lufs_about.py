"""LUFS Lab - the About window (credits, donation link, update switch)."""
from __future__ import annotations

import webbrowser
from urllib.parse import urlencode

APP_NAME = "LUFS Lab"
TAGLINE = "Loudness leveling for music and vocals"
AUTHOR_LINE = "Coded by Claude & Emmanuel Hoachlander"
DONATE_EMAIL = "section9agent@hotmail.com"
DONATE_URL = "https://www.paypal.com/donate/?" + urlencode({
    "business": DONATE_EMAIL, "no_recurring": "0",
    "item_name": "LUFS Lab donation", "currency_code": "USD",
})

# (name, what it does for LUFS Lab, license, link)
CREDITS = [
    ("Python and Tcl/Tk (tkinter)", "the programming language and the window toolkit",
     "Python Software Foundation License; Tcl/Tk license", "https://www.python.org"),
    ("NumPy", "fast audio maths", "BSD-3-Clause", "https://numpy.org"),
    ("SciPy", "filters and sample-rate conversion", "BSD-3-Clause", "https://scipy.org"),
    ("SoundFile (python-soundfile)", "reading and writing audio files", "BSD-3-Clause",
     "https://github.com/bastibe/python-soundfile"),
    ("libsndfile", "the audio file engine inside SoundFile (WAV, FLAC, MP3, OGG, AIFF)",
     "LGPL-2.1-or-later", "https://libsndfile.github.io/libsndfile/"),
    ("lameenc", "Python bindings used to create MP3 files", "LGPL-3.0-or-later",
     "https://github.com/chrisstaite/lameenc"),
    ("LAME", "the MP3 encoder inside lameenc", "LGPL", "https://lame.sourceforge.io"),
    ("mpg123", "MP3 decoding inside libsndfile", "LGPL-2.1", "https://www.mpg123.de"),
    ("tkinterdnd2", "drag-and-drop into the window", "MIT", "https://github.com/Eliav2/tkinterdnd2"),
    ("tkdnd", "the native drag-and-drop library behind tkinterdnd2 (see its page for its license)",
     "see project page", "https://github.com/petasis/tkdnd"),
    ("PyInstaller", "turns the program into a stand-alone app",
     "GPL-2.0-or-later with an exception that lets it build programs under any license",
     "https://pyinstaller.org"),
]

STANDARDS_NOTE = (
    "Loudness is measured to the ITU-R BS.1770-4 / EBU R128 standard (K-weighting, gating and "
    "true-peak detection), implemented from the published specification. The results were "
    "cross-checked against FFmpeg's ebur128 meter (https://ffmpeg.org) and pyloudnorm (MIT, "
    "https://github.com/csteinmetz1/pyloudnorm); neither is part of this program."
)
CREDITS_FOOTER = ("Every library above is the work of its authors and is used under its own license. "
                  "The full license texts are in each project's repository.")


def show_about(parent, version, auto_var, on_check, icon_images=None, open_url=None):
    """Open the About window. `auto_var` is a tkinter BooleanVar shared with the Help menu."""
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk

    if open_url is None:
        def open_url(url):
            webbrowser.open(url)

    win = tk.Toplevel(parent)
    win.title(f"About {APP_NAME}")
    win.transient(parent)
    win.geometry("600x720")
    win.minsize(520, 560)

    big = tkfont.nametofont("TkDefaultFont").copy()
    big.configure(size=int(abs(big.cget("size")) * 1.9), weight="bold")
    bold = tkfont.nametofont("TkDefaultFont").copy()
    bold.configure(weight="bold")
    win._fonts = (big, bold)                                 # keep references alive

    head = ttk.Frame(win)
    head.pack(fill="x", padx=16, pady=(14, 4))
    if icon_images and len(icon_images) > 1:
        ttk.Label(head, image=icon_images[1]).pack(side="left", padx=(0, 12))
    titles = ttk.Frame(head)
    titles.pack(side="left")
    ttk.Label(titles, text=APP_NAME, font=big).pack(anchor="w")
    ttk.Label(titles, text=f"Version {version}").pack(anchor="w")
    ttk.Label(titles, text=TAGLINE, foreground="#555555").pack(anchor="w")

    ttk.Label(win, text=AUTHOR_LINE, font=bold).pack(anchor="w", padx=16, pady=(6, 8))

    don = ttk.LabelFrame(win, text="Support LUFS Lab")
    don.pack(fill="x", padx=16, pady=4)
    ttk.Label(don, text="If you enjoy this program, please consider a donation.",
              wraplength=520, justify="left").pack(anchor="w", padx=10, pady=(8, 4))
    ttk.Button(don, text="Donate with PayPal...", command=lambda: open_url(DONATE_URL)).pack(
        anchor="w", padx=10, pady=(0, 4))
    ttk.Label(don, text="Opens PayPal in your web browser. Thank you!",
              foreground="#555555").pack(anchor="w", padx=10, pady=(0, 8))

    upd = ttk.LabelFrame(win, text="Updates")
    upd.pack(fill="x", padx=16, pady=4)
    ttk.Checkbutton(upd, text="Check for updates automatically", variable=auto_var).pack(
        anchor="w", padx=10, pady=(8, 2))
    ttk.Button(upd, text="Check for updates now", command=on_check).pack(anchor="w", padx=10, pady=(2, 8))

    cr = ttk.LabelFrame(win, text="Open-source code used in this program")
    cr.pack(fill="both", expand=True, padx=16, pady=4)
    box = ttk.Frame(cr)
    box.pack(fill="both", expand=True, padx=8, pady=8)
    text = tk.Text(box, wrap="word", height=10, relief="flat", borderwidth=0, padx=4, pady=2)
    sb = ttk.Scrollbar(box, orient="vertical", command=text.yview)
    text.config(yscrollcommand=sb.set)
    text.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")
    text.tag_configure("name", font=bold)
    text.tag_configure("dim", foreground="#555555")
    try:
        hand = "pointinghand" if parent.tk.call("tk", "windowingsystem") == "aqua" else "hand2"
    except Exception:
        hand = "hand2"

    def link(url, idx):
        tag = f"link{idx}"
        text.tag_configure(tag, foreground="#1a5fb4", underline=True)
        text.tag_bind(tag, "<Button-1>", lambda e, u=url: open_url(u))
        text.tag_bind(tag, "<Enter>", lambda e: text.config(cursor=hand))
        text.tag_bind(tag, "<Leave>", lambda e: text.config(cursor=""))
        return tag

    for i, (name, what, lic, url) in enumerate(CREDITS):
        text.insert("end", name, "name")
        text.insert("end", f" - {what}\n")
        text.insert("end", f"License: {lic}\n", "dim")
        text.insert("end", url + "\n\n", link(url, i))
    text.insert("end", STANDARDS_NOTE + "\n\n")
    text.insert("end", CREDITS_FOOTER, "dim")
    text.config(state="disabled")

    ttk.Button(win, text="Close", command=win.destroy).pack(pady=(6, 12))
    win.bind("<Escape>", lambda e: win.destroy())
    win.focus_set()
    return win
