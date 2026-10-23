"""Подбор ключевых слов с учётом склонений (lead_filters.match_keywords)."""
import pytest

from lead_filters import match_keywords, normal_form, parse_keywords


@pytest.mark.parametrize("text", [
    "Нужен хороший фотограф на выходные",          # именительный
    "Ищу фотографа для семейной съёмки",          # родительный, винительный
    "Договорились с фотографом о цене",           # творительный
    "Фотографу нужно заранее знать место",        # дательный
    "Подскажите контакты фотографов",             # множественное число
])
def test_inflected_forms_match_single_word(text):
    assert match_keywords(text, ["фотограф"]) == ["фотограф"]


def test_unrelated_text_does_not_match():
    assert match_keywords("Всем привет, как дела?", ["фотограф", "репетитор"]) == []


def test_case_is_ignored():
    assert match_keywords("ФОТОГРАФ НУЖЕН СРОЧНО", ["Фотограф"]) == ["Фотограф"]


def test_yo_and_ye_are_the_same_letter():
    assert match_keywords("Сколько стоит фотосъемка?", ["фотосъёмка"]) == ["фотосъёмка"]
    assert match_keywords("Сколько стоит фотосъёмка?", ["фотосъемка"]) == ["фотосъемка"]


def test_phrase_needs_all_words():
    assert match_keywords("Срочно нужен фотограф на мероприятие", ["нужен фотограф"]) == ["нужен фотограф"]
    assert match_keywords("Нужен переводчик", ["нужен фотограф"]) == []
    assert match_keywords("Фотограф уехал", ["нужен фотограф"]) == []


def test_phrase_order_and_distance_do_not_matter():
    assert match_keywords("Фотограф в субботу очень нужен", ["нужен фотограф"]) == ["нужен фотограф"]


def test_words_inside_phrase_are_inflected_too():
    # «ищу фотографа» по ключу «ищу фотограф»; «ищем» и «фотографов» дают те же начальные формы
    assert match_keywords("Мы ищем фотографов", ["ищу фотограф"]) == ["ищу фотограф"]


def test_latin_words_are_compared_as_is_ignoring_case():
    assert match_keywords("Need Logo Design for my startup", ["logo design"]) == ["logo design"]
    assert match_keywords("Need logo designs for my startup", ["logo design"]) == []


def test_digits_and_symbols_in_keyword():
    assert match_keywords("Куплю iPhone 15 недорого", ["iphone 15"]) == ["iphone 15"]
    assert match_keywords("Куплю iPhone 14 недорого", ["iphone 15"]) == []


def test_returns_keywords_as_given_and_in_order():
    keywords = ["Репетитор", "фотограф", "нужен фотограф", "сантехник"]
    text = "Нужен фотограф или репетитор по математике"
    assert match_keywords(text, keywords) == ["Репетитор", "фотограф", "нужен фотограф"]


@pytest.mark.parametrize("text, keywords", [
    ("", ["фотограф"]),                  # пустое сообщение
    ("Нужен фотограф", []),              # нет ключей
    ("Нужен фотограф", ["", "  ", "!"]),  # в ключах нет слов
])
def test_empty_inputs_give_no_matches(text, keywords):
    assert match_keywords(text, keywords) == []


def test_punctuation_in_message_does_not_break_tokens():
    assert match_keywords("Фотограф!!! Нужен, срочно...", ["нужен фотограф"]) == ["нужен фотограф"]


def test_parse_keywords_splits_lowercases_and_trims():
    raw = "Фотограф, Репетитор;ремонт квартиры\n  Нужен мастер  "
    assert parse_keywords(raw) == ["фотограф", "репетитор", "ремонт квартиры", "нужен мастер"]


def test_parse_keywords_drops_empty_parts():
    assert parse_keywords(" , ;; \n") == []
    assert parse_keywords("") == []


def test_normal_form_gives_dictionary_form():
    assert normal_form("фотографа") == "фотограф"
    assert normal_form("репетитор") == "репетитор"
