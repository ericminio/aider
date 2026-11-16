import shutil

import pytest

from benchmark.tdd import probe_prime_factors


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for the live-eval oracle")
@pytest.mark.parametrize(
    "source,expected",
    [
        ("export const primeFactors = n => '9 = 3*3';", False),
        (
            (
                "export function primeFactors(n) { const original = n; const factors = []; for (let"
                " d = 2; d * d <= n; d++) { while (n % d === 0) { factors.push(d); n /= d; } } if"
                " (n > 1) factors.push(n); return `${original} = ${factors.join('*') || '1'}`; }"
            ),
            True,
        ),
        ("export const primeFactors = n => { while (true) {} };", False),
    ],
)
def test_live_evaluation_oracle_detects_overfitting_and_nontermination(tmp_path, source, expected):
    (tmp_path / "prime.js").write_text(source)
    (tmp_path / "package.json").write_text('{"type":"module"}')
    result = probe_prime_factors(tmp_path, timeout=1)
    assert result["passed"] is expected
    assert result["cases"] >= 10
    if not expected:
        assert result["output"]
