"""The app's markdown renderer (static/markdown.js), run under Node with the vendored marked and KaTeX."""

import json
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

STATIC = Path(str(files("llm_hub.web").joinpath("static")))
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node isn't installed")

RUNNER = """
const create = require(process.argv[1] + "/markdown.js");
const m = require(process.argv[1] + "/vendor/marked.min.js");
const katex = require(process.argv[1] + "/vendor/katex.min.js");
const md = create({ marked: m.marked || m, katex, sanitize: (html) => html });
const inputs = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(inputs.map((t) => md.render(t))));
"""


def render(*texts: str) -> list[str]:
    out = subprocess.run([NODE, "-e", RUNNER, str(STATIC)], input=json.dumps(texts), capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_inline_math_keeps_underscores_and_stars():
    [a, b] = render(r"The gap \(\delta_h^A\) and \(\delta_h^c\).", r"Here $\delta^*=0.872877$ holds.")
    assert a.count("<math") == 2 and "<em>" not in a and r"\(" not in a
    assert "<math" in b and "<em>" not in b and "0.872877" in b


def test_display_math():
    [a, b] = render("Let\n\\[\nu_{tt}-\\Delta u=0\n\\]\nhold.", "$$\\int_0^1 x\\,dx$$")
    assert 'class="math-display"' in a and 'display="block"' in a
    assert 'class="math-display"' in b


def test_prices_and_escaped_dollars_stay_text():
    [a, b] = render("It costs $5 and $10.", r"Write \$x\$ for a literal.")
    assert "<math" not in a and "$5 and $10" in a
    assert "<math" not in b


def test_code_is_left_alone():
    [a, b] = render("Inline `C^1` and `$x$` stay code.", "```python\nprice = '$a$'\n```")
    assert "<code>C^1</code>" in a and "<code>$x$</code>" in a and "<math" not in a
    assert "<math" not in b and "$a$" in b


def test_markdown_outside_math_still_works():
    [a] = render("Use _this_ and **that** with $x_1$ and a [link](https://example.com).")
    assert "<em>this</em>" in a and "<strong>that</strong>" in a and "<math" in a and 'href="https://example.com"' in a


def test_a_post_wrapped_in_a_markdown_fence_is_unwrapped():
    [a, b] = render("```markdown\n## Plan\n\n- one\n```", "```python\nx = 1\n```")
    assert "<h2>Plan</h2>" in a and "<li>one</li>" in a
    assert "<pre><code" in b  # other languages stay code


def test_invalid_tex_shows_its_source():
    [a] = render(r"Broken \(\frac{1}{\) here.")
    assert 'class="math-source"' in a and r"\frac{1}{" in a


def test_tables_with_math():
    [a] = render("| mesh | $\\delta$ |\n|---|---|\n| 12x2x2 | 0.869115 |")
    assert "<table>" in a and "<math" in a


def test_letter_styles_become_unicode_math_letters():
    # Chrome ignores mathvariant, so \\mathbb R must arrive as ℝ itself.
    [a] = render(r"For \(F\subset\mathbb R^{1+n}\) and \(\mathcal D_K\), \(\mathfrak g\), \(\mathbf{x}\).")
    assert "ℝ" in a and "𝒟" in a and "𝔤" in a and "𝐱" in a
    assert 'mathvariant="double-struck"' not in a
