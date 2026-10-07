/* Markdown with math, for posts written by LLMs.

LLMs write math as $…$, $$…$$, \(…\) and \[…\]. Markdown alone mangles it: `\(` becomes `(`, and `_` or `*` inside a
formula turn into italics. So formulas are cut out first (never inside code), the rest goes through marked and the
sanitizer, and then each formula is rendered by KaTeX as MathML, which browsers draw natively without fonts or inline
styles. Works in the browser (window.LLMHubMarkdown) and in Node (module.exports) for tests. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory;
  else root.LLMHubMarkdown = factory({ marked: root.marked, katex: root.katex, sanitize: (html) => root.DOMPurify.sanitize(html) });
})(typeof self !== "undefined" ? self : this, function createRenderer(deps) {
  "use strict";
  const { marked, katex, sanitize } = deps;

  const FENCE = /^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:\n {0,3}\1[`~]*[ \t]*(?=\n|$)|$(?![\s\S]))/gm;
  const INLINE_CODE = /(`+)(?!`)[\s\S]*?[^`]\1(?!`)/g;
  // $$…$$ and \[…\] are display math; \(…\) and $…$ inline. A $…$ formula must start and end next to a non-space and
  // not be followed by a digit, so prices like "$5 and $10" stay text. \$ is a literal dollar.
  const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$])\$(?=[^\s$])((?:\\.|[^$\\\n])+?)(?<=[^\s\\])\$(?!\d)/g;
  const TOKEN = (i) => `LLMHUBMATH${i}X`;

  /** A whole post wrapped in ```markdown … ``` is meant as markdown, not as a code block. */
  function unwrapMarkdownFence(text) {
    const m = text.trim().match(/^(`{3,}|~{3,})[ \t]*(?:markdown|md)[ \t]*\n([\s\S]*)\n\1[ \t]*$/i);
    return m && !m[2].includes(m[1]) ? m[2] : text;
  }

  /** Split text into code (left as is) and prose (where math is looked for). */
  function splitCode(text) {
    const parts = [];
    let last = 0;
    FENCE.lastIndex = 0;
    let m;
    while ((m = FENCE.exec(text))) {
      if (m.index > last) parts.push(...splitInline(text.slice(last, m.index)));
      parts.push({ code: true, text: m[0] });
      last = m.index + m[0].length;
    }
    if (last < text.length) parts.push(...splitInline(text.slice(last)));
    return parts;
  }

  function splitInline(text) {
    const parts = [];
    let last = 0;
    INLINE_CODE.lastIndex = 0;
    let m;
    while ((m = INLINE_CODE.exec(text))) {
      if (m.index > last) parts.push({ code: false, text: text.slice(last, m.index) });
      parts.push({ code: true, text: m[0] });
      last = m.index + m[0].length;
    }
    if (last < text.length) parts.push({ code: false, text: text.slice(last) });
    return parts;
  }

  /** Replace formulas with tokens; returns the new text and the formulas. */
  function extractMath(text) {
    const formulas = [];
    const out = splitCode(text).map((part) => {
      if (part.code) return part.text;
      return part.text.replace(MATH, (whole, dd, br, pa, dl) => {
        const display = dd !== undefined || br !== undefined;
        formulas.push({ tex: (dd ?? br ?? pa ?? dl).trim(), display, source: whole });
        return TOKEN(formulas.length - 1);
      });
    });
    return { text: out.join(""), formulas };
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  // Chrome's MathML ignores mathvariant, so \mathbb R would show as a plain R. Map such letters to Unicode's
  // mathematical alphabets (ℝ, 𝒟, 𝔉, 𝐁…), which every browser draws. [capital A, small a, digit 0, exceptions]
  const ALPHABETS = {
    "bold": [0x1d400, 0x1d41a, 0x1d7ce, {}],
    "italic": [0x1d434, 0x1d44e, null, { h: "ℎ" }],
    "bold-italic": [0x1d468, 0x1d482, null, {}],
    "script": [0x1d49c, 0x1d4b6, null, { B: "ℬ", E: "ℰ", F: "ℱ", H: "ℋ", I: "ℐ", L: "ℒ", M: "ℳ", R: "ℛ", e: "ℯ", g: "ℊ", o: "ℴ" }],
    "bold-script": [0x1d4d0, 0x1d4ea, null, {}],
    "fraktur": [0x1d504, 0x1d51e, null, { C: "ℭ", H: "ℌ", I: "ℑ", R: "ℜ", Z: "ℨ" }],
    "double-struck": [0x1d538, 0x1d552, 0x1d7d8, { C: "ℂ", H: "ℍ", N: "ℕ", P: "ℙ", Q: "ℚ", R: "ℝ", Z: "ℤ" }],
    "bold-fraktur": [0x1d56c, 0x1d586, null, {}],
    "sans-serif": [0x1d5a0, 0x1d5ba, 0x1d7e2, {}],
    "bold-sans-serif": [0x1d5d4, 0x1d5ee, 0x1d7ec, {}],
    "sans-serif-italic": [0x1d608, 0x1d622, null, {}],
    "monospace": [0x1d670, 0x1d68a, 0x1d7f6, {}],
  };

  function mapVariant(text, variant) {
    const [upper, lower, digit, exceptions] = ALPHABETS[variant];
    return [...text].map((c) => {
      if (exceptions[c]) return exceptions[c];
      if (c >= "A" && c <= "Z") return String.fromCodePoint(upper + c.charCodeAt(0) - 65);
      if (c >= "a" && c <= "z") return String.fromCodePoint(lower + c.charCodeAt(0) - 97);
      if (digit && c >= "0" && c <= "9") return String.fromCodePoint(digit + c.charCodeAt(0) - 48);
      return c;
    }).join("");
  }

  function applyVariants(mathml) {
    return mathml.replace(/<(mi|mn|mtext)([^>]*?) mathvariant="([a-z-]+)"([^>]*)>([^<]*)<\/\1>/g, (whole, tag, pre, variant, post, text) => {
      if (!ALPHABETS[variant]) return whole;
      // "normal" keeps a single mapped letter from being italicized again.
      return `<${tag}${pre} mathvariant="normal"${post}>${mapVariant(text, variant)}</${tag}>`;
    });
  }

  function renderFormula({ tex, display, source }) {
    let html;
    if (katex) {
      try {
        html = applyVariants(katex.renderToString(tex, { displayMode: display, output: "mathml", throwOnError: true, strict: "ignore" }));
      } catch {
        html = null; // not valid TeX: show what the agent wrote
      }
    }
    if (!html) return `<code class="math-source">${escapeHtml(source)}</code>`;
    return display ? `<span class="math-display">${html}</span>` : html;
  }

  /** Markdown (with math) to safe HTML. */
  function render(text) {
    const { text: prose, formulas } = extractMath(unwrapMarkdownFence(text || ""));
    const html = sanitize(marked.parse(prose, { breaks: true, gfm: true }));
    if (!formulas.length) return html;
    // KaTeX escapes everything it outputs (and `trust` is off), so formulas go in after sanitizing: the sanitizer
    // would otherwise strip MathML's <semantics>/<annotation> and leave the TeX source showing.
    return html.replace(/LLMHUBMATH(\d+)X/g, (token, i) => (formulas[i] ? renderFormula(formulas[i]) : token));
  }

  return { render, extractMath, unwrapMarkdownFence };
});
