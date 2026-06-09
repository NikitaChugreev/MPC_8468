import time

from PyQt5 import QtCore, QtWidgets

from windows.set_window import SetWindow
from config.settings import settings, save_settings
from utils.translator import translator, language_emitter

from ui.ui_ser.profwindow import Ui_ProfWindow

class ProfWindow(QtWidgets.QMainWindow, Ui_ProfWindow):
    def __init__(self, parent=None):
        super(ProfWindow, self).__init__(parent)
        
        self.setupUi(self)
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowTitleHint)
        self.setWindowTitle('GN')
        self.showFullScreen()

        self.translator = translator
        language_emitter.language_changed.connect(self.update_ui_texts)

        self.pass_labels = [self.TextPass, self.LineEdit1, self.LineEdit2, self.LineEdit3, self.LineEdit4, self.Button0,
                            self.Button1, self.Button2, self.Button3, self.Button4, self.Button5, self.Button6, self.Button7,
                            self.Button8, self.Button9, self.ButtonEditPass, self.ButtonNI, self.ButtonVE0]

        self.buttons = [self.Button0, self.Button1, self.Button2, self.Button3, self.Button4,
                       self.Button5, self.Button6, self.Button7, self.Button8, self.Button9]

        self.init_labels()
        self.init_buttons()

        self.input_pass = ""
        self.i = 0
        self.current_role = None

        self.ButtonOperator.clicked.connect(self.select_operator)
        self.ButtonTechnologist.clicked.connect(self.select_technologist)
        self.ButtonService.clicked.connect(self.select_service)
        self.ButtonSettings.clicked.connect(self.open_settings)

        self.ButtonNI.clicked.connect(self.pump)
        self.ButtonVE0.clicked.connect(self.venting_atm)
        self.ButtonExit.clicked.connect(self.power_off)

        self.update_ui_texts()

    def pump(self):
        pump_state = self.parent().controller.handle_command('get_states').get('pump')
    
        if pump_state == 0:
            if self.parent().plasma_process.current_state == 'idle':
                self.parent().controller.handle_command('on_pump')
                self.parent().update_status(self.translator.tr('pump_on'))
                self.parent().controller.pump_is_running = True
                self.ButtonNI.setText(self.translator.tr('stop_pumping'))
                self.ButtonNI.setChecked(True)
                self.parent().NIButton.setText(self.translator.tr('turn_off_pump'))
                self.parent().NIButton.setChecked(True)
                self.parent().pumping_start_time = time.time()
                self.parent().timer_pumping.start(100)
        else:
            self.parent().controller.handle_command('off_pump')
            self.parent().update_status(self.translator.tr('pump_off'))
            self.parent().controller.pump_is_running = False
            self.ButtonNI.setText(self.translator.tr('start_pumping'))
            self.ButtonNI.setChecked(False)
            self.parent().NIButton.setText(self.translator.tr('turn_on_pump'))
            self.parent().NIButton.setChecked(False)
            self.parent().timer_pumping.stop()

    def venting_atm(self):
        venting_state = self.parent().controller.handle_command('get_states').get('valve_ve01')

        if venting_state == 'open':
            self.parent().controller.handle_command('close_valve_ve01')
            self.parent().timer_venting_atm.stop()
            self.parent().update_status(self.translator.tr('end_venting_atm'))
            self.ButtonVE0.setText(self.translator.tr('start_venting'))
            self.ButtonVE0.setChecked(False)
            self.parent().VE0Button.setText(self.translator.tr('start_venting'))
            self.parent().VE0Button.setChecked(False)
        else:
            self.parent().controller.handle_command('open_valve_ve01')
            self.parent().venting_atm_start_time = time.time()
            self.parent().timer_venting_atm.start(100)
            self.parent().update_status(self.translator.tr('venting_atm'))
            self.ButtonVE0.setText(self.translator.tr('stop_venting'))
            self.ButtonVE0.setChecked(True)
            self.parent().VE0Button.setText(self.translator.tr('stop_venting'))
            self.parent().VE0Button.setChecked(True)

    def update_ui_texts(self):
        self.LabelText.setText(self.translator.tr('select_profile'))
        self.TextPass.setText(self.translator.tr('enter_password'))
        self.ButtonOperator.setText(self.translator.tr('operator'))
        self.ButtonTechnologist.setText(self.translator.tr('technologist'))
        self.ButtonSettings.setText(self.translator.tr('settings'))
        self.ButtonService.setText(self.translator.tr('service_engineer'))
        self.ButtonExit.setText(self.translator.tr('turn_off'))

        pump_state = self.parent().controller.handle_command('get_states').get('pump')
        if not pump_state:
            self.ButtonNI.setText(self.translator.tr('start_pumping'))
            self.ButtonNI.setChecked(False)
            self.parent().NIButton.setText(self.translator.tr('turn_on_pump'))
        else:
            self.ButtonNI.setText(self.translator.tr('stop_pumping'))
            self.ButtonNI.setChecked(True)
            self.parent().NIButton.setText(self.translator.tr('turn_off_pump'))

        venting_state = self.parent().controller.handle_command('get_states').get('valve_ve01')
        if venting_state == 'close':
            self.ButtonVE0.setText(self.translator.tr('start_venting'))
            self.ButtonVE0.setChecked(False)
            self.parent().VE0Button.setText(self.translator.tr('start_venting'))
        else:
            self.ButtonVE0.setText(self.translator.tr('stop_venting'))
            self.ButtonVE0.setChecked(True)
            self.parent().VE0Button.setText(self.translator.tr('stop_venting'))

    def init_labels(self):
        for label in self.pass_labels:
            label.hide()
        
        self.ButtonNI.show()
        self.ButtonVE0.show()

        if self.parent().plasma_process.current_state != 'idle':
            self.ButtonNI.setEnabled(False)
            self.ButtonVE0.setEnabled(False)
            

    def init_buttons(self):
        for button in self.buttons:
            button.clicked.connect(self.input_number)

    def select_operator(self):
        self.current_role = 'Operator'
        self.parent().user_mode = self.current_role
        self.parent().update_labels()
        self.close()
        
    def select_technologist(self):
        self.current_role = 'Technologist'
        self.parent().user_mode = self.current_role
        self.parent().update_labels()
        
        if settings['use_pass_technologist']:
            for label in self.pass_labels:
                label.show()

            self.ButtonEditPass.hide()
            self.ButtonNI.hide()
            self.ButtonVE0.hide()
        else:
            self.close()

    def input_number(self):
        number = self.sender().text()
        if self.i < 4:
            self.input_pass += number
            self.i += 1
            if self.i == 1:
                self.LineEdit1.setText('  *')
            elif self.i == 2:
                self.LineEdit2.setText('  *')
            elif self.i == 3: 
                self.LineEdit3.setText('  *')
            elif self.i == 4:
                self.LineEdit4.setText('  *')
                self.check_password()
    
    def check_password(self):
        required_pass = settings['pass_technologist'] if self.current_role == 'Technologist' else settings['pass_service']
        
        if self.input_pass == str(required_pass):
            self.parent().user_mode = self.current_role
            self.parent().update_labels()
            self.close()
        else:
            self.TextPass.setText(self.translator.tr('wrong_password'))
            self.reset_password_input()

    def reset_password_input(self):
        self.input_pass = ""
        self.i = 0

        for line_edit in [self.LineEdit1, self.LineEdit2, self.LineEdit3, self.LineEdit4]:
            line_edit.setText("")

    def select_service(self):
        self.current_role = 'Service'
        self.parent().user_mode = self.current_role

        for label in self.pass_labels:
            label.show()

        self.ButtonEditPass.hide()
        self.ButtonNI.hide()
        self.ButtonVE0.hide()

        self.parent().update_labels()

    def open_settings(self):
        set = SetWindow(self)
        set.show()

    def power_off(self):
        import subprocess

        self.time_work = time.time() - self.parent().time_start_work
        last_time = settings.get('time_work', 0)
        current_time = last_time + self.time_work
        settings.update({'time_work': current_time})
        save_settings(settings)
        
        subprocess.run(['shutdown', '-h', 'now'])

