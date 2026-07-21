"""Zero-shot encoder span extraction pilot on the Turkish fee-polarity probe.

The product publishes nothing it cannot quote, so an extractor that returns
character spans is grounded by construction: a span either is a substring of
the source or it does not exist. This pilot asks whether a zero-shot
multilingual GLiNER model can even locate the relevant spans in Turkish
finance text on CPU, before any fine-tuning is considered. Labels are
natural-language prompts to the model, so their phrasing is part of what is
being measured.

The probe reuses the fee-polarity dataset: sentences labelled charged, waived
or mixed must yield at least one fee-flavoured span (hit), and the
not_a_fee_statement traps must not (false positive). Span classification is
not scored; only whether the model finds the material the runtime would need.

    uv run --no-project --managed-python --python 3.13 --with gliner \
        python evals/encoder_span_pilot.py --out evals/results/encoder-span-pilot.json

GLiNER and torch are deliberately not project dependencies; run through an
isolated environment as above (managed Python avoids a system torchvision
leaking into the ephemeral environment). Nothing is written unless --out is
given.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASET = Path(__file__).parent / "datasets" / "fee-polarity-tr-v0.1.jsonl"
DEFAULT_MODEL = "urchade/gliner_multi-v2.1"

# Prompt-labels the model matches against. Fee and fee-waiver phrasings carry
# the polarity distinction the product cares about; the rest cover the other
# fact families so the pilot also shows what a full extractor would see.
DEFAULT_LABELS = (
    "ücret",
    "ücretsiz işlem",
    "kâr payı oranı",
    "vade",
    "ödül",
    "para tutarı",
)

# Spans with these labels count as fee-flavoured for hit and trap scoring.
FEE_LABELS = frozenset({"ücret", "ücretsiz işlem", "ücret muafiyeti", "masraf", "komisyon"})

# Gold labels whose sentences assert something about a fee being (not) charged.
FEE_ASSERTING = frozenset({"charged", "waived", "mixed"})


@dataclass(frozen=True, slots=True)
class Example:
    id: str
    sentence: str
    label: str
    note: str


@dataclass(frozen=True, slots=True)
class Span:
    text: str
    label: str
    score: float
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class SentenceOutcome:
    example: Example
    spans: tuple[Span, ...]
    verbatim_violations: int
    seconds: float

    @property
    def found_any(self) -> bool:
        return bool(self.spans)

    @property
    def found_fee(self) -> bool:
        return any(span.label in FEE_LABELS for span in self.spans)


def load_examples(path: Path = DATASET) -> tuple[Example, ...]:
    examples: list[Example] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "id" not in row:  # header comment row
            continue
        examples.append(Example(row["id"], row["sentence"], row["label"], row.get("note", "")))
    return tuple(examples)


def to_span(sentence: str, entity: dict[str, Any]) -> tuple[Span, bool]:
    """Convert one GLiNER entity, reporting whether it is verbatim in the source.

    GLiNER spans should be exact substrings by construction; the check exists
    to prove that claim on our data rather than assume it.
    """
    span = Span(
        text=str(entity["text"]),
        label=str(entity["label"]),
        score=round(float(entity["score"]), 4),
        start=int(entity["start"]),
        end=int(entity["end"]),
    )
    verbatim = sentence[span.start : span.end] == span.text and span.text in sentence
    return span, verbatim


def evaluate(
    model: Any,  # gliner.GLiNER, not imported at module scope
    examples: tuple[Example, ...],
    labels: tuple[str, ...],
    threshold: float,
) -> tuple[SentenceOutcome, ...]:
    outcomes: list[SentenceOutcome] = []
    for example in examples:
        started = time.monotonic()
        entities = model.predict_entities(example.sentence, list(labels), threshold=threshold)
        seconds = time.monotonic() - started
        spans: list[Span] = []
        violations = 0
        for entity in entities:
            span, verbatim = to_span(example.sentence, entity)
            spans.append(span)
            if not verbatim:
                violations += 1
        outcomes.append(SentenceOutcome(example, tuple(spans), violations, seconds))
    return tuple(outcomes)


def summarise(outcomes: tuple[SentenceOutcome, ...]) -> dict[str, Any]:
    fee_cases = [o for o in outcomes if o.example.label in FEE_ASSERTING]
    traps = [o for o in outcomes if o.example.label == "not_a_fee_statement"]
    fee_hits = sum(1 for o in fee_cases if o.found_fee)
    trap_false_positives = sum(1 for o in traps if o.found_fee)
    latencies = [o.seconds for o in outcomes]
    return {
        "sentences": len(outcomes),
        "with_any_span": sum(1 for o in outcomes if o.found_any),
        "verbatim_violations": sum(o.verbatim_violations for o in outcomes),
        "fee_cases": len(fee_cases),
        "fee_hits": fee_hits,
        "fee_hit_rate": round(fee_hits / len(fee_cases), 4) if fee_cases else 0.0,
        "traps": len(traps),
        "trap_false_positives": trap_false_positives,
        "trap_fp_rate": round(trap_false_positives / len(traps), 4) if traps else 0.0,
        "mean_seconds": round(statistics.fmean(latencies), 3) if latencies else 0.0,
        "median_seconds": round(statistics.median(latencies), 3) if latencies else 0.0,
    }


def print_report(outcomes: tuple[SentenceOutcome, ...], show_spans: bool) -> None:
    for outcome in outcomes:
        example = outcome.example
        if example.label in FEE_ASSERTING:
            verdict = "isabet" if outcome.found_fee else "KACTI"
        else:
            verdict = "YANLIS-ALARM" if outcome.found_fee else "temiz"
        print(
            f"  {example.id}  {example.label:20} span {len(outcome.spans):2}"
            f"  {verdict:12} {outcome.seconds:5.2f} sn"
        )
        if show_spans:
            for span in outcome.spans:
                print(f'      {span.label:16} {span.score:.2f}  "{span.text}"')

    summary = summarise(outcomes)
    print(
        f"\n  herhangi bir span: {summary['with_any_span']}/{summary['sentences']}"
        f"\n  birebir alinti ihlali: {summary['verbatim_violations']}"
        f"\n  ucret isabeti (charged/waived/mixed):"
        f" {summary['fee_hits']}/{summary['fee_cases']}"
        f" (%{100 * summary['fee_hit_rate']:.0f})"
        f"\n  tuzakta yanlis alarm: {summary['trap_false_positives']}/{summary['traps']}"
        f" (%{100 * summary['trap_fp_rate']:.0f})"
        f"\n  gecikme: ortalama {summary['mean_seconds']:.2f} sn,"
        f" medyan {summary['median_seconds']:.2f} sn"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="GLiNER model id on Hugging Face")
    parser.add_argument(
        "--labels",
        default=",".join(DEFAULT_LABELS),
        help="comma-separated natural-language span labels",
    )
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--show-spans", action="store_true")
    arguments = parser.parse_args()

    labels = tuple(label.strip() for label in arguments.labels.split(",") if label.strip())
    examples = load_examples()
    print(f"probe: {DATASET.name}  ornek: {len(examples)}")
    print(f"model: {arguments.model}  esik: {arguments.threshold}")
    print(f"etiketler: {', '.join(labels)}")

    # Imported late so --help and argument errors do not require gliner installed.
    from gliner import GLiNER

    loading_started = time.monotonic()
    model = GLiNER.from_pretrained(arguments.model)
    print(f"model yuklendi: {time.monotonic() - loading_started:.1f} sn\n")

    outcomes = evaluate(model, examples, labels, arguments.threshold)
    print_report(outcomes, arguments.show_spans)

    if arguments.out is not None:
        payload = {
            "schema_version": "1.0",
            "probe": DATASET.name,
            "model": arguments.model,
            "labels": list(labels),
            "threshold": arguments.threshold,
            "summary": summarise(outcomes),
            "sentences": [
                {
                    "id": outcome.example.id,
                    "label": outcome.example.label,
                    "found_any_span": outcome.found_any,
                    "found_fee_span": outcome.found_fee,
                    "verbatim_violations": outcome.verbatim_violations,
                    "seconds": round(outcome.seconds, 3),
                    "spans": [
                        {
                            "text": span.text,
                            "label": span.label,
                            "score": span.score,
                            "start": span.start,
                            "end": span.end,
                        }
                        for span in outcome.spans
                    ],
                }
                for outcome in outcomes
            ],
        }
        arguments.out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nyazildi: {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
