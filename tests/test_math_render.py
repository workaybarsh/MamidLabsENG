"""LaTeX (math) -> readable Unicode conversion (qwen-desktop.ps1 Convert-MathText).

When the model produced a math answer, raw "$$", "\\frac{}{}", "\\int" markers looked like
"broken symbols" in the UI. Convert-MathText maps these to Unicode (∫, ∞, π, √, ∑), flattens
fractions to a/b and turns exponents/subscripts into Unicode (x^2 -> x²). It is readable text,
not real typesetting.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def extract_functions(source: str, names) -> str:
    import re
    out = []
    for name in names:
        m = re.search(r"function " + re.escape(name) + r"\b[\s\S]*?\n\}", source)
        assert m, f"{name} not found"
        out.append(m.group(0))
    return "\n".join(out)


def run_convert(samples) -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    src = read_text("qwen-desktop.ps1")
    fns = extract_functions(src, ["Convert-MathText", "Convert-ToSuperscript", "Convert-ToSubscript"])
    # Python decodes output as UTF-8; the console code page may differ (cp1254). Pin it.
    prelude = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    blocks = []
    for s in samples:
        # The here-string terminator ('@) must be at the start of a line (unindented).
        blocks.append("$__s = @'\n" + s + "\n'@\nWrite-Output (Convert-MathText $__s)")
    script = prelude + fns + "\n" + "\n".join(blocks) + "\n"
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_math_symbols_are_converted_to_unicode():
    samples = [
        r"$$\int_0^\infty \frac{(\ln x)^4}{1+x^6}\,dx < \infty$$",
        r"$$\sum_{n=1}^{\infty}\frac{\pi^2}{6}$$",
        r"$x \to 0^+$ ve $\alpha \geq \beta$",
    ]
    out = run_convert(samples)
    for glyph in ("\u222b", "\u221e", "\u03c0", "\u2211", "\u2192", "\u2265", "\u03b1"):
        assert glyph in out, f"missing glyph: {glyph!r}"
    assert "$$" not in out
    assert "\\frac" not in out
    assert "\\int" not in out


def test_exponents_and_subscripts_become_unicode():
    out = run_convert([r"x^4 + x^6 + x^{10}", r"\int_0^\infty", r"n^2"])
    for glyph in ("\u2074", "\u2076", "\u00b9", "\u2070", "\u2080", "\u00b2"):
        assert glyph in out, f"missing superscript/subscript: {glyph!r}"


def test_plain_currency_dollar_is_not_stripped_without_latex():
    out = run_convert(["Price: 100 $ and 200 $"])
    assert "100 $ and 200 $" in out


def test_plain_text_is_not_corrupted():
    """An earlier version's nested-array flattening corrupted letters ('Fiyat'->'Fiyaa'); regression."""
    out = run_convert(["Fiyat: 100 $ ve 200 $ (LaTeX yok)", "Merhaba dunya"])
    assert "Fiyat" in out
    assert "Merhaba dunya" in out
