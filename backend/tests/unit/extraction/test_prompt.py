from __future__ import annotations

import json

from _factories import make_document

from katilim_analiz.extraction.rules import extract_rules
from katilim_analiz.llm import ModelFactField
from katilim_analiz.llm.prompt import (
    MAX_USER_PROMPT_BYTES,
    build_prompt_package,
    build_system_prompt,
    select_model_fields,
)


def test_prompt_budget_counts_serialized_utf8_bytes_not_characters() -> None:
    hostile_to_naive_character_limits = 'İ\\"🧿' * 2_000
    document = make_document(
        ("heading", hostile_to_naive_character_limits),
        ("paragraph", "Geçerlilik ve müşteri koşulu kaynakta açıkça yer alır." * 100),
    )

    package = build_prompt_package(document, frozenset(ModelFactField))
    payload = json.loads(package.user_content)

    assert len(package.user_content.encode("utf-8")) <= MAX_USER_PROMPT_BYTES
    assert payload["blocks"]
    for visible in payload["blocks"]:
        source = next(block for block in document.blocks if block.id == visible["block_id"])
        assert "visible_start_char" not in visible
        assert "visible_end_char" not in visible
        assert visible["text"] in source.text


def test_prompt_budget_rejects_metadata_that_cannot_fit() -> None:
    document = make_document(("heading", "Kampanya"))

    try:
        build_prompt_package(
            document,
            frozenset({ModelFactField.TITLE}),
            max_user_bytes=10,
        )
    except ValueError as error:
        assert "request metadata" in str(error)
    else:  # pragma: no cover - explicit fail message is clearer than a bare assertion
        raise AssertionError("undersized prompt budget was accepted")


def test_model_field_selection_chooses_one_high_signal_family_and_caps_fields() -> None:
    document = make_document(
        ("heading", "Genel Bilgi"),
        ("paragraph", "Aylık kâr payı oranı %1,89 ve finansman tutarı 50.000 TL'dir."),
        ("paragraph", "Kampanya yeni müşteriler için geçerlidir."),
    )

    selected = select_model_fields(
        document,
        frozenset(
            {
                ModelFactField.RATE,
                ModelFactField.FINANCING_AMOUNT,
                ModelFactField.TERM,
                ModelFactField.CUSTOMER_SEGMENT,
                ModelFactField.NEW_CUSTOMER_ONLY,
            }
        ),
    )

    assert selected == frozenset(
        {
            ModelFactField.RATE,
            ModelFactField.FINANCING_AMOUNT,
        }
    )


def test_model_field_selection_skips_unsupported_no_signal_field() -> None:
    document = make_document(("paragraph", "Ürün hakkında genel açıklama."))

    assert select_model_fields(document, frozenset({ModelFactField.REWARD})) == frozenset()


def test_prompt_prioritizes_field_relevant_block_over_generic_heading() -> None:
    document = make_document(
        ("heading", "Genel Bilgilendirme"),
        ("paragraph", "Yıllık kâr payı oranı %24,50 olarak açıklanmıştır."),
    )

    package = build_prompt_package(
        document,
        frozenset({ModelFactField.RATE}),
        max_blocks=1,
    )
    payload = json.loads(package.user_content)

    assert payload["blocks"][0]["block_id"] == document.blocks[1].id


def test_term_selector_rejects_installment_count_even_with_bare_vade_word() -> None:
    document = make_document(
        ("heading", "Eğitim Kampanyası"),
        ("paragraph", "Ödemelerde vade farksız 3 taksit fırsatı sunulur."),
    )

    assert select_model_fields(document, frozenset({ModelFactField.TERM})) == frozenset()


def test_term_selector_accepts_only_representable_month_year_duration_signal() -> None:
    document = make_document(
        ("heading", "Eğitim Kampanyası"),
        ("paragraph", "Ödemelerde 3 gün erteleme sonrasında 12 ay vade uygulanır."),
    )

    assert select_model_fields(document, extract_rules(document).unresolved_fields) == frozenset(
        {ModelFactField.TERM}
    )


def test_selected_field_guide_is_trusted_system_content_not_untrusted_user_json() -> None:
    fields = frozenset({ModelFactField.TERM})
    document = make_document(("paragraph", "Finansman için 12 ay vade uygulanır."))

    system_prompt = build_system_prompt(fields)
    user_payload = json.loads(build_prompt_package(document, fields).user_content)

    assert "taksit adedi, gün/hafta ve erteleme değil" in system_prompt
    assert "field_guides" not in user_payload


def test_signal_centered_window_keeps_candidate_after_first_thousand_characters() -> None:
    late_candidate = "12 ay vade"
    document = make_document(
        ("paragraph", f"{'x' * 1_400} {late_candidate} uygulanır."),
    )

    package = build_prompt_package(
        document,
        frozenset({ModelFactField.TERM}),
        max_blocks=1,
        max_block_chars=300,
    )
    payload = json.loads(package.user_content)

    assert late_candidate in payload["blocks"][0]["text"]
