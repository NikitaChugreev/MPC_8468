from PyQt5 import QtCore, QtWidgets

from config.settings import settings
from recipes.recipes import recipes, save_recipes

if settings.get('NUMBER_GASES') == 3:
    from ui.ui_ser.ui_3 import Ui_WordWindow
elif settings.get('NUMBER_GASES') == 2:
    from ui.ui_ser.ui_2 import Ui_WordWindow

class WordWindow(QtWidgets.QMainWindow, Ui_WordWindow):
    def __init__(self, parent=None, label_sender=None, recipe_number=None):
        super(WordWindow, self).__init__(parent)

        self.recipe_number = recipe_number
        self.label_sender = label_sender
        self.max_length = 100 if self.label_sender in ['Комментарий', 'Comment'] else 30

        self.setupUi(self)
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowTitleHint)
        self.setWindowTitle('GN')
        self.showFullScreen()

        self.init_labels()

        self.ButtonCancel.clicked.connect(self.close)
        self.ButtonClear.clicked.connect(self.clear)
        self.ButtonSave.clicked.connect(self.save)
        self.ButtonBackspace.clicked.connect(self.backspace)
        self.Button4_2.clicked.connect(self.language)
        self.Button4_4.clicked.connect(self.language)

        for i in range(4):
            for j in range(15):
                if hasattr(self, f"Button{i}_{j}"):
                    getattr(self, f"Button{i}_{j}").clicked.connect(self.input_symbol)

        self.Button4_1.clicked.connect(self.input_symbol)
        self.Button4_3.clicked.connect(self.input_symbol)
        self.Button4_5.clicked.connect(self.input_symbol)

        self.Button4_3.setText(' ')

    def input_symbol(self):
        if len(self.ComText.toPlainText()) < self.max_length:
            self.ComText.setPlainText(self.ComText.toPlainText() + self.sender().text())
            self.SymbolNumber.setText(str(self.max_length - len(self.ComText.toPlainText())))
        
    def clear(self):
        self.ComText.setPlainText('')
        self.SymbolNumber.setText(str(self.max_length))

    def language(self):
        layouts = {
            (True, True): {
                'row1': ['Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P', '(', ')'],
                'row2': ['A', 'S', 'D', 'F', 'G', 'H', 'J', 'K', 'L', ':', '"'],
                'row3': ['', 'Z', 'X', 'C', 'V', 'B', 'N', 'M', '<', '>']
            },
            (True, False): {
                'row1': ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p', '[', ']'],
                'row2': ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l', ';', '"'],
                'row3': ['', 'z', 'x', 'c', 'v', 'b', 'n', 'm', '<', '>']
            },
            (False, True): {
                'row1': ['Й', 'Ц', 'У', 'К', 'Е', 'Н', 'Г', 'Ш', 'Щ', 'З', 'Х', 'Ъ'],
                'row2': ['Ф', 'Ы', 'В', 'А', 'П', 'Р', 'О', 'Л', 'Д', 'Ж', 'Э'],
                'row3': ['', 'Я', 'Ч', 'С', 'М', 'И', 'Т', 'Ь', 'Б', 'Ю']
            },
            (False, False): {
                'row1': ['й', 'ц', 'у', 'к', 'е', 'н', 'г', 'ш', 'щ', 'з', 'х', 'ъ'],
                'row2': ['ф', 'ы', 'в', 'а', 'п', 'р', 'о', 'л', 'д', 'ж', 'э'],
                'row3': ['', 'я', 'ч', 'с', 'м', 'и', 'т', 'ь', 'б', 'ю']
            }
        }

        key = (self.Button4_2.isChecked(), self.Button4_4.isChecked())
        layout = layouts.get(key)

        if layout:
            for i, text in enumerate(layout['row1'], 1):
                getattr(self, f"Button1_{i}").setText(text)
            
            for i, text in enumerate(layout['row2'], 1):
                getattr(self, f"Button2_{i}").setText(text)
            
            for i, text in enumerate(layout['row3'], 2):
                if text:
                    getattr(self, f"Button3_{i}").setText(text)

    def save(self):
        if self.label_sender in ['Название', 'Name']:
            title = self.ComText.toPlainText()
            self.parent().TitleLine.setText(title)
            recipes[str(self.recipe_number)]['title'] = title

            getattr(self.parent(), f"Button{self.recipe_number}").setText(str(self.recipe_number) + '. ' + title)
        elif self.label_sender in ['Комментарий', 'Comment']:
            com = self.ComText.toPlainText()
            self.parent().ComText.setPlainText(com)
            recipes[str(self.recipe_number)]['com'] = com

        save_recipes(recipes)
        self.close()

    def backspace(self):
        if int(len(self.SymbolNumber.text())) < self.max_length:
            self.ComText.setPlainText(self.ComText.toPlainText()[:-1])
            self.SymbolNumber.setText(str(self.max_length - len(self.ComText.toPlainText())))

    def init_labels(self):
        self.RecNumber.setText(self.recipe_number)
        self.TitleLabel.setText(self.label_sender)

        if self.label_sender in ['Название', 'Name']:
            self.ComText.setPlainText(recipes[str(self.recipe_number)]['title'])
        elif self.label_sender in ['Комментарий', 'Comment']:
            self.ComText.setPlainText(recipes[str(self.recipe_number)]['com'])

        self.SymbolNumber.setText(str(self.max_length - len(self.ComText.toPlainText())))

