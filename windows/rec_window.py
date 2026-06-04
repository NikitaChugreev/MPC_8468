import logging

from PyQt5 import QtCore, QtGui, QtWidgets

from windows.word_window import WordWindow
from windows.key_window import KeyWindow

from recipes.recipes import recipes, save_recipes
from config.settings import settings
from utils.translator import Translator

if settings.get('NUMBER_GASES') == 3:
    from ui.ui_ser.ui_3 import Ui_RecWindow
elif settings.get('NUMBER_GASES') == 2:
    from ui.ui_ser.ui_2 import Ui_RecWindow

number_gases = settings.get('NUMBER_GASES')

class RecWindow(QtWidgets.QMainWindow, Ui_RecWindow):
    def __init__(self, parent=None):
        super(RecWindow, self).__init__(parent)
        self.setupUi(self)
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowTitleHint)
        self.setWindowTitle('GN')
        self.showFullScreen()

        self.map_num_gas = {
            '0': 'Air',
            '1': 'Ar',
            '2': 'O2',
            '3': 'N2',
            '4': 'Custom gas',
        }

        self.map_gas_num = {
            'Air': 0,
            'Ar': 1,
            'O2': 2,
            'N2': 3,
            'Custom gas': 4,
        }

        self.data_for_copy = None
        self.translator = Translator()

        self.buttons = [getattr(self, f"Button{i}") for i in range(1, 51)]
        self.ButtonClose.clicked.connect(self.close)

        self.ScrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.VerticalScrollBar.setMaximum(self.ScrollArea.verticalScrollBar().maximum())
        self.VerticalScrollBar.valueChanged.connect(self.changeScrollArea)
        self._apply_scrollbar_style()

        self.init_title_buttons()
        self.init_first_recipe()

        for btn in self.buttons:
            btn.clicked.connect(self.open_recipe)

        self.TitleButton.clicked.connect(self.open_word)
        self.ComButton.clicked.connect(self.open_word)
        self.ButtonCheck.clicked.connect(self.check)
        self.ButtonClear.clicked.connect(self.clear)
        self.ButtonSave.clicked.connect(self.copy_recipe)

        if self.parent().user_mode == 'Operator':
            self.change_for_operator()
        else:
            self.change_for_technologist()

        self.update_ui_texts()

    def update_ui_texts(self):
        # Локализованные названия газов (включая «Свой газ» = index 4)
        self.map_num_gas = {
            '0': self.translator.tr('air'),
            '1': self.translator.tr('argon'),
            '2': self.translator.tr('oxigen'),
            '3': self.translator.tr('nitrogen'),
            '4': self.translator.tr('custom_gas'),
        }
        self.map_gas_num = {v: int(k) for k, v in self.map_num_gas.items()}
        self.LabelText_3.hide()
        self.LabelText_3.setText(self.translator.tr('recipes'))
        self.LabelText.setText(self.translator.tr('recipe'))
        self.TitleButton.setText(self.translator.tr('name'))
        self.ComButton.setText(self.translator.tr('comment'))
        self.ButtonCheck.setText(self.translator.tr('select'))
        self.ButtonCheck.setIcon(QtGui.QIcon('ui/Pictures13/Select.png'))
        self.LabelText_11.setText(self.translator.tr('base_pressure_2'))
        self.LabelText_5.setText(self.translator.tr('gas_1'))
        self.LabelText_6.setText(self.translator.tr('gas_2'))
        self.LabelText_8.setText(self.translator.tr('power'))
        self.LabelProf_16.setText(self.translator.tr('pressure_unit'))
        self.LabelText_9.setText(self.translator.tr('flow_unit'))
        self.LabelText_10.setText(self.translator.tr('flow_unit'))
        self.LabelText_12.setText(self.translator.tr('power_unit'))
        self.ButtonClear.setText(self.translator.tr('clear'))
        self.ButtonClear.setIcon(QtGui.QIcon('ui/Pictures13/Clean.png'))
        self.ButtonSave.setText(self.translator.tr('copy_parameters'))
        self.ButtonSave.setIcon(QtGui.QIcon('ui/Pictures13/Save.png'))
        self.ButtonClose.setText(self.translator.tr('close'))
        self.ButtonClose.setIcon(QtGui.QIcon('ui/Pictures13/Close.png'))

    def _apply_scrollbar_style(self):
        """Стильный вертикальный скроллбар: трек + ручка с градиентом и hover."""
        self.VerticalScrollBar.setStyleSheet("""
            QScrollBar:vertical {
                border: none;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #e8e8e8, stop:1 #f0f0f0);
                width: 18px;
                margin: 4px 2px;
                border-radius: 9px;
            }
            QScrollBar::handle:vertical {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #5a9fd4, stop:0.5 #4a8fc4, stop:1 #3a7fb4);
                border-radius: 9px;
                min-height: 48px;
                margin: 2px 1px;
            }
            QScrollBar::handle:vertical:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #6aafe4, stop:0.5 #5a9fd4, stop:1 #4a8fc4);
            }
            QScrollBar::handle:vertical:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #4a8fc4, stop:0.5 #3a7fb4, stop:1 #2a6fa4);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
                background: none;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
        """)

    def init_first_recipe(self):
        recipe = recipes['1']
        
        self.RecNumber.setText('1')
        self.TitleLine.setText(recipe['title'])
        self.ComText.setPlainText(recipe['com'])
        self.PressLine.setText(str(recipe['ResPressure']))
        self.PowerLine.setText(str(recipe['power']))
        self.TimeLine.setText(recipe['time'])

        for i in range(1, number_gases + 1):
            getattr(self, f'VE{i}GasLine').setText(self.map_num_gas.get(str(recipe[f'VE{i}']['gas'])))
            getattr(self, f'VE{i}FlowLine').setText(str(recipe[f'VE{i}']['flow']))
        

    def change_for_technologist(self):
        self.ButtonSave.show()
        self.ButtonClear.show()
        self.TitleButton.setEnabled(True)
        self.ComButton.setEnabled(True)
        self.TitleLine.setReadOnly(False)
        self.ComText.setReadOnly(False)
        self.PressLine.setReadOnly(False)
        self.PowerLine.setReadOnly(False)
        self.TimeLine.setReadOnly(False)

        for i in range(1, number_gases + 1):
            getattr(self, f'VE{i}GasLine').setReadOnly(False)
            getattr(self, f'VE{i}FlowLine').setReadOnly(False)
        

    def change_for_operator(self):
        self.ButtonSave.hide()
        self.ButtonClear.hide()
        self.TitleButton.setEnabled(False)
        self.ComButton.setEnabled(False)
        self.TitleLine.setReadOnly(True)
        self.ComText.setReadOnly(True)
        self.PressLine.setReadOnly(True)
        self.PowerLine.setReadOnly(True)
        self.TimeLine.setReadOnly(True)

        for i in range(1, number_gases + 1):
            getattr(self, f'VE{i}GasLine').setReadOnly(True)
            getattr(self, f'VE{i}FlowLine').setReadOnly(True)
        

    def init_title_buttons(self):
        for i, btn in enumerate(self.buttons):
            btn.setText(btn.text() + ' ' + recipes[str(i+1)]['title'])

    def open_recipe(self):
        btn_name = self.sender().objectName()
        if not btn_name.startswith('Button'):
            logging.error(f"Unexpected button name: {btn_name}")
            return
        num_recipe = int(btn_name.replace('Button', ''))

        style_on = 'text-align: left; background-color: rgb(120, 220, 220); border: 1px solid rgb(120, 120, 120); border-radius: 2px;'
        style_off = 'text-align: left; background-color: rgb(220, 220, 220); border: 1px solid rgb(120, 120, 120); border-radius: 2px;'
        
        for btn in self.buttons:
            btn.setStyleSheet(style_off)
            
        getattr(self, f"Button{str(num_recipe)}").setStyleSheet(style_on)

        self.RecNumber.setText(str(num_recipe))

        recipe = recipes[str(num_recipe)]
        
        self.TitleLine.setText(recipe['title'])
        self.ComText.setPlainText(recipe['com'])
        self.PressLine.setText(str(recipe['ResPressure']))
        self.PowerLine.setText(str(recipe['power']))
        self.TimeLine.setText(recipe['time'])

        for i in range(1, number_gases + 1):
            getattr(self, f'VE{i}GasLine').setText(self.map_num_gas.get(str(recipe[f'VE{i}']['gas'])))
            getattr(self, f'VE{i}FlowLine').setText(str(recipe[f'VE{i}']['flow']))

    def check(self):
        if self.data_for_copy is not None:
            recipes[str(self.RecNumber.text())] = self.data_for_copy
            save_recipes(recipes)

        recipes[str(self.RecNumber.text())]['title'] = self.TitleLine.text()
        recipes[str(self.RecNumber.text())]['com'] = self.ComText.toPlainText()
        try:
            recipes[str(self.RecNumber.text())]['ResPressure'] = float(self.PressLine.text())
        except ValueError:
            recipes[str(self.RecNumber.text())]['ResPressure'] = 0.1
        try:
            recipes[str(self.RecNumber.text())]['power'] = int(self.PowerLine.text())
        except ValueError:
            recipes[str(self.RecNumber.text())]['power'] = 0    
        recipes[str(self.RecNumber.text())]['time'] = self.TimeLine.text()

        for i in range(1, number_gases + 1):
            recipes[str(self.RecNumber.text())][f'VE{i}']['gas'] = self.map_gas_num.get(getattr(self, f'VE{i}GasLine').text())
            recipes[str(self.RecNumber.text())][f'VE{i}']['flow'] = float(getattr(self, f'VE{i}FlowLine').text())
        
        save_recipes(recipes)
        
        self.parent().update_recipe(int(self.RecNumber.text()))
        self.close()
    
    def changeScrollArea(self):
        self.ScrollArea.verticalScrollBar().setValue(self.VerticalScrollBar.value())

    def open_word(self):
        word = WordWindow(parent=self, label_sender=self.sender().text(), recipe_number=self.RecNumber.text())
        word.show()

    def open_key(self):
        key = KeyWindow(parent=self)
        key.show()

    def clear(self):
        getattr(self, f"Button{self.RecNumber.text()}").setText(self.RecNumber.text() + '.')
        self.TitleLine.setText('')
        self.ComText.setPlainText('')
        self.PressLine.setText('0.0')
        self.PowerLine.setText('0')
        self.TimeLine.setText('00:00')

        for i in range(1, number_gases + 1):
            getattr(self, f'VE{i}GasLine').setText('')
            getattr(self, f'VE{i}FlowLine').setText('0.0')
        

    def copy_recipe(self):
        try:
            if self.parent() is not None:
                self.data_for_copy = self.parent().get_current_recipe()
            
            if self.data_for_copy is None:
                logging.error("copy_recipe: get_current_recipe returned None")
                return
            
            def safe_get(data, *keys, default=''):
                try:
                    result = data
                    for key in keys:
                        if isinstance(result, dict) and key in result:
                            result = result[key]
                        else:
                            return default
                    return result
                except (KeyError, TypeError, AttributeError):
                    return default
            
            res_pressure = safe_get(self.data_for_copy, 'ResPressure', default=0.0)
            self.PressLine.setText(str(res_pressure))

            self.PowerLine.setText(str(safe_get(self.data_for_copy, 'power', default=0)))
            self.TimeLine.setText(str(safe_get(self.data_for_copy, 'time', default='00:00')))

            for i in range(1, number_gases + 1):
                ve_i_gas = safe_get(self.data_for_copy, f'VE{i}', 'gas', default=None)
                ve_i_gas_str = str(ve_i_gas) if ve_i_gas is not None and ve_i_gas != -1 else '0'
                getattr(self, f'VE{i}GasLine').setText(self.map_num_gas.get(ve_i_gas_str, self.translator.tr('air')))
                getattr(self, f'VE{i}FlowLine').setText(str(safe_get(self.data_for_copy, f'VE{i}', 'flow', default=0.0)))
        
        except Exception as e:
            logging.error(f"Error in copy_recipe: {e}", exc_info=True)

            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Ошибка", f"Ошибка при копировании рецепта: {e}")


