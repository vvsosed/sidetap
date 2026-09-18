from sidetap.cost import Rates


def test_translation_is_priced_per_million_characters():
    rates = Rates(mt_per_million_chars=20.0)
    assert rates.translation_usd(1_000_000) == 20.0
    assert rates.translation_usd(500) == 0.01


def test_synthesis_is_priced_per_million_characters():
    rates = Rates(tts_per_million_chars=30.0)
    assert rates.synthesis_usd(1_000_000) == 30.0


def test_recognition_is_priced_per_minute():
    rates = Rates(stt_per_minute=0.016)
    assert rates.recognition_usd(60.0) == 0.016
    assert rates.recognition_usd(30.0) == 0.008


def test_zero_is_free():
    rates = Rates()
    assert rates.translation_usd(0) == 0.0
    assert rates.recognition_usd(0.0) == 0.0
