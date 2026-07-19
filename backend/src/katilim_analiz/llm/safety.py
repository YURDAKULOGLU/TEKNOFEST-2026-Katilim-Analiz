"""Deterministic screening for obvious indirect prompt-injection content."""

from __future__ import annotations

import re

_INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?\b", re.I),
    re.compile(
        r"\b(?:onceki|önceki)\s+(?:(?:tum|tüm)\s+)?"
        r"(?:talimatlari|talimatları|yonergeleri|yönergeleri)\s+"
        r"(?:yoksay|yok\s+say|unut|gormezden\s+gel|görmezden\s+gel)",
        re.I,
    ),
    re.compile(
        r"\b(?:sistem|system)\s+(?:mesajini|mesajını|promptunu)\s+"
        r"(?:yoksay|yok\s+say|gormezden\s+gel|görmezden\s+gel)",
        re.I,
    ),
    re.compile(r"\b(?:system|developer|assistant)\s*(?:prompt|message|mesaji|mesajı)\b", re.I),
    re.compile(r"\b(?:return|output|çıktı|cikti)\s+(?:only\s+)?json\b", re.I),
    re.compile(r"\b(?:tool|function)\s*(?:call|çağrısı|cagrisi)\b", re.I),
    re.compile(r"\b(?:execute|run|çalıştır|calistir)\s+(?:this\s+)?(?:sql|shell|command)", re.I),
    re.compile(r"\bcurl\s+https?://", re.I),
    re.compile(r"(?:/etc/passwd|api[_ -]?key|secret[_ -]?key|parolayi?\s+goster)", re.I),
    re.compile(
        r"\b(?:yönetici|yonetici|admin|sistem|system)[^\n]{0,48}"
        r"(?:parola|şifre|sifre|api[_ -]?key|secret[_ -]?key)[^\n]{0,80}"
        r"(?:cevap|yanıt|yanit|başlık|baslik|çıktı|cikti|output)[^\n]{0,48}"
        r"(?:ekle|yaz|göster|goster|döndür|dondur|paylaş|paylas)",
        re.I,
    ),
    re.compile(
        r"\b(?:kaynakta|kanıtta|kanitta)\s+"
        r"(?:yazmasa|olmasa|bulunmasa)\s+da\b",
        re.I,
    ),
    re.compile(
        r"\b(?:sahte|uydur(?:ulmuş|ulmus)?)\b[^\n]{0,48}"
        r"(?:citation(?:_id)?|atıf|atif|kaynak)[^\n]{0,48}"
        r"(?:üret|uret|ekle|yaz|döndür|dondur)",
        re.I,
    ),
)


def is_obvious_prompt_injection(text: str) -> bool:
    """Flag explicit instruction-takeover markers without claiming full detection."""

    return any(pattern.search(text) is not None for pattern in _INJECTION_PATTERNS)
