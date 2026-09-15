"""
Cross-platform font resolution.

The invoice layouts depend on having visually distinct typefaces — a sans, a
serif, a condensed sans, a second sans, and a monospace. Which actual font
files provide those differs by platform, and hardcoding one platform's paths
means the generator only runs there.

Each role below lists candidates in preference order. The first one that
exists on this machine wins. If none do, the role falls back to one of
reportlab's built-in Type1 fonts, which are always available but produce a
plainer-looking set of invoices.

Run this file directly to see what resolved on your system.
"""

from pathlib import Path
import sys

# Where to look, by platform.
SEARCH_DIRS = {
    "linux": [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/liberation"),
        Path("/usr/share/fonts/truetype"),
        Path("/usr/share/fonts"),
        Path.home() / ".fonts",
    ],
    "win32": [
        Path("C:/Windows/Fonts"),
        Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    ],
    "darwin": [
        Path("/System/Library/Fonts/Supplemental"),
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path.home() / "Library/Fonts",
    ],
}

# role -> (candidate filenames in preference order, built-in fallback)
ROLES = {
    "sans":       (["DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                    "arial.ttf", "Arial.ttf", "Helvetica.ttc",
                    "segoeui.ttf", "verdana.ttf"], "Helvetica"),
    "sans-bold":  (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
                    "arialbd.ttf", "Arial Bold.ttf", "Helvetica.ttc",
                    "segoeuib.ttf", "verdanab.ttf"], "Helvetica-Bold"),

    "serif":      (["DejaVuSerif.ttf", "LiberationSerif-Regular.ttf",
                    "times.ttf", "Times New Roman.ttf", "Times.ttc",
                    "georgia.ttf"], "Times-Roman"),
    "serif-bold": (["DejaVuSerif-Bold.ttf", "LiberationSerif-Bold.ttf",
                    "timesbd.ttf", "Times New Roman Bold.ttf", "Times.ttc",
                    "georgiab.ttf"], "Times-Bold"),

    # Condensed gives the dense layout its own character. Narrow faces are
    # not universal, so this degrades to the regular sans rather than to a
    # built-in — keeping the layout readable if a little wider.
    "cond":       (["DejaVuSansCondensed.ttf", "LiberationSansNarrow-Regular.ttf",
                    "arialn.ttf", "Arial Narrow.ttf", "tahoma.ttf",
                    "DejaVuSans.ttf", "arial.ttf"], "Helvetica"),
    "cond-bold":  (["DejaVuSansCondensed-Bold.ttf", "LiberationSansNarrow-Bold.ttf",
                    "arialnb.ttf", "Arial Narrow Bold.ttf", "tahomabd.ttf",
                    "DejaVuSans-Bold.ttf", "arialbd.ttf"], "Helvetica-Bold"),

    # A second sans, so the minimal layout doesn't look identical to modern.
    "sans2":      (["LiberationSans-Regular.ttf", "verdana.ttf", "Verdana.ttf",
                    "tahoma.ttf", "DejaVuSans.ttf", "arial.ttf"], "Helvetica"),
    "sans2-bold": (["LiberationSans-Bold.ttf", "verdanab.ttf", "Verdana Bold.ttf",
                    "tahomabd.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"],
                   "Helvetica-Bold"),

    "mono":       (["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf",
                    "consola.ttf", "cour.ttf", "Courier New.ttf",
                    "Menlo.ttc"], "Courier"),
}


def _dirs():
    key = "win32" if sys.platform.startswith("win") else \
          "darwin" if sys.platform == "darwin" else "linux"
    return [d for d in SEARCH_DIRS[key] if d.is_dir()]


def _find(filenames):
    dirs = _dirs()
    # Exact filename match first — fast and predictable.
    for name in filenames:
        for d in dirs:
            p = d / name
            if p.is_file():
                return p
    # Case-insensitive sweep, since font filename casing varies.
    lowered = {n.lower() for n in filenames}
    for d in dirs:
        try:
            for p in d.iterdir():
                if p.is_file() and p.name.lower() in lowered:
                    return p
        except (PermissionError, OSError):
            continue
    return None


def resolve():
    """role -> (path_or_None, builtin_fallback_name)"""
    return {role: (_find(names), fallback)
            for role, (names, fallback) in ROLES.items()}


def register(pdfmetrics, TTFont, prefix=""):
    """
    Register every resolvable role with reportlab and return a
    role -> font-name mapping the layouts can use directly.

    Roles that found no file map to a built-in Type1 name instead, which
    reportlab knows about without registration.
    """
    names = {}
    for role, (path, fallback) in resolve().items():
        if path is None:
            names[role] = fallback
            continue
        font_name = f"{prefix}{role}"
        try:
            pdfmetrics.registerFont(TTFont(font_name, str(path)))
            names[role] = font_name
        except Exception:
            # A font file that exists but reportlab can't parse (some .ttc
            # collections, broken installs) should not stop generation.
            names[role] = fallback
    return names


def truetype_path(role):
    """Filesystem path for PIL's ImageFont.truetype, or None."""
    path, _ = resolve()[role]
    return str(path) if path else None


if __name__ == "__main__":
    print(f"platform: {sys.platform}")
    print("searching:")
    for d in _dirs():
        print(f"  {d}")
    print()
    for role, (path, fallback) in resolve().items():
        if path:
            print(f"  {role:<12} {path}")
        else:
            print(f"  {role:<12} (none found — falling back to {fallback})")
