import json
import os

from PyQt5 import QtCore, QtGui, QtWidgets
from ui.setwindow import Ui_SetWindow
from windows.key_window import KeyWindow

from config.settings import settings, save_settings
from utils.translator import Translator


class SetWindow(QtWidgets.QMainWindow, Ui_SetWindow):
    def __init__(self, parent=None):
        super(SetWindow, self).__init__(parent)
        self.setupUi(self)
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowTitleHint)
        self.setWindowTitle('GN')
        self.showFullScreen()

        self.translator = Translator()

        self.ButtonPass.setText(self.translator.tr('yes') if settings['use_pass_technologist'] else self.translator.tr('no'))
        self.ButtonPass.setChecked(bool(settings['use_pass_technologist']))
        self.ClockVE0.setText(str(settings['time_venting']))
        self.ClockNI.setText(str(settings['time_pump']))
        self.TimePump.setText(str(int(settings['time_pump_for_service']) // 3600))
        self.TimePumpSer.setText(str(int(settings['max_time_pump_for_service']) // 3600))
        self.userInputRrgConvCoeffPlace_1.setText(str(settings['coef_rrg1']))
        self.userInputRrgConvCoeffPlace_2.setText(str(settings['coef_rrg2']))
        self.comboBoxLang.setCurrentIndex(settings['LANG'])
        # Инициализация кнопки бузера из настроек
        self.ButtonBuzzer.setText(self.translator.tr('yes') if settings.get('enable_sound', True) else self.translator.tr('no'))
        self.ButtonBuzzer.setChecked(bool(settings.get('enable_sound', True)))

        self.ButtonPass.clicked.connect(self.set_use_pass)
        self.ButtonBuzzer.clicked.connect(self.set_enable_sound)
        self.ButtonSave.clicked.connect(self.save)
        self.ButtonCancel.clicked.connect(self.close)
        self.ButtonNIRes.clicked.connect(self.reset_time_pump)

        self.ClockVE0.clicked.connect(lambda: self.open_key(sender='ClockVE0'))
        self.ClockNI.clicked.connect(lambda: self.open_key(sender='ClockNI'))
        self.userInputRrgConvCoeffPlace_1.clicked.connect(lambda: self.open_key(sender='coef_rrg1'))
        self.userInputRrgConvCoeffPlace_2.clicked.connect(lambda: self.open_key(sender='coef_rrg2'))

        self.update_ui_texts()

    def reset_time_pump(self):
        self.TimePump.setText('0')
        settings.update({'time_pump_for_service': 0})
        save_settings(settings)

    def update_ui_texts(self):
        self.LabelText.setText(self.translator.tr('settings'))
        self.LabelSet_4.setText(self.translator.tr('use_password_for_technologist'))
        self.LabelSet_2.setText(self.translator.tr('chamber_vent_time'))
        self.LabelSet_3.setText(self.translator.tr('max_pump_down_time'))
        self.LabelSet_19.setText(self.translator.tr('mfc_coefficient_1'))
        self.LabelSet_23.setText(self.translator.tr('mfc_coefficient_2'))
        self.LabelSet_13.setText(self.translator.tr('system_operating_time'))
        self.LabelSet_14.setText(self.translator.tr('pump_operating_time'))
        self.LabelSet_18.setText(self.translator.tr('reset_pump_operating_time'))
        self.ButtonNIRes.setText(self.translator.tr('reset'))
        self.LabelSet_16.setText(self.translator.tr('pump_maintenance_reminder'))
        self.LabelSet_9.setText(self.translator.tr('enable_sound'))
        self.LabelSet_12.setText(self.translator.tr('hours'))
        self.LabelSet_15.setText(self.translator.tr('hours'))
        self.LabelSet_17.setText(self.translator.tr('hours'))
        self.LabelSet_21.setText(self.translator.tr('select_language'))
        self.ButtonSave.setText(self.translator.tr('save'))
        self.ButtonSave.setIcon(QtGui.QIcon('ui/Pictures13/Save.png'))
        self.ButtonCancel.setText(self.translator.tr('cancel'))
        self.ButtonCancel.setIcon(QtGui.QIcon('ui/Pictures13/Cancel.png'))
        self.LabelSet_6.setText(self.translator.tr('sec'))
        self.LabelSet_5.setText(self.translator.tr('min'))
        # ButtonBuzzer инициализируется из настроек в __init__, не перезаписываем здесь


    def open_key(self, sender=None):
        key_window = KeyWindow(self, sender=sender)
        key_window.show()

    def set_use_pass(self):
        self.ButtonPass.setText(self.translator.tr('yes') if self.ButtonPass.text() == self.translator.tr('no') else self.translator.tr('no'))
        self.ButtonPass.setChecked(self.ButtonPass.text() == self.translator.tr('yes'))

    def set_enable_sound(self):
        self.ButtonBuzzer.setText(self.translator.tr('yes') if self.ButtonBuzzer.text() == self.translator.tr('no') else self.translator.tr('no'))
        self.ButtonBuzzer.setChecked(self.ButtonBuzzer.text() == self.translator.tr('yes'))
    
    def save(self):
        settings.update({
            'use_pass_technologist': True if self.ButtonPass.text() == self.translator.tr('yes') else False,
            'time_venting': int(self.ClockVE0.text()),
            'time_pump': int(self.ClockNI.text()),
            'coef_rrg1': float(self.userInputRrgConvCoeffPlace_1.text()),
            'coef_rrg2': float(self.userInputRrgConvCoeffPlace_2.text()),
            'LANG': self.comboBoxLang.currentIndex(),
            'enable_sound': True if self.ButtonBuzzer.text() == self.translator.tr('yes') else False
        })
        
        save_settings(settings)
        
        self.close()