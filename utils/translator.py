from PyQt5.QtCore import QObject, pyqtSignal
from config.settings import settings
import json


class _LanguageEmitter(QObject):
    language_changed = pyqtSignal()


language_emitter = _LanguageEmitter()

class Translator:
    def __init__(self, locale_file="locales/common.json"):
        self.locale_file = locale_file
        lang_index = settings.get('LANG', 0)  # 0 = ru, 1 = en
        self.lang_index = lang_index
        self.translations = {}
        self.load_translations()

    def load_translations(self):
        try:
            with open(self.locale_file, "r", encoding="utf-8") as f:
                self.translations = json.load(f)
        except FileNotFoundError:
            print(f"⚠️ Translation file not found: {self.locale_file}")
            self.translations = {}

    def set_language(self, lang_index):
        self.lang_index = lang_index
        language_emitter.language_changed.emit()

    def tr(self, key):
        value = self.translations.get(key)
        if value is None:
            return key
        
        if isinstance(value, list) and len(value) > self.lang_index:
            return value[self.lang_index]

        return key
    

translator = Translator()