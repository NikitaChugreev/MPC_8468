from PyQt5 import QtCore, QtWidgets
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtCore import Qt

from config.settings import settings, save_settings

if settings.get('NUMBER_GASES') == 3:
    from ui.ui_ser.ui_3 import Ui_KeyWindow
elif settings.get('NUMBER_GASES') == 2:
    from ui.ui_ser.ui_2 import Ui_KeyWindow

from utils.translator import Translator

class KeyWindow(QtWidgets.QMainWindow, Ui_KeyWindow):
    def __init__(self, parent=None, sender=None, recipe=False, recipe_number=None):
        super(KeyWindow, self).__init__(parent)
        self.setupUi(self)
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowTitleHint)
        self.setWindowTitle('GN')
        self.showFullScreen()

        self.label_sender = sender

        self.translator = Translator()

        self.ButtonPoint_1.hide()
        self.ButtonPoint_2.hide()
        self.update_ui_texts()

        if self.label_sender == 'PressLine':
            self.LabelTextPar.setText(self.translator.tr('base_pressure'))
            self.LabelTextLim.setText('')

        if self.label_sender == 'ClockVE0':
            self.LabelTextPar.setText(self.translator.tr('chamber_vent_time'))
            self.LabelTextLim.setText(self.translator.tr('vent_time_range'))
            self.NumberLine.setText(str(settings['time_venting']))
            self.max_limit = 1000
            self.min_limit = 3
            self.message = self.translator.tr('error_vent_time_range')

        elif self.label_sender == 'ClockNI':
            self.LabelTextPar.setText(self.translator.tr('pump_down_time_range_text'))
            self.LabelTextLim.setText(self.translator.tr('pump_down_time_range'))
            self.NumberLine.setText(str(settings['time_pump']))
            self.max_limit = 59
            self.min_limit = 2
            self.message = self.translator.tr('error_pump_down_time_range')

        elif self.label_sender in ['coef_rrg1', 'coef_rrg2', 'coef_rrg3', 'coef_rrg4']:
            self.LabelTextPar.setText(self.translator.tr('coefficient'))
            self.LabelTextLim.setText(self.translator.tr('coefficient_range'))
            self.max_limit = 10.00
            self.min_limit = 0.01
            self.message = self.translator.tr('error_coefficient_range')
            self.NumberLine.setText(str(settings[self.label_sender]))

        elif self.label_sender == 'PressZad':
            self.NumberLine.setText(getattr(self.parent(), self.label_sender).text())
            self.LabelTextPar.setText(self.translator.tr('base_pressure'))
            self.LabelTextLim.setText(self.translator.tr('base_pressure_range') + str(settings.get('ResPressure')) + " " + self.translator.tr('pressure_unit'))
            self.max_limit = 10
            self.min_limit = settings.get('ResPressure')
            self.message = self.translator.tr('error_pressure_range') + str(settings.get('ResPressure')) + " " + self.translator.tr('pressure_unit')

        elif self.label_sender in ['VE1FlowZad', 'VE2FlowZad', 'VE3FlowZad', 'VE4FlowZad']:
            if getattr(self.parent(), self.label_sender).text() != '0.0':
                self.NumberLine.setText(getattr(self.parent(), self.label_sender).text())
            else:
                self.NumberLine.setText('')
            self.LabelTextLim.setText(self.translator.tr('gas_flow_max'))
            self.LabelTextPar.setText(self.translator.tr('gas_flow') + ' ' + self.label_sender[2])
            self.max_limit = 30
            self.min_limit = 0.5
            self.message = self.translator.tr('error_gas_flow_max')

        elif self.label_sender == 'TimeZad':
            self.ButtonPoint.hide()
            self.ButtonPoint_1.show()
            self.ButtonPoint_2.show()
            self.NumberLine.setText(getattr(self.parent(), self.label_sender).text())
            self.LabelTextPar.setText(self.translator.tr('process_time'))
            self.LabelTextLim.setText(self.translator.tr('minutes_seconds'))

        elif self.label_sender == 'HFPowerZad':
            if getattr(self.parent(), self.label_sender).text() == '0':
                self.NumberLine.setText('')
            else:
                self.NumberLine.setText(getattr(self.parent(), self.label_sender).text())

            self.LabelTextPar.setText(self.translator.tr('plasma_power'))
            self.LabelTextLim.setText(self.translator.tr('plasma_power_range') + str(settings.get('MAX_POWER_BP')) + " " + self.translator.tr('power_unit'))
            self.max_limit = settings.get('MAX_POWER_BP')
            self.min_limit = settings.get('MIN_POWER_BP', 10)
            self.message = self.translator.tr('error_plasma_power_range' + str(settings.get('MAX_POWER_BP')) + " " + self.translator.tr('power_unit'))

        for button in [self.Button0, self.Button1, self.Button2, self.Button3, self.Button4,
                       self.Button5, self.Button6, self.Button7, self.Button8, self.Button9]:
            button.clicked.connect(self.input_number)

        self.ButtonPoint.clicked.connect(lambda: self.button_point(button='ButtonPoint'))
        self.ButtonPoint_1.clicked.connect(lambda: self.button_point(button='ButtonPoint_1'))
        self.ButtonPoint_2.clicked.connect(lambda: self.button_point(button='ButtonPoint_2'))

        self.ButtonBackspace.clicked.connect(lambda: self.NumberLine.setText(self.NumberLine.text()[:-1]))
        self.ButtonCancel.clicked.connect(self.close)
        self.ButtonClear.clicked.connect(lambda: self.NumberLine.setText(''))
        self.ButtonCheck.clicked.connect(self.check)

    def update_ui_texts(self):
        self.ButtonBackspace.setText(self.translator.tr('delete'))
        self.ButtonCancel.setText(self.translator.tr('cancel'))
        self.ButtonClear.setText(self.translator.tr('clear'))
        self.ButtonCheck.setText(self.translator.tr('select'))

    def button_point(self, button):
        if button == 'ButtonPoint' and not str.endswith(self.NumberLine.text(), '.') and len(self.NumberLine.text()) != 0 and '.' not in self.NumberLine.text():
                self.NumberLine.setText(self.NumberLine.text() + '.')

        elif button == 'ButtonPoint_1':
            if len(self.NumberLine.text()) == 0:
                self.NumberLine.setText(self.NumberLine.text() + '00:')

        elif button == 'ButtonPoint_2':
            if len(self.NumberLine.text()) == 3:
                self.NumberLine.setText(self.NumberLine.text() + '00')
            elif len(self.NumberLine.text()) == 2:
                self.NumberLine.setText(self.NumberLine.text() + ':00')
            elif len(self.NumberLine.text()) == 1:
                self.NumberLine.setText('0' + self.NumberLine.text() + ':00')
    
    def check(self):
        is_valid = True

        if self.label_sender in ['TimeZad']:
            if self.NumberLine.text() != '' and self.NumberLine.text()[-1] == ':':
                getattr(self.parent(), self.label_sender).setText(self.NumberLine.text() + '00')
            elif len(self.NumberLine.text()) == 1:
                getattr(self.parent(), self.label_sender).setText('0' + self.NumberLine.text() + ':00')
            elif len(self.NumberLine.text()) == 2:
                getattr(self.parent(), self.label_sender).setText(self.NumberLine.text() + ':00')
            elif len(self.NumberLine.text()) == 4 and self.NumberLine.text().startswith('00:'):
                getattr(self.parent(), self.label_sender).setText('00:0' + self.NumberLine.text()[3])
            else:
                getattr(self.parent(), self.label_sender).setText(self.NumberLine.text())
        
        elif self.NumberLine.text() != '':
            if self.min_limit <= float(self.NumberLine.text()) <= self.max_limit:
                if self.label_sender in ['ClockVE0', 'ClockNI', 'PressZad', 'HFPowerZad', 'VE1FlowZad', 'VE2FlowZad', 'VE3FlowZad', 'VE4FlowZad']:
                    if self.label_sender in ['VE1FlowZad', 'VE2FlowZad', 'VE3FlowZad', 'VE4FlowZad']:
                        getattr(self.parent(), self.label_sender).setText(str(float(self.NumberLine.text())))
                    elif self.label_sender == 'HFPowerZad':
                        getattr(self.parent(), self.label_sender).setText(str(int(float(self.NumberLine.text()))))
                    else:
                        getattr(self.parent(), self.label_sender).setText(self.NumberLine.text())
                elif self.label_sender == 'coef_rrg1':
                    self.parent().userInputRrgConvCoeffPlace_1.setText(self.NumberLine.text())
                elif self.label_sender == 'coef_rrg2':
                    self.parent().userInputRrgConvCoeffPlace_2.setText(self.NumberLine.text())
                elif self.label_sender == 'coef_rrg3':
                    self.parent().userInputRrgConvCoeffPlace_3.setText(self.NumberLine.text())
                elif self.label_sender == 'coef_rrg4':
                    self.parent().userInputRrgConvCoeffPlace_4.setText(self.NumberLine.text())
            else:
                is_valid = False

        if self.NumberLine.text() == '':
            if self.label_sender in ['VE1FlowZad', 'VE2FlowZad', 'VE3FlowZad', 'VE4FlowZad', 'PressZad']:
                getattr(self.parent(), self.label_sender).setText('0.0')
            elif self.label_sender == 'coef_rrg1':
                getattr(self.parent(), 'userInputRrgConvCoeffPlace_1').setText('1.0')
            elif self.label_sender == 'coef_rrg2':
                getattr(self.parent(), 'userInputRrgConvCoeffPlace_2').setText('1.0')
            elif self.label_sender == 'coef_rrg3':
                getattr(self.parent(), 'userInputRrgConvCoeffPlace_3').setText('1.0')
            elif self.label_sender == 'coef_rrg4':
                getattr(self.parent(), 'userInputRrgConvCoeffPlace_4').setText('1.0')
            elif self.label_sender == 'TimeZad':
                getattr(self.parent(), self.label_sender).setText('01:00')
            elif self.label_sender == 'HFPowerZad':
                getattr(self.parent(), self.label_sender).setText('0')

        if not is_valid:
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
            msg.setInformativeText(self.translator.tr("enter_valid_value"))
            # Убираем фокус с кнопки ОК для сенсорного экрана
            msg.setDefaultButton(None)
            # Находим кнопку ОК и убираем с неё фокус
            ok_button = msg.button(QMessageBox.Ok)
            if ok_button:
                ok_button.setFocusPolicy(QtCore.Qt.NoFocus)
                ok_button.clearFocus()
                # Устанавливаем фокус на само окно вместо кнопки
                msg.setFocus()
            # Показываем окно и устанавливаем фокус на него
            msg.show()
            msg.activateWindow()
            msg.raise_()
            msg.exec()
        else:
            self.close()

    def input_number(self, button=None):
        new_text = self.NumberLine.text() + self.sender().text()
    
        if self.label_sender == 'TimeZad':
            if len(new_text) == 1 and int(self.sender().text()) >= 6:
                return
            elif len(new_text) == 2:
                new_text += ':'
            elif len(new_text) == 4 and int(self.sender().text()) >= 6:
                return
            elif len(new_text) > 5:
                return
            
        if self.label_sender == 'HFPowerZad' and len(new_text) == 1 and str.startswith(new_text, '0'):
            return 
        
        self.NumberLine.setText(new_text)