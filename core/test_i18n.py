"""Тесты i18n: проверка что переводы загружаются и применяются."""

from django.test import TestCase
from django.utils import translation


class I18nTests(TestCase):
    def test_ru_is_default_language(self):
        with translation.override("ru"):
            self.assertEqual(translation.gettext("Начало"), "Начало")

    def test_en_translation_loads(self):
        with translation.override("en"):
            # Если .mo скомпилирован и загрузился — получим английский
            translated = translation.gettext("Начало")
            self.assertEqual(translated, "Home")

    def test_en_translation_for_key_terms(self):
        with translation.override("en"):
            self.assertEqual(translation.gettext("Финансирование"), "Funding")
            self.assertEqual(translation.gettext("Кейсы финансирования"), "Funding Cases")
            self.assertEqual(translation.gettext("План-факт БДДС"), "Plan-Fact (Cash Flow)")
            self.assertEqual(translation.gettext("БДР"), "Profit & Loss")
            self.assertEqual(translation.gettext("CFO дашборд"), "CFO Dashboard")
            self.assertEqual(translation.gettext("Курсы валют"), "Exchange Rates")
            self.assertEqual(translation.gettext("Высокий риск"), "High Risk")
