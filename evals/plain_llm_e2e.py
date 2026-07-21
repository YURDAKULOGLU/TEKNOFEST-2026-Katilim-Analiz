"""Can a plain LLM replace the staged pipeline in one shot? An honest probe.

One prompt, one call per campaign text: "extract the full campaign record".
No rules, no staged validation, no retries. The output is scored against the
specification scenario table the staged pipeline extracts perfectly, and every
quote the model offers is checked for verbatim groundedness, because the
product's hard rule is that every fact carries an exact substring of the
source. The same plain treatment is applied to the 18-sentence fee-polarity
probe for comparison with the engineered schema-constrained baseline.

    python -m evals.plain_llm_e2e --model qwen3.5:9b --repeats 3

Results land in evals/results/plain-llm-e2e.json. Stages can be run separately
(--stage e2e | probe | all); the results file is merged, not overwritten.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RESULTS_PATH = Path(__file__).parent / "results" / "plain-llm-e2e.json"
PROBE_DATASET = Path(__file__).parent / "datasets" / "fee-polarity-tr-v0.1.jsonl"
PROBE_LABELS = ("charged", "waived", "mixed", "not_a_fee_statement")

#: The three specification-scenario campaign texts, verbatim (same wording the
#: staged pipeline is tested against in
#: backend/tests/unit/extraction/test_rules_and_candidate.py on
#: fix/1-vade-masraf-cikarimi). Kept here as data on purpose: this eval must
#: not import the pipeline it is auditing.
BANK_TEXTS = {
    "A": (
        "Konut Finansmanı\n"
        "Yeni ev sahibi olmak isteyen müşterilerimize özel %1,89 kâr payı oranı "
        "ile 120 aya kadar konut finansmanı fırsatı sunulmaktadır.\n"
        "Kampanya kapsamında 50.000 TL'ye kadar dosya masrafı alınmamaktadır.\n"
        "Kampanya 31 Aralık 2026 tarihine kadar geçerlidir."
    ),
    "B": (
        "Konut Finansmanı\n"
        "Konut finansmanında avantajlı ödeme seçenekleri. %1,95 kâr payı oranı "
        "ile 120 ay vadeye kadar finansman imkanı sunulmaktadır.\n"
        "Kampanya kapsamında ekspertiz ücreti banka tarafından karşılanmaktadır."
    ),
    "C": (
        "Konut Finansmanı\n"
        "Yeni konut alımlarına özel %1,87 kâr payı oranı ile 96 ay vadeli "
        "konut finansmanı fırsatı.\n"
        "Kampanya kapsamında 5.000 TL değerinde alışveriş çeki verilmektedir."
    ),
}

#: The comparison table the specification expects from those texts.
EXPECTED: dict[str, dict[str, Any]] = {
    "A": {
        "profit_rate": 1.89,
        "term_months": 120,
        "fee_status": "waived",
        "fee_waiver_ceiling": 50000,
        "reward_amount": None,
        "reward_type": None,
        "validity_date": "2026-12-31",
    },
    "B": {
        "profit_rate": 1.95,
        "term_months": 120,
        "fee_status": "waived",
        "fee_waiver_ceiling": None,
        "reward_amount": None,
        "reward_type": None,
        "validity_date": None,
    },
    "C": {
        "profit_rate": 1.87,
        "term_months": 96,
        "fee_status": "unknown",
        "fee_waiver_ceiling": None,
        "reward_amount": 5000,
        "reward_type": "voucher",
        "validity_date": None,
    },
}

FIELDS = tuple(EXPECTED["A"])


def _field(value_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "quote"],
        "properties": {
            "value": value_schema,
            "quote": {"type": ["string", "null"]},
        },
    }


E2E_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(FIELDS),
    "properties": {
        "profit_rate": _field({"type": ["number", "null"]}),
        "term_months": _field({"type": ["integer", "null"]}),
        "fee_status": _field(
            {"type": "string", "enum": ["charged", "waived", "unknown"]}
        ),
        "fee_waiver_ceiling": _field({"type": ["number", "null"]}),
        "reward_amount": _field({"type": ["number", "null"]}),
        "reward_type": _field(
            {"enum": ["voucher", "cashback", "points", "other", None]}
        ),
        "validity_date": _field({"type": ["string", "null"]}),
    },
}

#: Deliberately plain. The experiment is "düz LLM": one generic instruction,
#: no staged pipeline, no engineered guardrails.
E2E_PROMPT = (
    "Aşağıdaki Türkçe katılım bankası kampanya metninden tam kampanya kaydını "
    "çıkar. Alanlar: aylık kâr payı oranı (profit_rate, yüzde), vade "
    "(term_months, ay), masraf/ücret durumu (fee_status: charged, waived veya "
    "unknown), masraf muafiyeti üst limiti (fee_waiver_ceiling, TL), ödül "
    "tutarı (reward_amount, TL), ödül türü (reward_type), geçerlilik tarihi "
    "(validity_date, YYYY-MM-DD). Her alan için quote alanına kanıt olan "
    "cümleyi metinden birebir (harfi harfine aynı) kopyala. Metinde "
    "belirtilmeyen alanlar için value ve quote null olsun.\n\nMetin:\n{text}"
)

PROBE_PROMPT = (
    "Bu Türkçe banka kampanya cümlesi bir ücretin alınıp alınmadığı hakkında "
    "ne söylüyor? charged, waived, mixed veya not_a_fee_statement olarak "
    "cevapla.\n\nCümle: {sentence}"
)

PROBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label"],
    "properties": {"label": {"type": "string", "enum": list(PROBE_LABELS)}},
}


def call_model(
    base_url: str, model: str, prompt: str, schema: dict[str, Any], timeout: float
) -> tuple[dict[str, Any] | None, float, str | None]:
    """One shot, temperature 0, no retries. Failures are reported, not hidden."""
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "think": False,
            "keep_alive": 300,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": 4096},
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - operator-supplied local endpoint
        f"{base_url}/api/chat", data=body, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    try:
        # The endpoint is an operator-supplied local Ollama address, not user input.
        envelope = json.load(urllib.request.urlopen(request, timeout=timeout))  # noqa: S310
        parsed = json.loads(envelope["message"]["content"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
        return None, time.monotonic() - started, f"{type(error).__name__}: {error}"
    if not isinstance(parsed, dict):
        return None, time.monotonic() - started, "model returned non-object JSON"
    return parsed, time.monotonic() - started, None


def _values_match(expected: Any, got: Any) -> bool:
    if expected is None or got is None:
        return expected is got
    if isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        return abs(float(expected) - float(got)) < 1e-9
    return str(expected).strip() == str(got).strip()


def score_record(bank: str, record: dict[str, Any]) -> dict[str, Any]:
    source = BANK_TEXTS[bank]
    fields: dict[str, Any] = {}
    quotes_given = quotes_grounded = 0
    inversions = 0
    for name in FIELDS:
        expected = EXPECTED[bank][name]
        cell = record.get(name)
        got = cell.get("value") if isinstance(cell, dict) else None
        quote = cell.get("quote") if isinstance(cell, dict) else None
        if _values_match(expected, got):
            verdict = "correct"
        elif got is None:
            verdict = "missing"
        else:
            verdict = "wrong"
        if name == "fee_status" and {expected, got} == {"charged", "waived"}:
            inversions += 1
        if quote is not None:
            quotes_given += 1
            if quote in source:
                quotes_grounded += 1
        fields[name] = {
            "expected": expected,
            "got": got,
            "verdict": verdict,
            "quote": quote,
            "quote_grounded": None if quote is None else quote in source,
        }
    return {
        "fields": fields,
        "correct": sum(1 for f in fields.values() if f["verdict"] == "correct"),
        "wrong": sum(1 for f in fields.values() if f["verdict"] == "wrong"),
        "missing": sum(1 for f in fields.values() if f["verdict"] == "missing"),
        "quotes_given": quotes_given,
        "quotes_grounded": quotes_grounded,
        "polarity_inversions": inversions,
    }


def run_e2e(base_url: str, model: str, repeats: int, timeout: float) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for repeat in range(1, repeats + 1):
        banks: dict[str, Any] = {}
        for bank, text in BANK_TEXTS.items():
            record, seconds, error = call_model(
                base_url, model, E2E_PROMPT.format(text=text), E2E_SCHEMA, timeout
            )
            if record is None:
                banks[bank] = {"error": error, "seconds": round(seconds, 1)}
                print(f"  r{repeat} banka {bank}: HATA {error} ({seconds:.1f} sn)")
                continue
            scored = score_record(bank, record)
            scored["seconds"] = round(seconds, 1)
            banks[bank] = scored
            print(
                f"  r{repeat} banka {bank}: dogru {scored['correct']}/{len(FIELDS)}"
                f"  yanlis {scored['wrong']}  eksik {scored['missing']}"
                f"  alinti {scored['quotes_grounded']}/{scored['quotes_given']} birebir"
                f"  ters-kutup {scored['polarity_inversions']}  {seconds:.1f} sn"
            )
        scored_banks = [b for b in banks.values() if "fields" in b]
        runs.append(
            {
                "repeat": repeat,
                "banks": banks,
                "total_correct": sum(b["correct"] for b in scored_banks),
                "total_fields": len(FIELDS) * len(BANK_TEXTS),
                "quotes_given": sum(b["quotes_given"] for b in scored_banks),
                "quotes_grounded": sum(b["quotes_grounded"] for b in scored_banks),
                "polarity_inversions": sum(
                    b["polarity_inversions"] for b in scored_banks
                ),
                "seconds": round(sum(b["seconds"] for b in banks.values()), 1),
            }
        )
    return {"model": model, "repeats": repeats, "runs": runs}


def run_probe(base_url: str, model: str, timeout: float) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in PROBE_DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    examples = [row for row in rows if "id" in row]
    correct = answered = 0
    elapsed = 0.0
    misses: list[dict[str, str]] = []
    for example in examples:
        parsed, seconds, _error = call_model(
            base_url,
            model,
            PROBE_PROMPT.format(sentence=example["sentence"]),
            PROBE_SCHEMA,
            timeout,
        )
        elapsed += seconds
        label = parsed.get("label") if parsed else None
        if label not in PROBE_LABELS:
            continue
        answered += 1
        if label == example["label"]:
            correct += 1
        else:
            misses.append(
                {"id": example["id"], "expected": example["label"], "got": label}
            )
    print(
        f"  probe: dogru {correct}/{len(examples)}"
        f"  cevapsiz {len(examples) - answered}"
        f"  {elapsed / max(len(examples), 1):.1f} sn/ornek"
    )
    for miss in misses:
        print(f"    {miss['id']}: bekleniyordu={miss['expected']} verdi={miss['got']}")
    return {
        "model": model,
        "correct": correct,
        "answered": answered,
        "total": len(examples),
        "seconds_per_example": round(elapsed / max(len(examples), 1), 1),
        "misses": misses,
    }


def summarize(e2e: dict[str, Any]) -> None:
    runs = e2e["runs"]
    if not runs:
        return
    worst = min(runs, key=lambda r: r["total_correct"])
    best = max(runs, key=lambda r: r["total_correct"])
    print(
        f"\ne2e ozet ({e2e['model']}, {len(runs)} tekrar):"
        f" en kotu {worst['total_correct']}/{worst['total_fields']}"
        f" (r{worst['repeat']}), en iyi {best['total_correct']}/{best['total_fields']}"
        f" (r{best['repeat']})"
    )
    given = sum(r["quotes_given"] for r in runs)
    grounded = sum(r["quotes_grounded"] for r in runs)
    print(
        f"  alinti birebir: {grounded}/{given} (%{100 * grounded / given:.0f})"
        if given
        else "  alinti verilmedi"
    )
    print(
        f"  ters-kutup (charged<->waived): {sum(r['polarity_inversions'] for r in runs)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--stage", choices=("e2e", "probe", "all"), default="all")
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    arguments = parser.parse_args()

    payload: dict[str, Any] = {"schema_version": "1.0"}
    if arguments.out.exists():
        payload = json.loads(arguments.out.read_text(encoding="utf-8"))
    if arguments.stage in ("e2e", "all"):
        print(
            f"e2e: {arguments.model}, {arguments.repeats} tekrar, tek atis, retry yok"
        )
        e2e = run_e2e(
            arguments.base_url, arguments.model, arguments.repeats, arguments.timeout
        )
        summarize(e2e)
        payload["e2e"] = e2e
    if arguments.stage in ("probe", "all"):
        print(f"\nprobe: {PROBE_DATASET.name}, duz tek prompt")
        payload["probe"] = run_probe(
            arguments.base_url, arguments.model, arguments.timeout
        )
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nyazildi: {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
