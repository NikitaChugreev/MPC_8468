from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtCore import Qt

from windows.key_window import KeyWindow

from config.settings import settings, save_settings
from utils.translator import translator, language_emitter


number_gases = settings.get('NUMBER_GASES', 2)
if number_gases == 3:
    ui_dir = 'ui/ui_ser/ui_3/'
    from ui.ui_ser.ui_3.setwindow import Ui_SetWindow
elif number_gases == 2:
    ui_dir = 'ui/ui_ser/ui_2/'
    from ui.ui_ser.ui_2.setwindow import Ui_SetWindow

class SetWindow(QtWidgets.QMainWindow, Ui_SetWindow):
    def __init__(self, parent=None):
        super(SetWindow, self).__init__(parent)
        self.setupUi(self)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowTitleHint)
        self.setWindowTitle('GN')
        self.showFullScreen()

        self.translator = translator
        language_emitter.language_changed.connect(self.update_ui_texts)

        self.ButtonPass.setText(self.translator.tr('yes') if settings['use_pass_technologist'] else self.translator.tr('no'))
        self.ButtonPass.setChecked(bool(settings['use_pass_technologist']))
        self.ClockVE0.setText(str(settings['time_venting']))
        self.ClockNI.setText(str(settings['time_pump']))
        self.TimePump.setText(str(int(settings['time_pump_for_service']) // 3600))
        self.TimePumpSer.setText(str(int(settings['max_time_pump_for_service']) // 3600))
        self.TimeMPC.setText(str(int(settings['time_work']) // 3600))

        number_gases = settings.get("NUMBER_GASES")
        for i in range(1, number_gases + 1):
            getattr(self, f'userInputRrgConvCoeffPlace_{i}').setText(str(settings[f'coef_rrg{i}']))

        self.comboBoxLang.setCurrentIndex(settings['LANG'])
        self.ButtonBuzzer.setText(self.translator.tr('yes') if settings.get('enable_sound', True) else self.translator.tr('no'))
        self.ButtonBuzzer.setChecked(bool(settings.get('enable_sound', True)))

        self.ButtonPass.clicked.connect(self.set_use_pass)
        self.ButtonBuzzer.clicked.connect(self.set_enable_sound)
        self.ButtonSave.clicked.connect(self.save)
        self.ButtonCancel.clicked.connect(self.close)
        self.ButtonNIRes.clicked.connect(self.reset_time_pump)

        self.ClockVE0.clicked.connect(lambda: self.open_key(sender='ClockVE0'))
        self.ClockNI.clicked.connect(lambda: self.open_key(sender='ClockNI'))
        for i in range(1, number_gases + 1):
            getattr(self, f'userInputRrgConvCoeffPlace_{i}').clicked.connect(
                lambda checked, sender=f'coef_rrg{i}': self.open_key(sender=sender)
            )

        self.old_lang = self.comboBoxLang.currentIndex()

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
        self.ButtonSave.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Save.png'))
        self.ButtonCancel.setText(self.translator.tr('cancel'))
        self.ButtonCancel.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Cancel.png'))
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
        try:
            new_lang = self.comboBoxLang.currentIndex()

            settings.update({
                'use_pass_technologist': True if self.ButtonPass.text() == self.translator.tr('yes') else False,
                'time_venting': int(self.ClockVE0.text()),
                'time_pump': int(self.ClockNI.text()),
                'LANG': self.comboBoxLang.currentIndex(),
                'enable_sound': True if self.ButtonBuzzer.text() == self.translator.tr('yes') else False
            })

            number_gases = settings.get("NUMBER_GASES")
            for i in range(1, number_gases + 1):
                    settings.update({f'coef_rrg{i}': float(getattr(self, f'userInputRrgConvCoeffPlace_{i}').text())})
            save_settings(settings)
            
            if new_lang != self.old_lang:
                translator.set_language(new_lang)  # обновляет lang_index и уведомляет все окна


            self.close()

        except ValueError:
            msg = QMessageBox()
    
            msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
            msg.setStyleSheet("""
                            QMessageBox {
                                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                                    stop:0 rgb(255, 255, 200), stop:1 rgb(255, 255, 150));
                                border: 3px solid rgb(255, 200, 0);
                                border-radius: 15px;
                                padding: 20px;
                            }
                            QLabel {
                                font: 80 28pt "Lato Semibold"; 
                                color: rgb(0, 3, 51);
                                background-color: transparent;
                            }
                            QPushButton {
                            font: 80 18pt "Lato Semibold"; 
                                min-width: 100px;
                                min-height: 40px;
                                padding: 8px;
                            }
                            """)

            msg.setIcon(QMessageBox.Warning)
            msg.setText(self.translator.tr('warning'))
            msg.setInformativeText(self.translator.tr("error_invalid_data"))
            msg.setDefaultButton(None)
            ok_button = msg.button(QMessageBox.Ok)
            if ok_button:
                ok_button.setFocusPolicy(Qt.NoFocus)
                ok_button.clearFocus()
                msg.setFocus()
            msg.show()
            msg.activateWindow()
            msg.raise_()
            msg.exec()