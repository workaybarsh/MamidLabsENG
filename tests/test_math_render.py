"""LaTeX (matematik) -> okunur Unicode donusumu (qwen-desktop.ps1 Convert-MathText).

Model matematik yaniti urettiginde ham "$$", "\\frac{}{}", "\\int" gibi isaretler
arayuzde "bozuk sembol" gibi gorunuyordu. Convert-MathText bunlari Unicode
karsiliklariyla degistirir (∫, ∞, π, √, ∑), kesirleri a/b yapar ve us/alt simgeleri
cevirir (x^2 -> x²). Gercek matematik dizgisi degil; okunur metin amaclidir.
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
        assert m, f"{name} bulunamadi"
        out.append(m.group(0))
    return "\n".join(out)


def run_convert(samples) -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    src = read_text("qwen-desktop.ps1")
    fns = extract_functions(src, ["Convert-MathText", "Convert-ToSuperscript", "Convert-ToSubscript"])
    # Python ciktiyi UTF-8 cozer; konsol kod sayfasi farkli olabilir (cp1254). Sabitle.
    prelude = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    blocks = []
    for s in samples:
        # Here-string terminatöru ('@) satir basinda (girintisiz) olmali.
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
        assert glyph in out, f"eksik glif: {glyph!r}"
    # Ham LaTeX isaretleri kalmamali
    assert "$$" not in out
    assert "\\frac" not in out
    assert "\\int" not in out


def test_exponents_and_subscripts_become_unicode():
    out = run_convert([r"x^4 + x^6 + x^{10}", r"\int_0^\infty", r"n^2"])
    for glyph in ("\u2074", "\u2076", "\u00b9", "\u2070", "\u2080", "\u00b2"):
        assert glyph in out, f"eksik us/alt simge: {glyph!r}"


def test_plain_currency_dollar_is_not_stripped_without_latex():
    out = run_convert(["Fiyat: 100 $ ve 200 $"])
    assert "100 $ ve 200 $" in out


def test_plain_text_is_not_corrupted():
    """Onceki surumde ic ice dizi duzlesmesi harfleri bozuyordu ('Fiyat'->'Fiyaa'); regresyon."""
    out = run_convert(["Fiyat: 100 $ ve 200 $ (LaTeX yok)", "Merhaba dunya"])
    assert "Fiyat" in out
    assert "Merhaba dunya" in out


def test_chemistry_symbols_and_mhchem_ce():
    """Kimya: denge oku (\\rightleftharpoons -> ⇌) ve mhchem \\ce{...} icerigi okunur hale
    gelir (element sayilari alt simge, oklar ok). Kod bloklarindaki '->' bozulmaz."""
    out = run_convert([
        r"A + B \rightleftharpoons C",
        r"\ce{2H2 + O2 -> 2H2O}",
        r"\ce{H2SO4}",
    ])
    assert "\u21cc" in out, "denge oku (⇌) yok"
    assert "\u2082" in out and "\u2084" in out, "kimya alt simgeleri (₂, ₄) yok"
    assert "\u2192" in out, "kimyasal ok (→) yok"
    # \ce sarmalayicisi kalmamali
    assert "\\ce" not in out


def test_plain_arrow_outside_ce_is_not_converted():
    """'->' yalnizca \\ce icinde oka cevrilir; kod/duz metin bozulmaz."""
    out = run_convert(["sozluk a->b biciminde ve x <- y"])
    assert "a->b" in out
    assert "x <- y" in out
