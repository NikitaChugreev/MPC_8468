import math
import time
import threading
from datetime import datetime, timedelta

from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import QMessageBox
from concurrent.futures import ThreadPoolExecutor

from logging.handlers import RotatingFileHandler
import logging

import fun
from state_controller import controller
from state_machine import PlasmaAutoProcess, process_logger

from config.settings import settings

pressure_atm = 1000.0

number_gases = settings.get('NUMBER_GASES', 2)
if number_gases == 3:
    from ui.ui_ser.ui_3.mainwindow import Ui_MainWindow
    ui_dir = 'ui/ui_ser/ui_3/'
elif number_gases == 2:
    from ui.ui_ser.ui_2.mainwindow import Ui_MainWindow
    ui_dir = 'ui/ui_ser/ui_2/'

from windows.prof_window import ProfWindow
from windows.rec_window import RecWindow
from windows.key_window import KeyWindow

from recipes.recipes import recipes
from utils.translator import Translator

if number_gases == 4:
    work_gases = ['1', '2', '3', '4']
elif number_gases == 3:
    work_gases = ['1', '2', '3']
elif number_gases == 2:
    work_gases = ['1', '2']

all_gases = work_gases + ['01']

venting_logger = logging.getLogger('on_venting_clicked')
venting_logger.setLevel(logging.DEBUG)
venting_logger.propagate = False

venting_handler = RotatingFileHandler(
    filename="on_venting_clicked.log",
    maxBytes=10*1024*1024,
    backupCount=3,
    encoding='utf-8'
)
venting_handler.setFormatter(
    logging.Formatter('%(asctime)s.%(msecs)03d - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
)
venting_logger.addHandler(venting_handler)

log_handler = RotatingFileHandler(filename="app.log", maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.DEBUG, handlers=[log_handler])

from PyQt5.QtCore import QObject, QThread, pyqtSignal

class ReadFlowsWorker(QObject):
    flowRead = pyqtSignal(int, float)
    finished = pyqtSignal()

    def __init__(self, controller, active_rrgs, gas_types):
        super().__init__()
        self.controller = controller
        self.active_rrgs = active_rrgs
        self.gas_types = gas_types
        self._running = True

    def stop(self):
        self._running = False

    @QtCore.pyqtSlot()
    def run(self):
        try:
            for num_rrg in self.active_rrgs:
                if not self._running:
                    break

                try:
                    value = self.controller.handle_command(
                        'read_flow',
                        num_rrg=num_rrg,
                        type_gas=self.gas_types[num_rrg]
                    )
                    if value is None:
                        value = 0.0
                except Exception as e:
                    logging.error(f"Error reading flow for RRG {num_rrg}: {e}")
                    value = 0.0

                if not self._running:
                    break
                    
                try:
                    rrg_id = int(num_rrg) if str(num_rrg).strip() in ('1', '2', '3', '4') else num_rrg
                    self.flowRead.emit(rrg_id, float(value))
                except RuntimeError as e:
                    logging.debug(f"RuntimeError emitting signal (object destroyed): {e}")
                    break
                except Exception as e:
                    logging.error(f"Error emitting flowRead signal: {e}")

        except Exception as e:
            logging.error(f"Error in ReadFlowsWorker.run: {e}")
        finally:
            try:
                if self._running:
                    self.finished.emit()
            except RuntimeError:
                logging.debug("RuntimeError emitting finished signal (object destroyed)")
            except Exception as e:
                logging.error(f"Error emitting finished signal: {e}")


class VentingResultWorker(QObject):
    ventingCompleted = pyqtSignal(dict)
    finished = pyqtSignal()


class ReadRFWorker(QObject):
    rfDataRead = pyqtSignal(object)
    finished = pyqtSignal()
    
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._running = True
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5

    def stop(self):
        self._running = False
    
    @QtCore.pyqtSlot()
    def run(self):
        read_count = 0
        try:
            QtCore.QThread.msleep(1000)
            
            while self._running:
                try:
                    read_start = time.time()
                    status = None
                    read_time = 0
                    
                    if not self._running:
                        break
                    
                    if not self._consecutive_failures >= self._max_consecutive_failures:
                        status = self.controller.rf.read_status()
                        read_time = time.time() - read_start
                        read_count += 1
                        
                        if not self._running:
                            break
                        
                        if read_time > 1.5:
                            status = None
                    
                    if status:
                        self._consecutive_failures = 0
                        self.rfDataRead.emit(status)
                    else:
                        if read_time > 0:
                            self._consecutive_failures += 1
                        
                        if read_time > 0 and self._consecutive_failures >= self._max_consecutive_failures:
                            try:
                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                if reconnect_success:
                                    self._consecutive_failures = 0 
                                else:
                                    process_logger.warning(f"[ReadRFWorker] Failed to reconnect RF generator: {reconnect_msg}")
                            except Exception as reconnect_error:
                                process_logger.error(f"[ReadRFWorker] Error reconnecting RF generator: {reconnect_error}")
                        
                        self.rfDataRead.emit(None)
                    
                    if not self._running:
                        break
                except Exception as e:
                    self._consecutive_failures += 1
                    logging.error(f"Error reading RF status: {e}")
                    
                    if self._consecutive_failures >= self._max_consecutive_failures:
                        try:
                            reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                            if reconnect_success:
                                self._consecutive_failures = 0
                            else:
                                process_logger.warning(f"[ReadRFWorker] Failed to reconnect RF generator after error: {reconnect_msg}")
                        except Exception as reconnect_error:
                            process_logger.error(f"[ReadRFWorker] Error reconnecting RF generator after error: {reconnect_error}")
                    
                    self.rfDataRead.emit(None)
                
                if not self._running:
                    break
                
                if self._consecutive_failures >= self._max_consecutive_failures:
                    interval_seconds = 10
                else:
                    interval_seconds = 2
                
                sleep_iterations = interval_seconds * 10
                for _ in range(sleep_iterations):
                    if not self._running:
                        break
                    QtCore.QThread.msleep(100)
        except Exception as e:
            process_logger.error(f"[ReadRFWorker] Fatal error: {e}", exc_info=True)
            logging.error(f"Error in ReadRFWorker: {e}", exc_info=True)
        finally:
            process_logger.info(f"[ReadRFWorker] Thread finished, total reads: {read_count}")
            self.finished.emit()


class MainWindow(QtWidgets.QMainWindow, Ui_MainWindow):
    def __init__(self):
        super().__init__()
        self.setupUi(self)
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowTitleHint)
        self.setWindowTitle('GN')

        self.user_mode = 'Operator'

        self.flow_thread = None
        self.flow_worker = None
        self._flow_thread_lock = threading.Lock()
        self._flow_thread_busy = False
        
        self.rf_thread = None
        self.rf_worker = None
        self._rf_thread_lock = threading.Lock()
        self._rf_thread_busy = False
        
        self._stopping_gases = False
        self._stop_gas_step = 0
        self._stop_gas_rrgs = []
        self.timer_stop_gases = QTimer()
        self.timer_stop_gases.timeout.connect(self._process_stop_gases)
        self._stop_gas_executor = None
        self.stop_gases_thread = None
        self.stop_gases_worker = None
        
        self._venting_executor = None
        self._venting_in_progress = False
        self._venting_result_worker = VentingResultWorker()
        self._venting_result_worker.ventingCompleted.connect(self._on_venting_completed)
        
        self._rf_operations_executor = None

        self.translator = Translator()
        
        self.time_start_work = time.time()

        self.controller = controller
        self.plasma_process = PlasmaAutoProcess(self.controller, self)

        self.RecName.setReadOnly(True)
        self.StatusLine.setReadOnly(True)
        self.StatusLine.setFocusPolicy(QtCore.Qt.NoFocus)
        self.ButtonStart.setEnabled(False)
        
        self.ButtonClose.clicked.connect(self.close)
        self.ButtonOutput.clicked.connect(self.open_prof)
        self.ButtonRecept.clicked.connect(self.open_rec)

        self.ButtonStart.clicked.connect(self.on_start_button_clicked)
        self.NIButton.clicked.connect(self.on_start_pump_clicked)
        self.VEButton.clicked.connect(self.on_venting_clicked)
        self.HFButton.clicked.connect(self.on_start_plasma_clicked)
        self.VE0Button.clicked.connect(self.on_venting_atm_clicked)

        if hasattr(self, 'button_rrg_1'):
            self.button_rrg_1.clicked.connect(lambda: self.save_address_rrg("1"))
        if hasattr(self, 'button_rrg_2'):
            self.button_rrg_2.clicked.connect(lambda: self.save_address_rrg("2"))
        if hasattr(self, 'button_rf'):
            self.button_rf.clicked.connect(self.save_address_rf)

        self.current_recipe = None

        self.max_attempts = 10

        self.pump_is_running = False

        self.last_values = {
            'flow_rrg1': 0,
            'flow_rrg2': 0,
            'flow_rrg3': 0,
            'flow_rrg4': 0,
        }

        self.timer_update_time = QTimer()
        self.timer_update_time.timeout.connect(self.update_time)

        self.timer_check_button_start = QTimer()
        self.timer_check_button_start.timeout.connect(self.check_button_start)

        self.timer_read_flows = QTimer()
        self.timer_read_flows.timeout.connect(self.read_flows_async)
        self.timer_read_flows.start(2000)

        self.timer_permissions = QTimer()
        self.timer_permissions.timeout.connect(self.check_permissions)

        self.venting_atm_start_time = 0
        self.timer_venting_atm = QTimer()
        self.timer_venting_atm.timeout.connect(self.update_venting_atm_time)

        self.pumping_start_time = 0
        self.timer_pumping = QTimer()
        self.timer_pumping.timeout.connect(self.update_display_time)

        self.plasma_start_time = 0
        self.timer_plasma = QTimer()
        self.timer_plasma.timeout.connect(self.update_plasma_time)

        self.timer_update_values = QTimer()
        self.timer_update_values.timeout.connect(self.update_values)

        self.PressProgress.setMaximum(100)

        buttons_commands = {
            'PressZad': self.PressZad,
            'TimeZad': self.TimeZad,
            'HFPowerZad': self.HFPowerZad
        }

        for i in range(1, number_gases + 1):
            buttons_commands[f'VE{i}FlowZad'] = getattr(self, f'VE{i}FlowZad')

        for key, button in buttons_commands.items():
            button.clicked.connect(lambda checked, k=key: self.open_key(k))
        
        self.labels_service = [
            self.label_2, self.label_3, self.label_4, self.label_5, self.label_6, self.label_7, self.label_8, self.label_9, 
            self.label_11, self.label_13, self.label_14, self.label_16, self.label_17,
            self.label_26, self.label_28, self.label_29, self.label_30, self.label_31, self.label_35, self.label_36,
            self.PressLableSADC, self.PressLableSZnachU, self.WLabelS, self.BPButtonS,
            self.title_address_rf, self.led_start_value, self.led_stop_value,
            self.led_vacuum_value, self.pump_value, self.ps_value,
            self.valve_ve01_value, self.buzz_value, self.plasma_value, self.button_rf
        ]
        
        self.buttons_service = [
            self.DoorButtonS, self.StartButtonS, self.StopButtonS, self.DoorLightS, self.StartLightS, self.StopLightS, 
            self.VE01ButtonS, self.NIButtonS, self.HFButtonS, self.BuzzButtonS, self.ButtonClose
        ]

        for btn in self.buttons_service:
            btn.clicked.connect(lambda checked, btn=btn: self.handle_commands(btn.objectName()))

        for i in range(1, number_gases + 1):
            self.labels_service.extend([
                getattr(self, f'button_rrg_{i}'),
                getattr(self, f'valve_ve{i}_value'),
                getattr(self, f'title_address_rrg{i}')
            ])
            self.buttons_service.append(getattr(self, f'VE{i}ButtonS'))

        self.PressProgress.hide()
        self.TimeProgress.hide()

        if self.controller.init_is_successfully:
            self.timer_update_time.start(1000)
            self.timer_check_button_start.start(500)
            self.timer_permissions.start(500)

            self.update_labels()
            self.update_ui_texts()

            QTimer.singleShot(100, self.init_system)
            QTimer.singleShot(200, self.check_service_pump)
            QTimer.singleShot(500, lambda: self.timer_update_values.start(1000))
            
        else:
            info_text = self.translator.tr('error_init_devices') + str(self.controller.fault_device_init)
            self.show_msg(text=self.translator.tr('warning'), info_text=info_text)
            self.close()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.isFullScreen():
            self.showFullScreen()
        event.accept()

    def show_msg(self, text, info_text):
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

        msg.setIcon(QMessageBox.Information)
        msg.setText(text)
        msg.setInformativeText(info_text)
        msg.setDefaultButton(None)

        ok_button = msg.button(QMessageBox.Ok)
        if ok_button:
            ok_button.setFocusPolicy(QtCore.Qt.NoFocus)
            ok_button.clearFocus()
            msg.setFocus()

        msg.show()
        msg.activateWindow()
        msg.raise_()
        msg.exec()

    def check_service_pump(self):
        if settings['time_pump_for_service'] > settings['max_time_pump_for_service']:
            self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('service_pump'))

    def check_button_start(self):
        is_pressed = self.controller.handle_command('get_state_button_start')
        process_idle = self.plasma_process.current_state == 'idle'
        
        if is_pressed and self.ButtonStart.isEnabled() and process_idle:

                QTimer.singleShot(1000, self.on_start_button_clicked)

    def update_time(self):
        self.label_time.setText((datetime.now() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M:%S"))

    def save_address_rrg(self, number):
        find_address = self.controller.scan_modbus_rrg(number)
        self.label_2.setText(f"Адрес: {find_address}" if find_address != 0 else 'Адрес не найден')

    def save_address_rf(self):
        find_address = self.controller.scan_modbus_rf()
        self.label_2.setText(f"Адрес: {find_address}" if find_address != 0 else 'Адрес не найден')

    def _on_flow_thread_finished(self):
        with self._flow_thread_lock:
            if self.flow_worker:
                try:
                    try:
                        self.flow_worker.flowRead.disconnect()
                        self.flow_worker.finished.disconnect()
                    except (TypeError, RuntimeError):
                        pass
                    
                    self.flow_worker.deleteLater()
                except Exception as e:
                    logging.debug(f"Error deleting flow_worker: {e}")
                self.flow_worker = None

            if self.flow_thread:
                try:
                    try:
                        self.flow_thread.finished.disconnect()
                    except (TypeError, RuntimeError):
                        pass
                    
                    self.flow_thread.deleteLater()
                except Exception as e:
                    logging.debug(f"Error deleting flow_thread: {e}")
                self.flow_thread = None
            
            self._flow_thread_busy = False
    
    @QtCore.pyqtSlot()
    def _clear_flow_thread_references(self):
        try:
            with self._flow_thread_lock:
                self.flow_worker = None
                self.flow_thread = None
                self._flow_thread_busy = False
        except Exception as e:
            venting_logger.error(f"[_clear_flow_thread_references] Error clearing references: {e}")
    
    def _on_stop_gases_finished(self):
        try:
            if self.stop_gases_worker:
                try:
                    self.stop_gases_worker.deleteLater()
                except:
                    pass
                self.stop_gases_worker = None
            
            if self.stop_gases_thread:
                try:
                    self.stop_gases_thread.quit()
                    self.stop_gases_thread.wait(100)
                    self.stop_gases_thread.deleteLater()
                except:
                    pass
                self.stop_gases_thread = None
        except Exception as e:
            logging.error(f"Error in _on_stop_gases_finished: {e}")

    def stop_flow_thread(self, wait=False):
        venting_logger.info(f"[stop_flow_thread] Called, wait={wait}")
        
        if not self._flow_thread_lock.acquire(blocking=True, timeout=2.0):
            return
        
        try:
            if self.flow_worker:
                try:
                    self.flow_worker.stop()
                except Exception as e:
                    venting_logger.error(f"[stop_flow_thread] Error stopping flow worker: {e}", exc_info=True)

            if self.flow_thread:
                if self.flow_thread.isRunning():
                    try:
                        self.flow_thread.quit()
                        if wait:
                            if not self.flow_thread.wait(2000):
                                self.flow_thread.terminate()
                    except Exception as e:
                        venting_logger.error(f"[stop_flow_thread] Error stopping flow thread: {e}", exc_info=True)
                
                if wait:
                    try:
                        worker_to_delete = self.flow_worker
                        thread_to_delete = self.flow_thread
                        
                        if thread_to_delete:
                            try:
                                thread_to_delete.finished.disconnect()
                            except (TypeError, RuntimeError) as e:
                                venting_logger.debug(f"[stop_flow_thread] Could not disconnect finished signal (may already be disconnected): {e}")
                        
                        if worker_to_delete:
                            try:
                                try:
                                    worker_to_delete.flowRead.disconnect()
                                except (TypeError, RuntimeError):
                                    pass
                                try:
                                    worker_to_delete.finished.disconnect()
                                except (TypeError, RuntimeError):
                                    pass
                                
                                try:
                                    QtCore.QMetaObject.invokeMethod(
                                        worker_to_delete,
                                        "deleteLater",
                                        QtCore.Qt.QueuedConnection
                                    )
                                except Exception as e:
                                    venting_logger.debug(f"[stop_flow_thread] Error queuing worker deleteLater: {e}")
                            except Exception as e:
                                venting_logger.debug(f"[stop_flow_thread] Error deleting worker: {e}")
                        
                        if thread_to_delete:
                            try:
                                QtCore.QMetaObject.invokeMethod(
                                    thread_to_delete,
                                    "deleteLater",
                                    QtCore.Qt.QueuedConnection
                                )
                            except Exception as e:
                                venting_logger.debug(f"[stop_flow_thread] Error queuing thread deleteLater: {e}")
                    except Exception as e:
                        venting_logger.error(f"[stop_flow_thread] Error in cleanup: {e}", exc_info=True)
            
            if wait:
                def clear_references():
                    try:
                        with self._flow_thread_lock:
                            self.flow_worker = None
                            self.flow_thread = None
                            self._flow_thread_busy = False
                    except Exception as e:
                        venting_logger.error(f"[stop_flow_thread] Error clearing references: {e}")
                
                try:
                    if QtCore.QThread.currentThread() == QtWidgets.QApplication.instance().thread():
                        QTimer.singleShot(200, clear_references)
                    else:

                        QtCore.QMetaObject.invokeMethod(
                            self,
                            "_clear_flow_thread_references",
                            QtCore.Qt.QueuedConnection
                        )
                except Exception as e:
                    venting_logger.error(f"[stop_flow_thread] Error scheduling reference cleanup: {e}")
                    try:
                        with self._flow_thread_lock:
                            self.flow_worker = None
                            self.flow_thread = None
                            self._flow_thread_busy = False
                    except:
                        pass

        finally:
            self._flow_thread_lock.release()


    def read_flows_async(self):
        with self._flow_thread_lock:
            if self._flow_thread_busy:
                return
            
            if self.flow_thread is not None and self.flow_thread.isRunning():
                return

        active_rrgs = []
        gas_types = {}

        for i in work_gases:
            if getattr(self, f"VE{i}Button").isChecked():
                active_rrgs.append(i)
                gas_types[i] = getattr(self, f"VE{i}ComboBox").currentIndex()

        if not active_rrgs:
            with self._flow_thread_lock:
                if self.flow_thread is not None and self.flow_thread.isRunning():
                    self.stop_flow_thread()
            return

        with self._flow_thread_lock:
            if self.flow_thread is not None:
                return
            
            self._flow_thread_busy = True

        try:
            flow_thread = QThread()
            flow_worker = ReadFlowsWorker(
                controller=self.controller,
                active_rrgs=active_rrgs,
                gas_types=gas_types
            )

            flow_worker.moveToThread(flow_thread)
            flow_worker.flowRead.connect(self.on_flow_read)
            flow_thread.started.connect(flow_worker.run)
            flow_worker.finished.connect(flow_thread.quit)
            flow_thread.finished.connect(self._on_flow_thread_finished)

            with self._flow_thread_lock:
                self.flow_thread = flow_thread
                self.flow_worker = flow_worker

            flow_thread.start()
        except Exception as e:
            logging.error(f"Error starting flow thread: {e}")
            with self._flow_thread_lock:
                self.flow_worker = None
                self.flow_thread = None
                self._flow_thread_busy = False

    def on_flow_read(self, num_rrg, value):
        num_rrg = int(num_rrg) if num_rrg in (1, 2, 3, 4, '1', '2', '3', '4', 1.0, 2.0, 3.0, 4.0) else num_rrg
        if num_rrg not in (1, 2, 3, 4):
            return
        key = str(num_rrg)
        self.last_values[f'flow_rrg{key}'] = float(value)
        attr_name = f"VE{key}FlowZnach"
        if not hasattr(self, attr_name):
            return
        label = getattr(self, attr_name)
        label.setText(f"{float(value):.1f}")
    
    @QtCore.pyqtSlot(int, float)
    def update_flow_display(self, num_rrg, value):
        try:
            self.on_flow_read(num_rrg, value)
        except Exception as e:
            logging.error(f"Error updating flow display for RRG {num_rrg}: {e}")
    
    @QtCore.pyqtSlot()
    def _update_ui_after_stop(self):
        thread_id = threading.current_thread().ident
        try:
            self.VEButton.setText(self.translator.tr('start_venting_gas'))
            self.VEButton.setChecked(False)
            
            if settings.get('LANG') == 0:
                self.VEButton.setStyleSheet('font-size: 20px')
            
            self.update_status(self.translator.tr('gas_inlet_completed'))
        except Exception as e:
            logging.error(f"DEBUG: ERROR in _update_ui_after_stop: {e}", exc_info=True)

    def update_values(self):
        current_state = getattr(self.plasma_process, 'current_state', 'unknown')
        plasma_on = getattr(self.controller, '_cached_plasma_status', False)

        values = {'pressure': 0.0, 'water': 0.0}
        values_adc = None

        try:
            values_adc = self.controller.get_values_adc()
            if values_adc is None:
                values_adc = {'P': None, 'T': None}

            if values_adc.get('P') is not None:
               self.PressLableSADC.setText(str(values_adc['P']))
               self.PressLableSZnachU.setText(str(fun.bit_u(float(values_adc['P']))))

            try:
                pressure_raw = self.controller.handle_command('get_sensor_pressure')
                water_raw = self.controller.handle_command('get_sensor_water')

                values = {
                    'pressure': pressure_raw,
                    'water': water_raw,
                }
                
                if values['pressure'] is None:
                    values['pressure'] = 0.0
                if values['water'] is None:
                    values['water'] = 0.0

            except Exception as e:
                logging.error(f"Error reading sensor values: {e}, state={current_state}, plasma_on={plasma_on}", exc_info=True)
                values = {'pressure': 0.0, 'water': 0.0}

        except Exception as e:
            error_msg = str(e)
            error_code = getattr(e, 'errno', None)
            logging.error(f"update_values: CRITICAL ERROR in outer try block: {e}, errno={error_code}", exc_info=True)
            
            if error_code == 121 or 'I/O error' in error_msg or 'Remote I/O' in error_msg:
                try:
                    def reconnect_task():
                        try:
                            self.controller.reconnect_device('ADC')
                            self.controller.reconnect_device('sensor_water')
                        except Exception as reconnect_error:
                            logging.error(f"update_values: Error during sensor reconnection: {reconnect_error}")
                    if hasattr(self.controller, '_sensor_reconnect_executor'):
                        self.controller._sensor_reconnect_executor.submit(reconnect_task)
                    else:
                        reconnect_task()
                except Exception as reconnect_error:
                    logging.error(f"update_values: Error scheduling sensor reconnection: {reconnect_error}")
            
        states = None
        try:
            states = self.controller.handle_command('get_states')
        except Exception as e:
            logging.error(f"Error getting states: {e}")

        try:
            if states and isinstance(states, dict):
                led_vacuum = states.get('led_vacuum', False)
                
                try:
                    if values_adc and values_adc.get('P') is not None:
                        pressure_voltage = fun.bit_u(float(values_adc['P']))
                        pressure_led = settings.get('PRESSURE_LED', 4.347)
                        if pressure_voltage < pressure_led and not led_vacuum:
                            self.controller.handle_command('on_led_vacuum')
                        elif pressure_voltage >= pressure_led and led_vacuum:
                            self.controller.handle_command('off_led_vacuum')
                except Exception as adc_error:
                    logging.debug(f"ADC error in LED vacuum: {adc_error}")
        except Exception as e:
            logging.error(f"Error managing LED vacuum: {e}")

        try:
            if states and isinstance(states, dict):
                current_led_start_state = states.get('led_start', False)
                button_enabled = self.ButtonStart.isEnabled()
                button_text_is_start = self.ButtonStart.text() == self.translator.tr('start')
                
                should_led_start_be_on = button_enabled and button_text_is_start
                
                if should_led_start_be_on and not current_led_start_state:
                    self.controller.handle_command('on_led_start')
                elif not should_led_start_be_on and current_led_start_state:
                    self.controller.handle_command('off_led_start')
                
                current_led_stop_state = states.get('led_stop', False)
                button_text_is_stop = self.ButtonStart.text() == self.translator.tr('stop')
                
                should_led_stop_be_on = button_enabled and button_text_is_stop
                
                if should_led_stop_be_on and not current_led_stop_state:
                    self.controller.handle_command('on_led_stop')
                elif not should_led_stop_be_on and current_led_stop_state:
                    self.controller.handle_command('off_led_stop')
        except Exception as e:
            logging.error(f"Error syncing LED start/stop: {e}")

        try:
            pressure_value = float(values['pressure'])
            if pressure_value < 10:
                new_pressure_text = f"{pressure_value:.2f}"
            else:
                new_pressure_text = f"{int(pressure_value)}"
            
            self.PressZnach.setText(new_pressure_text)
        
        except (ValueError, TypeError) as e:
            logging.error(f"Error converting pressure to float: {e}, value: {values.get('pressure')}")
            self.PressZnach.setText("0.00")

        try:
            if values['water'] is not None:
                new_water_text = f"{values['water']:.3f}"
                self.WLabelS.setText(new_water_text)
            else:
                self.WLabelS.setText("0.000")
        except Exception as e:
            logging.error(f"Error updating water display: {e}, value: {values.get('water')}")
            self.WLabelS.setText("0.000")
    
        try:
            press_znach = float(self.PressZnach.text())
            press_zad = float(self.PressZad.text())
            
            if press_znach <= press_zad:
                self.PressProgress.setValue(100)
            else:
                logZnach = math.log10(max(0.001, press_znach))
                logZad = math.log10(max(0.001, press_zad))
                logMax = math.log10(pressure_atm)
                
                if logMax != logZad and logMax > logZad:
                    progressPressure = 100.0 - ((logZnach - logZad) / (logMax - logZad)) * 100.0
                    progressPressure = max(0.0, min(100.0, progressPressure))
                    self.PressProgress.setValue(round(progressPressure))
                else:
                    if press_zad > 0 and press_zad < pressure_atm:
                        progressPressure = 100.0 - ((press_znach - press_zad) / (pressure_atm - press_zad)) * 100.0
                        progressPressure = max(0.0, min(100.0, progressPressure))
                        self.PressProgress.setValue(round(progressPressure))
                    else:
                        self.PressProgress.setValue(0)
        except (ValueError, TypeError) as e:
            logging.error(f"Error calculating pressure progress: {e}, PressZnach: {self.PressZnach.text()}, PressZad: {self.PressZad.text()}")
        
        if self.user_mode == 'Technologist':
            if (current_state in ['idle', 'fault'] and not self.HFButton.isChecked()):
                button_text_is_stop = self.VEButton.text() == self.translator.tr('stop_venting_gas')
                if self.VEButton.isChecked() and button_text_is_stop:
                    self.VEButton.setEnabled(True)
                else:
                    try:
                        press_znach = float(self.PressZnach.text())
                        press_zad = float(self.PressZad.text())
                        if press_znach >= press_zad:
                            self.VEButton.setEnabled(False)
                        else:
                            self.VEButton.setEnabled(True)
                    except (ValueError, TypeError) as e:
                        logging.error(f"Error checking pressure for VEButton enable in update_values: {e}")

    def update_ui_texts(self):
        self.LabelProf_3.setText(self.translator.tr('status'))
        self.LabelProf.setText(self.translator.tr('recipe'))
        self.ButtonStart.setText(self.translator.tr('start'))
        self.ButtonRecept.setText(self.translator.tr('recipes'))
        self.ButtonRecept.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Recept.png'))
        self.ButtonOutput.setText(self.translator.tr('exit'))
        self.ButtonOutput.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Exit.png'))
        self.NIButton.setText(self.translator.tr('turn_on_pump'))
        self.VEButton.setText(self.translator.tr('start_venting_gas'))
        self.HFButton.setText(self.translator.tr('turn_on_plasma'))
        self.VE0Button.setText(self.translator.tr('start_venting'))

        for i in range(1, number_gases + 1):
            getattr(self, f'VE{i}Button').setText(self.translator.tr(f'gas_{i}'))

        self.LabelProf_2.setText(self.translator.tr('final_pressure'))
        self.LabelProf_6.setText(self.translator.tr('forward_power'))
        self.LabelProf_18.setText(self.translator.tr('reflected_power'))
        self.LabelProf_13.setText(self.translator.tr('process_time'))
        self.LabelProf_4.setText(self.translator.tr('setpoint'))
        self.LabelProf_5.setText(self.translator.tr('value'))
        self.LabelProf_16.setText(self.translator.tr('pressure_unit'))
        self.LabelProf_14.setText(self.translator.tr('pressure_unit'))
        self.LabelProf_7.setText(self.translator.tr('flow_unit'))
        self.LabelProf_10.setText(self.translator.tr('flow_unit'))
        self.LabelProf_8.setText(self.translator.tr('flow_unit'))
        self.LabelProf_9.setText(self.translator.tr('flow_unit'))
        self.LabelProf_12.setText(self.translator.tr('power_unit'))
        self.LabelProf_11.setText(self.translator.tr('power_unit'))
        # self.LabelProf_18.setText(self.translator.tr('power_unit'))
        
        for i in work_gases:
            getattr(self, f'VE{i}ComboBox').setItemText(0, self.translator.tr('air'))
            getattr(self, f'VE{i}ComboBox').setItemText(1, self.translator.tr('argon'))
            getattr(self, f'VE{i}ComboBox').setItemText(2, self.translator.tr('oxigen'))
            getattr(self, f'VE{i}ComboBox').setItemText(3, self.translator.tr('nitrogen'))
            if getattr(self, f'VE{i}ComboBox').count() < 5:
                getattr(self, f'VE{i}ComboBox').addItem("") 
            getattr(self, f'VE{i}ComboBox').setItemText(4, self.translator.tr('custom_gas'))

    def init_system(self):
        self.update_status(self.translator.tr('init'))
        self.controller.handle_command('on_bp')

        self.ButtonStart.setText(self.translator.tr('stop'))
        self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Stop.png'))
        QtWidgets.QApplication.processEvents()
        
        result = self.plasma_process.start_process()
        if not result:
            self.ButtonStart.setText(self.translator.tr('start'))
            self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Start.png'))
            QtWidgets.QApplication.processEvents()
        else:
            if self.ButtonStart.text() != self.translator.tr('stop'):
                self.ButtonStart.setText(self.translator.tr('stop'))
                self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Stop.png'))
                QtWidgets.QApplication.processEvents()

    def on_start_button_clicked(self):
        current_state = self.plasma_process.current_state
        
        if current_state == 'idle':

            if self.ButtonStart.text() != self.translator.tr('start'):
                self.ButtonStart.setText(self.translator.tr('start'))
                self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Start.png'))
                QtWidgets.QApplication.processEvents()
            
            if self.ButtonStart.text() == self.translator.tr('start'):          
                if self.TimeZad.text() == '00:00':
                    self.update_status(self.translator.tr('error_process_time_not_set'))
                    if self.user_mode == 'Operator':
                        QTimer.singleShot(1000, lambda: self.update_status(self.translator.tr('system_ready_oper')))
                    else:
                        QTimer.singleShot(1000, lambda: self.update_status(self.translator.tr('system_ready_tech')))
                elif float(self.HFPowerZad.text()) == 0:
                    self.update_status(self.translator.tr('error_forward_power_not_set'))
                    if self.user_mode == 'Operator':
                        QTimer.singleShot(1000, lambda: self.update_status(self.translator.tr('system_ready_oper')))
                    else:
                        QTimer.singleShot(1000, lambda: self.update_status(self.translator.tr('system_ready_tech')))
                elif all(float(getattr(self, f'VE{i}FlowZad').text()) == 0 for i in range(1, number_gases + 1)):
                        self.update_status(self.translator.tr('error_all_gas_flows_zero'))
                        if self.user_mode == 'Operator':
                            QTimer.singleShot(1000, lambda: self.update_status(self.translator.tr('system_ready_oper')))
                        else:
                            QTimer.singleShot(1000, lambda: self.update_status(self.translator.tr('system_ready_tech')))
                else:
                    if self.user_mode == "Operator":
                        if self.RecName.text() == '':
                            self.update_status(self.translator.tr('error_no_recipe_selected'))
                            QTimer.singleShot(1000, lambda: self.update_status(self.translator.tr('system_ready_oper')))    
                        else:
                            self.ButtonStart.setText(self.translator.tr('stop'))
                            self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Stop.png'))
                            QtWidgets.QApplication.processEvents()
                            
                            self.timer_venting_atm.stop()
                            self.timer_pumping.stop()
                            self.timer_plasma.stop()
        
                            result = self.plasma_process.start_recipe()
                            if not result:
                                self.ButtonStart.setText(self.translator.tr('start'))
                                self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Start.png'))
                                QtWidgets.QApplication.processEvents()
                            else:
                                if self.ButtonStart.text() != self.translator.tr('stop'):
                                    self.ButtonStart.setText(self.translator.tr('stop'))
                                    self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Stop.png'))
                                    QtWidgets.QApplication.processEvents()
                    else:
                        self.TimeZnach.setText('00:00')
                        self.DisplayTime.setText('00:00')

                        self.TimeProgress.setValue(0)

                        self.ButtonStart.setText(self.translator.tr('stop'))
                        self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Stop.png'))
                        
                        QtWidgets.QApplication.processEvents()
                        
                        result = self.plasma_process.start_recipe()
                        if not result:
                            self.ButtonStart.setText(self.translator.tr('start'))
                            self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Start.png'))
                            QtWidgets.QApplication.processEvents()
                        else:
                            if self.ButtonStart.text() != self.translator.tr('stop'):
                                self.ButtonStart.setText(self.translator.tr('stop'))
                                self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Stop.png'))
                                QtWidgets.QApplication.processEvents()
        else:
            if current_state == 'init_recipe' and self.plasma_process.current_step <= 2:
                return
            
            if current_state not in ['idle', 'fault']:
                self.plasma_process.stop_process()
                self.ButtonStart.setText(self.translator.tr('start'))
                self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Start.png'))

    def on_start_pump_clicked(self):
        if self.NIButton.text() == self.translator.tr('turn_on_pump'):
            if self.plasma_process.current_state == 'idle':
                success = False
                for attempt in range(self.max_attempts):
                    try:
                        self.controller.handle_command('on_pump')
                        time.sleep(0.1)
                        states = self.controller.handle_command('get_states')
                        if states and states.get('pump'):
                            success = True
                            break
                    except Exception as e:
                        logging.error(f"Error turning on pump (attempt {attempt + 1}): {e}")
                    
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.2)

                if success:
                    self.update_status(self.translator.tr('pump_on'))
                    self.NIButton.setText(self.translator.tr('turn_off_pump'))
                    self.pumping_start_time = time.time()
                    self.timer_pumping.start(100)
                else:
                    self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_turn_on_pump'))
                    self.update_status(self.translator.tr('error_turn_on_pump'))
                    self.NIButton.setChecked(False)
                    self.NIButton.setText(self.translator.tr('turn_on_pump'))
        else:
            success = False
            for attempt in range(self.max_attempts):
                try:
                    self.controller.handle_command('off_pump')
                    time.sleep(0.1)
                    states = self.controller.handle_command('get_states')
                    if states and not states.get('pump'):
                        success = True
                        break
                except Exception as e:
                    logging.error(f"Error turning off pump (attempt {attempt + 1}): {e}")
                
                if attempt < self.max_attempts - 1:
                    time.sleep(0.2)

            if success:
                self.update_status(self.translator.tr('pump_off'))
                self.NIButton.setText(self.translator.tr('turn_on_pump'))
                self.timer_pumping.stop()

            else:
                self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_turn_off_pump'))
                self.update_status(self.translator.tr('error_turn_off_pump'))
                self.NIButton.setChecked(True)

    def on_venting_clicked(self):
        if self.VEButton.text() == self.translator.tr('start_venting_gas'):
            if self.plasma_process.current_state == 'idle':
                validation_start = time.time()
                res, msg = self.validate_gas_selection()
                validation_time = time.time() - validation_start
                venting_logger.debug(f"[on_venting_clicked] validate_gas_selection took {validation_time:.3f}s, res={res}")
            
                if res:
                    selected_gases = []
                    for i in work_gases:
                        if getattr(self, f"VE{i}Button").isChecked():
                            selected_gases.append(i)
                    
                    if self._venting_in_progress:
                        return
                    
                    self._venting_in_progress = True
                    self.VEButton.setEnabled(False)
                    
                    main_window_ref = self
                    max_attempts_ref = self.max_attempts
                    
                    def start_venting_task():
                        try:
                            QTimer.singleShot(0, lambda: main_window_ref.update_status(main_window_ref.translator.tr('setting_flows')))
                            
                            success_set_flow = False
                            
                            # ШАГ 1: Установка потоков
                            flow_setup_start = time.time()
                            for attempt in range(max_attempts_ref):
                                attempt_start = time.time()
                                
                                # Устанавливаем потоки для всех РРГ
                                set_flow_success = True
                                for i in work_gases:
                                    flow_op_start = time.time()
                                    try:
                                        if i in selected_gases:
                                            type_gas = getattr(main_window_ref, f"VE{i}ComboBox").currentIndex()
                                            flow_lh = float(getattr(main_window_ref, f"VE{i}FlowZad").text())
                                            venting_logger.debug(f"[start_venting_task] Setting flow RRG{i}: type_gas={type_gas}, flow={flow_lh}")
                                            
                                            result = main_window_ref.controller.handle_command(command='set_flow', 
                                                                                   num_rrg=i, 
                                                                                   type_gas=type_gas,
                                                                                   flow_lh=flow_lh)
                                            flow_op_time = time.time() - flow_op_start
                                            venting_logger.debug(f"[start_venting_task] set_flow RRG{i} took {flow_op_time:.3f}s, result={result}")
                                            
                                            if result is False:
                                                set_flow_success = False
                                        else:
                                            # Устанавливаем поток в 0 для неактивных РРГ
                                            type_gas = getattr(main_window_ref, f"VE{i}ComboBox").currentIndex()
                                            venting_logger.debug(f"[start_venting_task] Setting flow RRG{i} to 0 (not selected)")
                                            
                                            result = main_window_ref.controller.handle_command(command='set_flow', 
                                                                                   num_rrg=i, 
                                                                                   type_gas=type_gas,
                                                                                   flow_lh=0)
                                            flow_op_time = time.time() - flow_op_start
                                            venting_logger.debug(f"[start_venting_task] set_flow RRG{i} to 0 took {flow_op_time:.3f}s, result={result}")
                                            
                                            if result is False:
                                                venting_logger.warning(f"[start_venting_task] Failed to set flow to 0 for RRG {i} (attempt {attempt + 1})")
                                                set_flow_success = False
                                    except Exception as e:
                                        flow_op_time = time.time() - flow_op_start
                                        venting_logger.error(f"[start_venting_task] Exception setting flow for RRG {i} (took {flow_op_time:.3f}s): {e}", exc_info=True)
                                        set_flow_success = False
                                    
                                    time.sleep(0.1)
                                
                                attempt_time = time.time() - attempt_start
                                venting_logger.debug(f"[start_venting_task] Flow setup attempt {attempt + 1} took {attempt_time:.3f}s, success={set_flow_success}")
                                
                                if set_flow_success:
                                    success_set_flow = True
                                    break
                                
                                if attempt < max_attempts_ref - 1:
                                    time.sleep(0.3)
                            
                            flow_setup_time = time.time() - flow_setup_start
                            venting_logger.info(f"[start_venting_task] Flow setup completed in {flow_setup_time:.3f}s, success={success_set_flow}")
                            
                            if success_set_flow:
                                # ШАГ 2: Открытие клапанов
                                valve_setup_start = time.time()
                                success_open_valve = False
                                
                                for attempt in range(max_attempts_ref):
                                    attempt_start = time.time()
                                    venting_logger.debug(f"[start_venting_task] Valve setup attempt {attempt + 1}/{max_attempts_ref}")
                                    
                                    for i in work_gases:
                                        valve_op_start = time.time()
                                        if i in selected_gases:
                                            venting_logger.debug(f"[start_venting_task] Opening valve VE{i}")
                                            main_window_ref.controller.handle_command(f"open_valve_ve{i}")
                                        else:
                                            venting_logger.debug(f"[start_venting_task] Closing valve VE{i}")
                                            main_window_ref.controller.handle_command(f"close_valve_ve{i}")
                                        valve_op_time = time.time() - valve_op_start
                                        venting_logger.debug(f"[start_venting_task] Valve VE{i} operation took {valve_op_time:.3f}s")
                                        time.sleep(0.05)
                                    
                                    time.sleep(0.1)
                                    
                                    states_check_start = time.time()
                                    valves_states = main_window_ref.controller.handle_command('get_valves_states')
                                    states_check_time = time.time() - states_check_start
                                    venting_logger.debug(f"[start_venting_task] get_valves_states took {states_check_time:.3f}s")
                                    
                                    if not isinstance(valves_states, dict):

                                        if states_check_time > 1.0:
                                            venting_logger.warning(f"[start_venting_task] SLOW get_valves_states: {states_check_time:.3f}s > 1s")

                                        all_valid = True
                                    else:
                                        all_valid = True
                                        for i in work_gases:
                                            if i in selected_gases:
                                                valve_state = valves_states.get(f"valve_ve{i}", 'unknown')
                                                if valve_state == 'close':
                                                    venting_logger.warning(f"[start_venting_task] Valve VE{i} should be open but is {valve_state}")
                                                    all_valid = False
                                    
                                    attempt_time = time.time() - attempt_start
                                    venting_logger.debug(f"[start_venting_task] Valve setup attempt {attempt + 1} took {attempt_time:.3f}s, all_valid={all_valid}")
                                    
                                    if all_valid:
                                        success_open_valve = True
                                        break
                                    
                                    if attempt < max_attempts_ref - 1:
                                        time.sleep(0.2)
                                
                                valve_setup_time = time.time() - valve_setup_start
                                venting_logger.info(f"[start_venting_task] Valve setup completed in {valve_setup_time:.3f}s, success={success_open_valve}")
                                
                                success_valve = success_open_valve
                                selected_gases_list = selected_gases.copy()
                                
                                main_window_ref._venting_result = {
                                    'success': success_valve,
                                    'selected_gases': selected_gases_list
                                }
                                
                                venting_logger.info(f"[start_venting_task] Emitting signal: success={success_valve}, gases={selected_gases_list}")
                                main_window_ref._venting_result_worker.ventingCompleted.emit({
                                    'success': success_valve,
                                    'selected_gases': selected_gases_list
                                })

                            else:
                                main_window_ref._venting_result = {
                                    'success': False,
                                    'error': 'flow_setup_failed',
                                    'selected_gases': selected_gases.copy()
                                }
                                
                                main_window_ref._venting_result_worker.ventingCompleted.emit({
                                    'success': False,
                                    'error': 'flow_setup_failed',
                                    'selected_gases': selected_gases.copy()
                                })
                            
                        except Exception as e:
                            venting_logger.error(f"[start_venting_task] FATAL ERROR: {e}", exc_info=True)
                            main_window_ref._venting_result = {
                                'success': False,
                                'error': 'exception',
                                'exception': str(e)
                            }
                            
                            main_window_ref._venting_result_worker.ventingCompleted.emit({
                                'success': False,
                                'error': 'exception',
                                'exception': str(e)
                            })
                    
                    if self._venting_executor is None:
                        self._venting_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="VentingGas")
                    self._venting_executor.submit(start_venting_task)

                else:
                    self.VEButton.setChecked(False)
                    self.VEButton.setText(self.translator.tr('start_venting_gas'))
                    if settings.get('LANG') == 0:
                        self.VEButton.setStyleSheet('font-size: 20px')
                    self.update_status(msg)

        else:
            if self._venting_in_progress:
                return
            
            self._venting_in_progress = True
            self.VEButton.setEnabled(False)
            
            active_rrgs = []
            gas_types = {}
            for i in work_gases:
                try:
                    if getattr(self, f"VE{i}Button").isChecked():
                        active_rrgs.append(i)
                        gas_types[i] = getattr(self, f"VE{i}ComboBox").currentIndex()
                except Exception as e:
                    venting_logger.error(f"[on_venting_clicked] Error checking VE{i}Button: {e}")
            
            
            self.update_status(self.translator.tr('stopping_venting_gas'))
            
            def stop_gases_task():
                start_time = time.time()
                
                try:
                    step1_start = time.time()
                    try:
                        flow_thread_running = False
                        try:
                            if self.flow_thread is not None and self.flow_thread.isRunning():
                                flow_thread_running = True
                        except:
                            pass
                        
                        if flow_thread_running:
                            stop_thread_start = time.time()
                            try:
                                self.stop_flow_thread(wait=True)
                                stop_thread_time = time.time() - stop_thread_start
                            except Exception as e:
                                stop_thread_time = time.time() - stop_thread_start
                                venting_logger.error(f"[stop_gases_task] Error in stop_flow_thread (took {stop_thread_time:.3f}s): {e}", exc_info=True)
                                try:
                                    if self.flow_thread and self.flow_thread.isRunning():
                                        self.flow_thread.terminate()
                                        if not self.flow_thread.wait(1000):
                                            venting_logger.error("[stop_gases_task] Flow thread did not terminate after 1s")
                                except Exception as e2:
                                    venting_logger.error(f"[stop_gases_task] Error force terminating flow thread: {e2}")
                        
                    except Exception as e:
                        venting_logger.error(f"[stop_gases_task] Error stopping flow thread: {e}", exc_info=True)
                    
                    time.sleep(0.1)
                    
                    step2_start = time.time()
                    valves_closed = {}
                    for i in active_rrgs:
                        valve_op_start = time.time()
                        try:
                            result = self.controller.handle_command(f"close_valve_ve{i}")
                            valve_op_time = time.time() - valve_op_start
                            venting_logger.debug(f"[stop_gases_task] close_valve_ve{i} took {valve_op_time:.3f}s, result={result}")
                            
                            if result:
                                valves_closed[i] = True
                            else:
                                valves_closed[i] = False
                            
                            if i < active_rrgs[-1]:
                                time.sleep(0.05)
                        except Exception as e:
                            valve_op_time = time.time() - valve_op_start
                            venting_logger.error(f"[stop_gases_task] Error closing valve VE{i} (took {valve_op_time:.3f}s): {e}", exc_info=True)
                            valves_closed[i] = False
                    
                    step2_time = time.time() - step2_start
                    venting_logger.info(f"[stop_gases_task] Step 2 completed in {step2_time:.3f}s, valves_closed: {valves_closed}")
                    
                    time.sleep(0.1)
                    
                    step3_start = time.time()
                    flows_set_to_zero = {}
                    for rrg_num in active_rrgs:
                        flow_op_start = time.time()
                        try:
                            type_gas = gas_types.get(rrg_num, 0)
                            result = self.controller.handle_command(
                                command='set_flow',
                                num_rrg=rrg_num,
                                flow_lh=0,
                                type_gas=type_gas
                            )
                            
                            if result:
                                time.sleep(0.1)
                                current_flow = self.controller.handle_command(
                                    command='read_flow',
                                    num_rrg=rrg_num,
                                    type_gas=type_gas
                                )
                                
                                if current_flow is not None:
                                    flows_set_to_zero[rrg_num] = (abs(current_flow) < 0.1)
                                else:
                                    flows_set_to_zero[rrg_num] = False
                            else:
                                flows_set_to_zero[rrg_num] = False

                            if rrg_num < active_rrgs[-1]:
                                time.sleep(0.05)
                        except Exception as e:
                            flow_op_time = time.time() - flow_op_start
                            venting_logger.error(f"[stop_gases_task] Error setting flow for RRG {rrg_num} (took {flow_op_time:.3f}s): {e}", exc_info=True)
                            flows_set_to_zero[rrg_num] = False
                    
                    step3_time = time.time() - step3_start
                    venting_logger.info(f"[stop_gases_task] Step 3 completed in {step3_time:.3f}s, flows_set_to_zero: {flows_set_to_zero}")
                    
                    all_valves_closed = all(valves_closed.values()) if valves_closed else False
                    all_flows_zero = all(flows_set_to_zero.values()) if flows_set_to_zero else False
                    
                    if not all_flows_zero:
                        time.sleep(0.5)
                        
                        for rrg_num, is_zero in flows_set_to_zero.items():
                            if not is_zero:
                                type_gas = gas_types.get(rrg_num, 0)
                                for retry in range(3):
                                    try:
                                        retry_start = time.time()
                                        result = self.controller.handle_command(
                                            command='set_flow',
                                            num_rrg=rrg_num,
                                            flow_lh=0,
                                            type_gas=type_gas
                                        )
                                        retry_time = time.time() - retry_start
                                        
                                        if result:
                                            time.sleep(0.3)

                                            current_flow = self.controller.handle_command(
                                                command='read_flow',
                                                num_rrg=rrg_num,
                                                type_gas=type_gas
                                            )

                                            if current_flow is not None and abs(current_flow) < 0.1:
                                                flows_set_to_zero[rrg_num] = True
                                                break
                                        time.sleep(0.3)
                                    except Exception as e:
                                        venting_logger.error(f"[stop_gases_task] Error retrying RRG {rrg_num} (retry {retry + 1}): {e}")
                        
                        all_flows_zero = all(flows_set_to_zero.values()) if flows_set_to_zero else False
                    
                    if not all_valves_closed:
                        time.sleep(0.5)
                        
                        for i, is_closed in valves_closed.items():
                            if not is_closed:
                                for retry in range(3):
                                    try:
                                        retry_start = time.time()
                                        
                                        if result:
                                            valves_closed[i] = True
                                            break
                                        time.sleep(0.3)
                                    except Exception as e:
                                        venting_logger.error(f"[stop_gases_task] Error retrying valve VE{i} (retry {retry + 1}): {e}")
                        
                        all_valves_closed = all(valves_closed.values()) if valves_closed else False
                    
                    self._venting_result_worker.ventingCompleted.emit({
                        'success': True,
                        'stop': True,
                        'selected_gases': []
                    })
                    
                except Exception as e:
                    elapsed = time.time() - start_time
                    venting_logger.error(f"[stop_gases_task] EXCEPTION after {elapsed:.3f}s: {e}", exc_info=True)
                    venting_logger.error(f"[stop_gases_task] EXCEPTION: {e}", exc_info=True)
                    self._venting_result_worker.ventingCompleted.emit({
                        'success': True,
                        'stop': True,
                        'selected_gases': []
                    })
            
            try:
                if self._stop_gas_executor is None:
                    self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
                future = self._stop_gas_executor.submit(stop_gases_task)
            except Exception as e:
                logging.error(f"DEBUG: Error submitting task to executor: {e}", exc_info=True)
            
            def read_flows_post_stop_task():
                thread_id = threading.current_thread().ident
                import time
                for read_count in range(4):
                    time.sleep(0.5)
                    for rrg_num in active_rrgs:
                        try:
                            type_gas = gas_types.get(rrg_num, 0)
                            read_start = time.time()
                            current_flow = self.controller.handle_command(
                                command='read_flow',
                                num_rrg=rrg_num,
                                type_gas=type_gas
                            )
                            flow_value = current_flow if current_flow is not None else 0.0
                            QtCore.QMetaObject.invokeMethod(
                                self,
                                "update_flow_display",
                                QtCore.Qt.QueuedConnection,
                                QtCore.Q_ARG(int, rrg_num),
                                QtCore.Q_ARG(float, flow_value)
                            )
                        except Exception as e:
                            logging.error(f"DEBUG: Error reading flow for RRG {rrg_num}: {e}", exc_info=True)
                            QtCore.QMetaObject.invokeMethod(
                                self,
                                "update_flow_display",
                                QtCore.Qt.QueuedConnection,
                                QtCore.Q_ARG(int, rrg_num),
                                QtCore.Q_ARG(float, 0.0)
                            )
            
            QTimer.singleShot(500, lambda: self._stop_gas_executor.submit(read_flows_post_stop_task))

    def on_start_plasma_clicked(self):
        water_ok = True

        if settings.get('check_water_flow', True):
            try:
                water_flow = self.controller.handle_command('get_sensor_water')
                if water_flow == 0.0:
                    self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_water_flow_zero'))
                    water_ok = False
            except Exception as e:
                logging.error(f"start_recipe: Error checking water flow: {e}")
                self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_checking_water_flow'))
                water_ok = False

        if not self.timer_plasma.isActive():
            if water_ok:
                if self.TimeZad.text() != '00:00':
                    power = int(self.HFPowerZad.text())
                    if settings.get('MIN_POWER_BP') <= power <= settings.get('MAX_POWER_BP'):
                        
                        self.update_status(self.translator.tr('turning_on_plasma'))
                        self.HFButton.setChecked(True) 
                        
                        def start_plasma_task():
                            start_time = time.time()
                            
                            try:
                                if hasattr(self, 'stop_rf_reading'):
                                    self.stop_rf_reading(wait=False)
                                    time.sleep(0.3)
                                    
                                    if hasattr(self.controller.rf, '_lock'):
                                        if self.controller.rf._lock.acquire(blocking=False):
                                            self.controller.rf._lock.release()
                                        else:
                                            if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                                                self.controller.rf._lock.release()
                               
                                # ШАГ 1: Устанавливаем мощность
                                logging.info(f"START PLASMA: Step 1 - Setting power to {power}W")
                                success_set_power = False
                                for attempt in range(self.max_attempts):
                                    try:
                                        result = self.controller.handle_command('set_power', power=str(power))
                                        if result:
                                            success_set_power = True
                                            break
                                    except Exception as e:
                                        logging.error(f"START PLASMA: Error setting power (attempt {attempt + 1}): {e}")
                                    
                                    if attempt < self.max_attempts - 1:
                                        time.sleep(0.3)
                                
                                if not success_set_power:
                                    QtCore.QMetaObject.invokeMethod(
                                        self,
                                        "_on_plasma_start_error",
                                        QtCore.Qt.QueuedConnection,
                                        QtCore.Q_ARG(str, 'error_set_power')
                                    )
                                    return
                                
                                # ШАГ 2: Включаем плазму
                                logging.info("START PLASMA: Step 2 - Turning on plasma")
                                success = False
                                for attempt in range(self.max_attempts):
                                    try:
                                        result = self.controller.handle_command('on_plasma')
                                        if not result:
                                            if attempt < self.max_attempts - 1:
                                                time.sleep(0.3)
                                                continue
                                        
                                        time.sleep(1.0)
                                        
                                        try:
                                            rf_status = None
                                            rf_on = False
                                            for status_attempt in range(3):
                                                rf_status = self.controller.rf.read_status()
                                                if rf_status:
                                                    rf_on = rf_status.get('rf_on', False)
                                                    if rf_on:
                                                        break
                                                if status_attempt < 2:
                                                    time.sleep(0.5)
                                            if rf_status and rf_on:
                                                self.controller._cached_plasma_status = True
                                                success = True
                                                logging.info(f"START PLASMA: Plasma confirmed ON on attempt {attempt + 1}")
                                                break


                                            if not rf_status:

                                                def reconnect_rf_async():
                                                    try:
                                                        reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                                        if not reconnect_success:
                                                            logging.warning(f"[START PLASMA] Failed to reconnect RF generator (was None): {reconnect_msg}")
                                                    except Exception as reconnect_error:
                                                        logging.error(f"[START PLASMA] Error reconnecting RF generator (was None): {reconnect_error}")
                                                
                                                if not hasattr(self, '_rf_operations_executor') or self._rf_operations_executor is None:
                                                    from concurrent.futures import ThreadPoolExecutor
                                                    self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                                self._rf_operations_executor.submit(reconnect_rf_async)

                                        except Exception as e:
                                            logging.error(f"START PLASMA: Error reading RF status (attempt {attempt + 1}): {e}")

                                            def reconnect_rf_async():
                                                try:
                                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                                    if not reconnect_success:
                                                        logging.warning(f"[START PLASMA] Failed to reconnect RF generator (after error): {reconnect_msg}")
                                                except Exception as reconnect_error:
                                                    logging.error(f"[START PLASMA] Error reconnecting RF generator (after error): {reconnect_error}")
                                            
                                            if not hasattr(self, '_rf_operations_executor') or self._rf_operations_executor is None:
                                                from concurrent.futures import ThreadPoolExecutor
                                                self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                            self._rf_operations_executor.submit(reconnect_rf_async)

                                    except Exception as e:
                                        logging.error(f"START PLASMA: Error turning on plasma (attempt {attempt + 1}): {e}", exc_info=True)
                                    
                                    if attempt < self.max_attempts - 1:
                                        time.sleep(0.4)
                                

                                if success:
                                    QTimer.singleShot(0, self._on_plasma_started)
                                else:
                                    QtCore.QMetaObject.invokeMethod(
                                        self,
                                        "_on_plasma_start_error",
                                        QtCore.Qt.QueuedConnection,
                                        QtCore.Q_ARG(str, 'error_turn_on_plasma')
                                    )
                                
                            except Exception as e:
                                elapsed = time.time() - start_time
                                logging.error(f"START PLASMA: EXCEPTION after {elapsed:.3f}s: {e}", exc_info=True)
                                QtCore.QMetaObject.invokeMethod(
                                    self,
                                    "_on_plasma_start_error",
                                    QtCore.Qt.QueuedConnection,
                                    QtCore.Q_ARG(str, 'error_turn_on_plasma')
                                )
                        
                        if self._stop_gas_executor is None:
                            self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
                        self._stop_gas_executor.submit(start_plasma_task)
                    else:
                        self.HFButton.setChecked(False)
                        self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_set_valid_power'))
                        self.update_status(self.translator.tr('error_set_valid_power'))
                        QTimer.singleShot(2000, lambda: self.update_status(self.translator.tr('system_ready_tech')))
                else:
                    self.HFButton.setChecked(False)
                    self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_set_valid_time'))
                    self.update_status(self.translator.tr('error_set_valid_time'))
                    QTimer.singleShot(2000, lambda: self.update_status(self.translator.tr('system_ready_tech')))
            else:
                self.HFButton.setChecked(False)
                self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_set_valid_time'))
                self.update_status(self.translator.tr('error_water_flow_zero'))
                QTimer.singleShot(2000, lambda: self.update_status(self.translator.tr('system_ready_tech')))
        else:
            self.update_status(self.translator.tr('turning_off_plasma'))
            
            def stop_plasma_task():
                start_time = time.time()
                
                try:
                    if hasattr(self, 'stop_rf_reading'):
                        logging.info("STOP PLASMA: Stopping RF reading thread before off_plasma...")
                        self.stop_rf_reading(wait=False) 
                        time.sleep(0.5)

                        lock_acquired = False
                        if hasattr(self.controller.rf, '_lock'):
                            lock_check_start = time.time()
                            if self.controller.rf._lock.acquire(blocking=False):
                                self.controller.rf._lock.release()
                                lock_acquired = True
                            else:
                                if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                                    self.controller.rf._lock.release()
                                    lock_acquired = True
                        
                        try:
                            if hasattr(self.controller.rf, 'instrument') and hasattr(self.controller.rf.instrument, 'serial'):
                                if hasattr(self.controller.rf.instrument.serial, 'reset_input_buffer'):
                                    self.controller.rf.instrument.serial.reset_input_buffer()
                                if hasattr(self.controller.rf.instrument.serial, 'reset_output_buffer'):
                                    self.controller.rf.instrument.serial.reset_output_buffer()
                        except Exception as e:
                            logging.warning(f"STOP PLASMA: Could not clear serial buffers: {e}")
                    
                    result = False
                    plasma_off_start = time.time()
                    max_plasma_attempts = 5
                    
                    for plasma_attempt in range(max_plasma_attempts):
                        if plasma_attempt > 0:
                            try:
                                test_status = self.controller.rf.read_status()
                                if test_status is None:
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        time.sleep(0.5)
                                    else:
                                        logging.error(f"STOP PLASMA: Failed to reconnect RF generator before attempt {plasma_attempt + 1}: {reconnect_msg}")
                            except Exception as reconnect_test_error:
                                logging.warning(f"STOP PLASMA: Error checking connection before attempt {plasma_attempt + 1}: {reconnect_test_error}")
                                try:
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        time.sleep(0.5)
                                except Exception as reconnect_error:
                                    logging.error(f"STOP PLASMA: Error during reconnection attempt: {reconnect_error}")
                        
                        result = self.controller.handle_command('off_plasma')
                        
                        if result:
                            break
                        else:
                            if plasma_attempt < max_plasma_attempts - 1:
                                try:
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if not reconnect_success:
                                        logging.error(f"STOP PLASMA: Failed to reconnect RF generator before retry: {reconnect_msg}")
                                except Exception as reconnect_error:
                                    logging.error(f"STOP PLASMA: Error reconnecting before retry: {reconnect_error}")
                                
                                time.sleep(0.8)
                                
                                try:
                                    if hasattr(self.controller.rf, 'instrument') and hasattr(self.controller.rf.instrument, 'serial'):
                                        if hasattr(self.controller.rf.instrument.serial, 'reset_input_buffer'):
                                            self.controller.rf.instrument.serial.reset_input_buffer()
                                        if hasattr(self.controller.rf.instrument.serial, 'reset_output_buffer'):
                                            self.controller.rf.instrument.serial.reset_output_buffer()
                                except Exception as e:
                                    logging.debug(f"STOP PLASMA: Could not clear serial buffers before retry: {e}")
                    
                    time.sleep(0.5)
                    
                    success = False
                    plasma_off_confirmed = False
                    status_check_attempts = 5
                    
                    if not result:
                        try:
                            reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                            if reconnect_success:
                                logging.info("STOP PLASMA: RF generator reconnected for status check")
                                time.sleep(0.5)
                        except Exception as reconnect_error:
                            logging.error(f"STOP PLASMA: Error reconnecting for status check: {reconnect_error}")
                    
                    for status_attempt in range(status_check_attempts):
                        try:
                            rf_status = self.controller.rf.read_status()
                            if rf_status:
                                rf_on = rf_status.get('rf_on', True)
                                forward_power = rf_status.get('forward_w', None)
                                reflected_power = rf_status.get('reflect_w', None)

                                
                                if not rf_on:
                                    plasma_off_confirmed = True
                                    break
                                elif forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                    plasma_off_confirmed = True
                                    break
                            else:

                                try:
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        time.sleep(0.5)
                                        
                                        retry_result = self.controller.handle_command('off_plasma')
                                        if retry_result:
                                            time.sleep(0.5)
                                            retry_status = self.controller.rf.read_status()
                                            if retry_status:
                                                retry_rf_on = retry_status.get('rf_on', True)
                                                retry_forward = retry_status.get('forward_w', None)
                                                retry_reflected = retry_status.get('reflect_w', None)
                                                if not retry_rf_on or (retry_forward == 0 and retry_reflected == 0):
                                                    plasma_off_confirmed = True
                                                    break
                                    else:
                                        logging.error(f"[STOP PLASMA] CRITICAL: Failed to reconnect RF generator (was None): {reconnect_msg}")
                                except Exception as reconnect_error:
                                    logging.error(f"[STOP PLASMA] CRITICAL: Error reconnecting RF generator (was None): {reconnect_error}")
                                
                                try:
                                    forward_power = self.controller.handle_command('get_forward_power')
                                    reflected_power = self.controller.handle_command('get_reflected_power')
                                    logging.info(f"STOP PLASMA: Power check (status_check {status_attempt + 1}): forward={forward_power}, reflected={reflected_power}")
                                    if forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                        plasma_off_confirmed = True
                                        break
                                except Exception as e:
                                    logging.error(f"STOP PLASMA: Error checking power (status_check {status_attempt + 1}): {e}")
                                
                                if status_attempt < status_check_attempts - 1:
                                    time.sleep(0.5)
                        except Exception as e:
                            logging.error(f"STOP PLASMA: Error reading RF status (status_check {status_attempt + 1}): {e}")
                            try:
                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                if reconnect_success:
                                    time.sleep(0.5)
                                    
                                    retry_result = self.controller.handle_command('off_plasma')
                                    if retry_result:
                                        time.sleep(0.5)
                                        retry_status = self.controller.rf.read_status()
                                        if retry_status:
                                            retry_rf_on = retry_status.get('rf_on', True)
                                            retry_forward = retry_status.get('forward_w', None)
                                            retry_reflected = retry_status.get('reflect_w', None)
                                            if not retry_rf_on or (retry_forward == 0 and retry_reflected == 0):
                                                plasma_off_confirmed = True
                                                break
                                else:
                                    logging.error(f"[STOP PLASMA] CRITICAL: Failed to reconnect RF generator (after error): {reconnect_msg}")
                            except Exception as reconnect_error:
                                logging.error(f"[STOP PLASMA] CRITICAL: Error reconnecting RF generator (after error): {reconnect_error}")
                            
                            try:
                                forward_power = self.controller.handle_command('get_forward_power')
                                reflected_power = self.controller.handle_command('get_reflected_power')
                                if forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                    plasma_off_confirmed = True
                                    break
                            except Exception as e2:
                                logging.error(f"STOP PLASMA: Error checking power after error (status_check {status_attempt + 1}): {e2}")
                    
                    if plasma_off_confirmed:
                        success = True
                        self.controller._cached_plasma_status = False
                        QtCore.QMetaObject.invokeMethod(
                            self,
                            "_on_plasma_stopped",
                            QtCore.Qt.QueuedConnection
                        )
                    else:
                        try:
                            final_reconnect_success, final_reconnect_msg = self.controller.reconnect_device('RF')
                            if final_reconnect_success:
                                time.sleep(0.5)
                                final_off_result = self.controller.handle_command('off_plasma')
                                if final_off_result:
                                    time.sleep(0.5)
                                    final_status = self.controller.rf.read_status()
                                    if final_status:
                                        final_rf_on = final_status.get('rf_on', True)
                                        final_forward = final_status.get('forward_w', None)
                                        final_reflected = final_status.get('reflect_w', None)
                                        if not final_rf_on or (final_forward == 0 and final_reflected == 0):
                                            plasma_off_confirmed = True
                                            success = True
                                            self.controller._cached_plasma_status = False
                                            QtCore.QMetaObject.invokeMethod(
                                                self,
                                                "_on_plasma_stopped",
                                                QtCore.Qt.QueuedConnection
                                            )
                                            return
                        except Exception as final_error:
                            logging.error(f"STOP PLASMA: CRITICAL - Final attempt also failed: {final_error}")
                        
                        QtCore.QMetaObject.invokeMethod(
                            self,
                            "_on_plasma_stop_error",
                            QtCore.Qt.QueuedConnection
                        )
                    
                except Exception as e:
                    elapsed = time.time() - start_time
                    logging.error(f"STOP PLASMA: EXCEPTION after {elapsed:.3f}s: {e}", exc_info=True)
                    QtCore.QMetaObject.invokeMethod(
                        self,
                        "_on_plasma_stop_error",
                        QtCore.Qt.QueuedConnection
                    )
            
            if self._stop_gas_executor is None:
                self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
            self._stop_gas_executor.submit(stop_plasma_task)
    
    @QtCore.pyqtSlot(dict)
    def _on_venting_completed(self, result):
        try:
            venting_logger.info(f"[_on_venting_completed] METHOD CALLED with result: {result}")
            
            success = result.get('success', False)
            selected_gases = result.get('selected_gases', [])
            error = result.get('error')
            is_stop = result.get('stop', False)
            
            venting_logger.info(f"[_on_venting_completed] CALLED: success={success}, selected_gases={selected_gases}, error={error}, is_stop={is_stop}")
            
            if is_stop:
                self.VEButton.setChecked(False)
                new_text = self.translator.tr('start_venting_gas')
                self.VEButton.setText(new_text)

                if settings.get('LANG') == 0:
                    self.VEButton.setStyleSheet('font-size: 20px')
                self.VEButton.setEnabled(True)
                self.update_status(self.translator.tr('system_ready_tech'))
                return
            
            if success:
                if len(selected_gases) == 1:
                    self.update_status(f"{self.translator.tr('venting_gas')} {selected_gases[0]}.")
                elif len(selected_gases) == 2:
                    self.update_status(f"{self.translator.tr('venting_mixture_gases')} {selected_gases[0]} {self.translator.tr('and')} {selected_gases[1]}.")
                
                new_text = self.translator.tr('stop_venting_gas')
                self.VEButton.setText(new_text)
                if settings.get('LANG') == 0:
                    self.VEButton.setStyleSheet('font-size: 18px')
                self.VEButton.setChecked(True)
                self.VEButton.setEnabled(True)
            else:
                self.VEButton.setChecked(False)
                self.VEButton.setText(self.translator.tr('start_venting_gas'))
                if settings.get('LANG') == 0:
                    self.VEButton.setStyleSheet('font-size: 20px')
                if error == 'flow_setup_failed':
                    self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_setting_flow'))
                    self.update_status(self.translator.tr('error_setting_flow'))
                elif error == 'valve_setup_failed' or error is None:
                    self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_open_valves'))
                    self.update_status(self.translator.tr('error_open_valves'))
                self.VEButton.setEnabled(True)
        except Exception as e:
            venting_logger.error(f"[_on_venting_completed] Error updating UI: {e}", exc_info=True)
        finally:
            self._venting_in_progress = False
    
    @QtCore.pyqtSlot()
    def _on_plasma_started(self):
        self.HFButton.setText(self.translator.tr('turn_off_plasma'))
        self.plasma_start_time = time.time()

        try:
            minutes, sec = map(int, self.TimeZad.text().split(":"))
            total_sec = minutes * 60 + sec
            self.TimeProgress.setMaximum(total_sec)
            self.TimeProgress.setValue(0)
        except (ValueError, TypeError) as e:
            logging.error(f"START PLASMA: Error initializing TimeProgress: {e}")
            self.TimeProgress.setMaximum(100)
            self.TimeProgress.setValue(0)
        
        self.timer_plasma.start(100)
        self.update_status(self.translator.tr('plasma_on'))
        QTimer.singleShot(1000, self.start_rf_reading)
    
    @QtCore.pyqtSlot(str)
    def _on_plasma_start_error(self, error_type):
        logging.warning(f"START PLASMA: UI update - error starting plasma: {error_type}")
        self.HFButton.setChecked(False)
        if error_type == 'error_set_power':
            self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_set_power'))
            self.update_status(self.translator.tr('error_set_power'))
        else:
            self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_turn_on_plasma'))
            self.update_status(self.translator.tr('error_turn_on_plasma'))
    
    @QtCore.pyqtSlot()
    def _on_plasma_stopped(self):
        self.update_status(self.translator.tr('plasma_off'))
        self.HFButton.setText(self.translator.tr('turn_on_plasma'))
        self.HFButton.setChecked(False)
        self.timer_plasma.stop()
        if hasattr(self, 'HFPowerZnach'):
            self.HFPowerZnach.setText("0")
        if hasattr(self, 'HFCurrentZnach'):
            self.HFCurrentZnach.setText("0")
        self.stop_rf_reading(wait=False)
    
    @QtCore.pyqtSlot()
    def _on_plasma_stop_error(self):
        self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_turn_off_plasma'))
        self.update_status(self.translator.tr('error_turn_off_plasma'))

    def _process_stop_gases(self):
        if not self._stopping_gases:
            self.timer_stop_gases.stop()
            return
        
        if self._stop_gas_step > 0 and self._stop_gas_attempt > 10:
            self._stopping_gases = False
            self._stop_gas_step = 0
            self._stop_gas_rrgs = []
            self._stop_gas_attempt = 0
            self.timer_stop_gases.stop()
            return
        
        try:
            if self._stop_gas_step == 0:
                # Шаг 1: Закрываем все клапаны в отдельном потоке (не блокирует UI)
                def close_valves():
                    try:
                        for i in work_gases:
                            try:
                                self.controller.handle_command(f"close_valve_ve{i}")
                            except Exception as e:
                                logging.error(f"Error closing valve VE{i}: {e}")
                    except Exception as e:
                        logging.error(f"Error in close_valves: {e}")
                
                if self._stop_gas_executor is None:
                    self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
                self._stop_gas_executor.submit(close_valves)
                
                self._stop_gas_step = 2
                self._stop_gas_attempt = 0
                return
            
            elif self._stop_gas_step == 2:
                # Шаг 3: Устанавливаем поток в 0 для всех активных РРГ одновременно в отдельном потоке
                if self._stop_gas_attempt == 0:
                    def set_all_flows_zero():
                        for rrg_num in self._stop_gas_rrgs:
                            try:
                                result = self.controller.handle_command(
                                    command='set_flow', 
                                    num_rrg=rrg_num, 
                                    flow_lh=0, 
                                    type_gas=getattr(self, f"VE{rrg_num}ComboBox").currentIndex())
                            except Exception as e:
                                logging.error(f"Exception setting flow to 0 for RRG {rrg_num}: {e}")
                    
                    if self._stop_gas_executor is None:
                        self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
                    self._stop_gas_executor.submit(set_all_flows_zero)
                    
                    self._stop_gas_attempt = 1
                    self._stop_gas_step = 3
                    return
                else:
                    self._stop_gas_step = 3
                    return
            
            elif self._stop_gas_step == 3:
                def final_cleanup():
                    try:
                        with self._flow_thread_lock:
                            if self.flow_thread is not None and self.flow_thread.isRunning():
                                self.stop_flow_thread(wait=False)
                    except Exception as e:
                        logging.error(f"Error in final cleanup: {e}")
                
                if self._stop_gas_executor:
                    self._stop_gas_executor.submit(final_cleanup)
                
                self._stopping_gases = False
                self._stop_gas_step = 0
                self._stop_gas_rrgs = []
                self._stop_gas_attempt = 0
                self.timer_stop_gases.stop()
                return
                
        except Exception as e:
            logging.error(f"Error in _process_stop_gases: {e}")
            self._stopping_gases = False
            self._stop_gas_step = 0
            self._stop_gas_rrgs = []
            self._stop_gas_attempt = 0
            self.timer_stop_gases.stop()

    def on_venting_atm_clicked(self):
        if not self.timer_venting_atm.isActive():
            success = False
            for _ in range(self.max_attempts):
                self.controller.handle_command('open_valve_ve01')
                if self.controller.handle_command('get_states').get('valve_ve01'):
                    success = True
                    break
            
            if success:
                self.update_status(self.translator.tr('venting_atm'))
                self.VE0Button.setText(self.translator.tr('stop_venting'))
                self.VE0Button.setChecked(True)
                self.venting_atm_start_time = time.time()
                self.timer_venting_atm.start(100)
            else:
                self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_open_valve_ve01'))
                self.update_status(self.translator.tr('error_open_valve_ve01'))
                self.VE0Button.setChecked(False)

        else:
            success = False
            for _ in range(self.max_attempts):
                self.controller.handle_command('close_valve_ve01')
                if self.controller.handle_command('get_states').get('valve_ve01') == 'close':
                    success = True
                    break

            if success:
                self.update_status(self.translator.tr('end_venting_atm'))
                self.VE0Button.setChecked(False)
                self.VE0Button.setText(self.translator.tr('start_venting'))
                self.timer_venting_atm.stop()
            else:
                self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_close_valve_ve01'))
                self.update_status(self.translator.tr('error_close_valve_ve01'))
                self.VE0Button.setChecked(True)
            
    def update_time_znach(self, time_str):
        self.TimeZnach.setText(time_str)

    def update_plasma_time(self):
        if self.plasma_start_time == 0:
            return
        
        elapsed_time = int(time.time() - self.plasma_start_time)
        mins, secs = elapsed_time // 60, elapsed_time % 60
        self.TimeZnach.setText(f"{mins:02d}:{secs:02d}")

        try:
            minutes, sec = map(int, self.TimeZad.text().split(":"))
            total_sec = minutes * 60 + sec
            
            if self.TimeProgress.maximum() != total_sec:
                self.TimeProgress.setMaximum(total_sec)
            
            progress_value = max(0, min(total_sec, elapsed_time))
            self.TimeProgress.setValue(progress_value)
        except (ValueError, TypeError) as e:
            logging.error(f"update_plasma_time: Error parsing time or updating progress: {e}")
            return
        
        if elapsed_time >= total_sec:
            self.timer_plasma.stop()
            
            def stop_plasma_async():
                try:
                    success = False
                    for attempt in range(self.max_attempts):
                        try:
                            result = self.controller.handle_command('off_plasma')
                            if result:
                                time.sleep(0.3)
                                try:
                                    rf_status = self.controller.rf.read_status()
                                    if rf_status:
                                        rf_on = rf_status.get('rf_on', True)
                                        if not rf_on:
                                            self.controller._cached_plasma_status = False
                                            success = True
                                            QtCore.QMetaObject.invokeMethod(
                                                self,
                                                "_on_plasma_timeout",
                                                QtCore.Qt.QueuedConnection
                                            )
                                            break
                                    else:
                                        def reconnect_rf_async():
                                            try:
                                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                                if not reconnect_success:
                                                    logging.warning(f"[PLASMA TIMEOUT] Failed to reconnect RF generator (was None): {reconnect_msg}")
                                            except Exception as reconnect_error:
                                                logging.error(f"[PLASMA TIMEOUT] Error reconnecting RF generator (was None): {reconnect_error}")
                                        
                                        if not hasattr(self, '_rf_operations_executor') or self._rf_operations_executor is None:
                                            from concurrent.futures import ThreadPoolExecutor
                                            self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                        self._rf_operations_executor.submit(reconnect_rf_async)
                                except Exception as e:
                                    logging.error(f"PLASMA TIMEOUT: Error reading RF status (attempt {attempt + 1}): {e}")

                                    def reconnect_rf_async():
                                        try:
                                            reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                            if not reconnect_success:
                                                logging.warning(f"[PLASMA TIMEOUT] Failed to reconnect RF generator (after error): {reconnect_msg}")
                                        except Exception as reconnect_error:
                                            logging.error(f"[PLASMA TIMEOUT] Error reconnecting RF generator (after error): {reconnect_error}")
                                    
                                    if not hasattr(self, '_rf_operations_executor') or self._rf_operations_executor is None:
                                        from concurrent.futures import ThreadPoolExecutor
                                        self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                    self._rf_operations_executor.submit(reconnect_rf_async)
                        except Exception as e:
                            logging.error(f"PLASMA TIMEOUT: Error stopping plasma (attempt {attempt + 1}): {e}", exc_info=True)
                        
                        if attempt < self.max_attempts - 1:
                            time.sleep(0.4)
                    
                    if not success:
                        QtCore.QMetaObject.invokeMethod(
                            self,
                            "_on_plasma_timeout_error",
                            QtCore.Qt.QueuedConnection
                        )
                except Exception as e:
                    logging.error(f"PLASMA TIMEOUT: EXCEPTION in stop_plasma_async: {e}", exc_info=True)
                    QtCore.QMetaObject.invokeMethod(
                        self,
                        "_on_plasma_timeout",
                        QtCore.Qt.QueuedConnection
                    )
            
            if self._stop_gas_executor is None:
                self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
            self._stop_gas_executor.submit(stop_plasma_async)
    
    @QtCore.pyqtSlot()
    def _on_plasma_timeout(self):
        elapsed_time = int(time.time() - self.plasma_start_time)
        self.timer_plasma.stop()

        try:
            minutes, sec = map(int, self.TimeZad.text().split(":"))
            total_sec = minutes * 60 + sec
            progress_value = max(0, min(total_sec, elapsed_time))
            self.TimeProgress.setValue(progress_value)
        except (ValueError, TypeError) as e:
            logging.error(f"Error updating TimeProgress on timeout: {e}")
            self.TimeProgress.setValue(elapsed_time)

        self.update_status(self.translator.tr('plasma_end'))
        self.HFButton.setText(self.translator.tr('turn_on_plasma'))
        self.HFButton.setChecked(False)

        if hasattr(self, 'HFPowerZnach'):
            self.HFPowerZnach.setText("0")
        if hasattr(self, 'HFCurrentZnach'):
            self.HFCurrentZnach.setText("0")

        self.stop_rf_reading(wait=False)
    
    @QtCore.pyqtSlot()
    def _on_plasma_timeout_error(self):
        self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_turn_off_plasma'))
        self.update_status(self.translator.tr('error_turn_off_plasma'))
        self.HFButton.setText(self.translator.tr('turn_on_plasma'))
        self.HFButton.setChecked(False)
        self.timer_plasma.stop()
        self.stop_rf_reading(wait=False)
    
    def start_rf_reading(self):
        with self._rf_thread_lock:
            if self._rf_thread_busy:
                return
            
            if self.rf_thread is not None and self.rf_thread.isRunning():
                return
            
            self._rf_thread_busy = True
        
        try:
            rf_thread = QThread()
            rf_worker = ReadRFWorker(controller=self.controller)
            
            rf_worker.moveToThread(rf_thread)
            rf_worker.rfDataRead.connect(self.on_rf_data_read)
            rf_thread.started.connect(rf_worker.run)
            rf_worker.finished.connect(rf_thread.quit)
            rf_thread.finished.connect(self._on_rf_thread_finished)
            
            with self._rf_thread_lock:
                self.rf_thread = rf_thread
                self.rf_worker = rf_worker
            
            rf_thread.start()
        except Exception as e:
            process_logger.error(f"[start_rf_reading] Error: {e}", exc_info=True)
            logging.error(f"Error starting RF reading thread: {e}")
            with self._rf_thread_lock:
                self.rf_worker = None
                self.rf_thread = None
                self._rf_thread_busy = False
    
    def stop_rf_reading(self, wait=False):
        try:
            if self.rf_worker:
                self.rf_worker.stop()
                if wait:
                    time.sleep(0.5)

            if wait:
                lock_acquired = self._rf_thread_lock.acquire(blocking=True, timeout=3.0)
            else:
                lock_acquired = self._rf_thread_lock.acquire(blocking=False)
            
            if lock_acquired:
                try:
                    if self.rf_thread:
                        if self.rf_thread.isRunning():
                            self.rf_thread.quit()
                            if wait:
                                if not self.rf_thread.wait(2000):
                                    self.rf_thread.terminate()
                                    self.rf_thread.wait(1000)
                            
                            if wait:
                                try:
                                    self.rf_thread.deleteLater()
                                except:
                                    pass
                                self.rf_worker = None
                                self.rf_thread = None
                    
                    self._rf_thread_busy = False
                finally:
                    self._rf_thread_lock.release()
        except Exception as e:
            logging.error(f"STOP RF READING: Error: {e}", exc_info=True)
            process_logger.error(f"[stop_rf_reading] Error: {e}", exc_info=True)
    
    def _on_rf_thread_finished(self):
        with self._rf_thread_lock:
            if self.rf_worker:
                try:
                    self.rf_worker.deleteLater()
                except:
                    pass
                self.rf_worker = None
            
            if self.rf_thread:
                try:
                    self.rf_thread.deleteLater()
                except:
                    pass
                self.rf_thread = None
            
            self._rf_thread_busy = False
    
    def on_rf_data_read(self, status):
        if status is None:
            return
        
        try:
            forward_power = status.get('forward_w', 0)
            reflected_power = status.get('reflect_w', 0)
            rf_on = status.get('rf_on', False)
            
            self.controller._cached_plasma_status = rf_on
            
            is_plasma_on = (
                rf_on or
                self.HFButton.text() == self.translator.tr('turn_off_plasma') or
                (hasattr(self.plasma_process, 'current_state') and 
                 self.plasma_process.current_state == 'processing')
            )
            
            process_logger.debug(f"[on_rf_data_read] forward={forward_power}, reflected={reflected_power}, rf_on={rf_on}, is_plasma_on={is_plasma_on}")
            
            if is_plasma_on:                
                if hasattr(self, 'HFPowerZnach'):
                    self.HFPowerZnach.setText(str(forward_power))
                if hasattr(self, 'HFCurrentZnach'):
                    self.HFCurrentZnach.setText(str(reflected_power))

            else:
                if not rf_on and forward_power == 0 and reflected_power == 0:
                    if hasattr(self, 'HFPowerZnach'):
                        self.HFPowerZnach.setText("0")
                    if hasattr(self, 'HFCurrentZnach'):
                        self.HFCurrentZnach.setText("0")

        except Exception as e:
            process_logger.error(f"[on_rf_data_read] Error: {e}", exc_info=True)
            logging.error(f"Error updating RF data display: {e}", exc_info=True)

    def update_display_time(self, time_str=''):
        if time_str == '':
            elapsed_time = int(time.time() - self.pumping_start_time)
            mins, secs = elapsed_time // 60, elapsed_time % 60
            self.DisplayTime.setText(f"{mins:02d}:{secs:02d}")
        else:
            self.DisplayTime.setText(time_str)
        
    def update_venting_atm_time(self):
        venting_time = settings['time_venting']
        remaining_time = int(venting_time - (time.time() - self.venting_atm_start_time))
        if remaining_time > 0:
            self.update_status(f"{self.translator.tr('venting_atm_end_for')} {remaining_time} {self.translator.tr('sec')}.")
        else:
            self.timer_venting_atm.stop()
            self.controller.handle_command('close_valve_ve01')
            self.update_status(self.translator.tr('end_venting_atm'))
            self.VE0Button.setText(self.translator.tr('start_venting'))
            self.VE0Button.setChecked(False)
            
    def validate_gas_selection(self):
        try:
            valves_active = []
            for i in work_gases:
                valves_active.append(1 if getattr(self, f'VE{i}Button').isChecked() else 0)
            
            if not any(valves_active):
                return False, self.translator.tr('no_gas_selected')
            
            flows = []

            for i in work_gases:
                if getattr(self, f'VE{i}Button').isChecked():
                    flow = float(getattr(self, f'VE{i}FlowZad').text() or 0)
                    if flow <= 0:
                        return False, f"{self.translator.tr('gas_flow')} {i} {self.translator.tr('cant_be_zero')}"
                    flows.append(flow)
            
            if sum(flows) <= 0:
                return False, f"{self.translator.tr('total_gas_flow')} {self.translator.tr('cant_be_zero')}"
                
            return True, self.translator.tr('validation_successful')
            
        except ValueError:
            return False, self.translator.tr('error_format_flow')
        except Exception as e:
            return False, f"{self.translator.tr('error_validation')}: {str(e)}."
    
    def check_permissions(self):
        process_state = self.plasma_process.current_state
        
        if process_state not in ['idle', 'fault']:
            if self.ButtonStart.text() != self.translator.tr('stop'):
                self.ButtonStart.setText(self.translator.tr('stop'))
                self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Stop.png'))
            self.ButtonStart.setEnabled(True)
        else:
            button_text = self.ButtonStart.text()
            if button_text != self.translator.tr('start'):
                self.ButtonStart.setText(self.translator.tr('start'))
                self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Start.png'))
            self.ButtonStart.setEnabled(True)

        self.NIButton.setEnabled(True)
        self.VEButton.setEnabled(True)
        self.HFButton.setEnabled(True)
        self.PressZad.setEnabled(True)
        self.HFPowerZad.setEnabled(True)
        self.TimeZad.setEnabled(True)

        for i in work_gases:
            getattr(self, f'VE{i}Button').setEnabled(False)
            getattr(self, f'VE{i}ComboBox').setEnabled(True)
            getattr(self, f'VE{i}FlowZad').setEnabled(True)

        self.NIButton.setStyleSheet('background-color:')

        # Запрет на изменение параметров во время работы авт. режима
        if self.ButtonStart.text() == self.translator.tr('stop') and self.user_mode != 'Service':
            self.NIButton.setEnabled(False)
            self.VEButton.setEnabled(False)
            self.HFButton.setEnabled(False)
            self.PressZad.setEnabled(False)
            self.HFPowerZad.setEnabled(False)
            self.TimeZad.setEnabled(False)

            for i in work_gases:
                getattr(self, f'VE{i}Button').setEnabled(False)
                getattr(self, f'VE{i}ComboBox').setEnabled(False)
                getattr(self, f'VE{i}FlowZad').setEnabled(False)   

        if self.user_mode == 'Operator':
            self.NIButton.setEnabled(False)
            self.VEButton.setEnabled(False)
            self.HFButton.setEnabled(False)
            self.PressZad.setEnabled(False)
            self.HFPowerZad.setEnabled(False)
            self.TimeZad.setEnabled(False)

            for i in work_gases:
                getattr(self, f'VE{i}Button').setEnabled(False)
                getattr(self, f'VE{i}ComboBox').setEnabled(False)
                getattr(self, f'VE{i}FlowZad').setEnabled(False)

            if self.current_recipe is None:
                self.ButtonStart.setEnabled(False)

        # Запрет на управление насосом, клапанами при запущенном авт. режиме
        if self.ButtonStart.text() == self.translator.tr('stop') and self.user_mode != 'Service':
            self.NIButton.setEnabled(False)
            self.VEButton.setEnabled(False)
            self.HFButton.setEnabled(False)
            self.VE0Button.setEnabled(False)

        # Запрет на включение плазмы при закрытых клапанах раб. газов
        if not self.HFButton.isChecked() and self.user_mode != 'Service':
            all_gases_off = all(not getattr(self, f'VE{i}Button').isChecked() for i in range(1, number_gases + 1))
            if all_gases_off:
                self.HFButton.setEnabled(False)

        # Запрет на включение плазмы при отсутствии напуска газов
        if not self.VEButton.isChecked() and not self.HFButton.isChecked() and self.user_mode != 'Service':
            self.HFButton.setEnabled(False)

        # Запрет на включение плазмы при невыставленных потоках раб. газов
        if not self.HFButton.isChecked() and self.user_mode != 'Service':
            all_flows_zero = True
            for i in range(1, number_gases + 1):
                if float(getattr(self, f'VE{i}FlowZnach').text()) != 0:
                    all_flows_zero = False
                    break
            if all_flows_zero:
                self.HFButton.setEnabled(False)

        # Запрет на запуск авт. режима при ручном управлении какого-либо устройства
        if any([self.NIButton.isChecked(), self.VEButton.isChecked(), 
                self.HFButton.isChecked(), self.VE0Button.isChecked()]) and self.user_mode != 'Service':
            self.ButtonStart.setEnabled(False)

        # Запрет на управление клапанами при работающей плазме
        if self.HFButton.isChecked() and self.user_mode != 'Service':
            self.VE0Button.setEnabled(False)
            self.VEButton.setEnabled(False)

        # Для технолога кнопка "напустить газ" недоступна, пока давление не станет ниже целевого
        # НО если кнопка уже нажата и текст = "остановить напуск газа", она должна быть доступна
        # Проверка выполняется ПОСЛЕ всех других установок, чтобы иметь приоритет
        if self.user_mode == 'Technologist':
            if (self.plasma_process.current_state in ['idle', 'fault'] and not self.HFButton.isChecked()):
                button_text_is_stop = self.VEButton.text() == self.translator.tr('stop_venting_gas')
                if self.VEButton.isChecked() and button_text_is_stop:
                    self.VEButton.setEnabled(True)
                else:
                    try:
                        press_znach = float(self.PressZnach.text())
                        press_zad = float(self.PressZad.text())
                        if press_znach >= press_zad:
                            self.VEButton.setEnabled(False)
                    except (ValueError, TypeError) as e:
                        logging.error(f"Error checking pressure for VEButton enable in check_permissions: {e}")

        if settings['time_pump_for_service'] > settings['max_time_pump_for_service']:
            self.NIButton.setStyleSheet('background-color: red')

        if self.user_mode == "Service":
            button_text = self.ButtonStart.text()
            if button_text == self.translator.tr('stop'):
                self.ButtonStart.setEnabled(True)
            elif button_text != self.translator.tr('start'):
                self.ButtonStart.setText(self.translator.tr('start'))
                self.ButtonStart.setIcon(QtGui.QIcon(ui_dir + 'Pictures13/Start.png'))
                self.ButtonStart.setEnabled(True)
            else:
                self.ButtonStart.setEnabled(True)

            self.NIButton.setEnabled(True)
            self.VEButton.setEnabled(True)
            self.HFButton.setEnabled(True)
            self.VE0Button.setEnabled(True)

        
        if not self.ButtonStart.isEnabled() and self.user_mode != 'Operator':
            self.PressZad.setEnabled(True)
            self.HFPowerZad.setEnabled(True)
            self.TimeZad.setEnabled(True)

            for i in work_gases:
                getattr(self, f'VE{i}FlowZad').setEnabled(True)
                getattr(self, f'VE{i}Button').setEnabled(True)
                getattr(self, f'VE{i}ComboBox').setEnabled(True)

    def update_status(self, status_message):
        self.StatusLine.setText(status_message)
        self.StatusLine.deselect()

    def update_labels(self):
        if self.user_mode == 'Operator':
            self.label_user.setText(self.translator.tr('operator'))

            if self.StatusLine.text() == self.translator.tr('system_ready_tech'):
                self.StatusLine.setText(self.translator.tr('system_ready_oper'))

            for label in self.labels_service:
                try:
                    label.hide()
                except:
                    pass

            for label in self.buttons_service:
                try:
                    label.hide()
                except:
                    pass

        elif self.user_mode == 'Technologist':
            self.label_user.setText(self.translator.tr('technologist'))
            if self.StatusLine.text() == self.translator.tr('system_ready_oper'):
                self.StatusLine.setText(self.translator.tr('system_ready_tech'))
                
            for label in self.labels_service:
                try:
                    label.hide()
                except:
                    pass

            for label in self.buttons_service:
                try:
                    label.hide()
                except:
                    pass

        elif self.user_mode == 'Service':
            self.label_user.setText(self.translator.tr('service_engineer'))

            for label in self.labels_service:
                try:
                    label.show()
                except:
                    pass
            
            for label in self.buttons_service:
                try:
                    label.show()
                except:
                    pass
            
    def open_prof(self):
        profile_window = ProfWindow(self)
        profile_window.show()

    def open_rec(self):
        recipes_window = RecWindow(self)
        recipes_window.show()

    def open_key(self, sender=None):
        key_window = KeyWindow(self, sender=sender)
        key_window.show()

    def update_recipe(self, num_recipe):
        self.current_recipe = num_recipe

        if self.user_mode == 'Operator':
            self.StatusLine.setText(self.translator.tr('system_ready_tech'))

        num_recipe = str(num_recipe)
            
        if num_recipe in recipes:
            self.RecName.setText(recipes[num_recipe]['title'])
            self.PressZad.setText(str(recipes[num_recipe]['ResPressure']))
            self.HFPowerZad.setText(str(recipes[num_recipe]['power']))
            self.TimeZad.setText(recipes[num_recipe]['time'])

            for i in work_gases:
                getattr(self, f"VE{str(i)}Button").setChecked(recipes[num_recipe][f"VE{str(i)}"]['switch'])
 
                if recipes[num_recipe][f"VE{str(i)}"]['flow'] != 0:
                    getattr(self, f"VE{str(i)}FlowZad").setText(str(recipes[num_recipe][f"VE{str(i)}"]['flow']))
                else:
                    getattr(self, f"VE{str(i)}FlowZad").setText('0.0')

                gas_val = recipes[num_recipe][f"VE{i}"]['gas']
                if gas_val is not None and str.isnumeric(str(gas_val)):
                    gas_idx = max(0, min(4, int(gas_val)))  # 0–4: Air, Ar, O2, N2, Свой газ
                    combo = getattr(self, f"VE{i}ComboBox")
                    while combo.count() < 5:
                        combo.addItem("")
                    combo.setItemText(4, self.translator.tr('custom_gas'))
                    combo.setCurrentIndex(gas_idx)

    def get_current_recipe(self):
        # 0 - Air, 1 - Ar, 2 - O2, 3 - N2, 4 - Свой газ (custom)
        try:
            def safe_float(value, default=0.0):
                try:
                    if value is None or value == '':
                        return default
                    return float(value)
                except (ValueError, TypeError):
                    return default
            
            def safe_int(value, default=0):
                try:
                    if value is None or value == '':
                        return default
                    return int(value)
                except (ValueError, TypeError):
                    return default
            
            def safe_str(value, default=''):
                if value is None:
                    return default
                return str(value) if str(value).strip() else default

            press_text = self.PressZad.text().strip()
            res_pressure = safe_float(press_text, 0.0)

            data_for_recipe = {
                "title": '',
                "com": '',
                "ResPressure": res_pressure,
                "power": safe_int(self.HFPowerZad.text(), 0),
                "time": safe_str(self.TimeZad.text(), '00:00')
            }
            for i in range(1, number_gases + 1):
                data_for_recipe[f'VE{i}'] = {
                    "switch": 1 if getattr(self, f'VE{i}Button').isChecked() else 0,
                    "gas": getattr(self, f'VE{i}ComboBox').currentIndex() if getattr(self, f'VE{i}Button').isChecked() else None,
                    "flow": safe_float(getattr(self, f'VE{i}FlowZad').text()) if getattr(self, f'VE{i}Button').isChecked() else 0.0
                }
            return data_for_recipe
        
        except Exception as e:
            logging.error(f"Error in get_current_recipe: {e}", exc_info=True)
            data = {
                "title": '',
                "com": '',
                "ResPressure": 0.0,
                "power": 0,
                "time": "00:00",
            }

            for i in range(1, number_gases + 1):
                data[f'VE{i}'] = {"switch": 0, "gas": None, "flow": 0.0}

            return data
            
    
    def handle_commands(self, sender):
        logs_text = {
            "open_valve_ve1": 'VE1 ОТКРЫТ ⭢ ЗАКРЫТ',
            "open_valve_ve2": 'VE2 ОТКРЫТ ⭢ ЗАКРЫТ',
            "open_valve_ve3": 'VE3 ОТКРЫТ ⭢ ЗАКРЫТ',
            "open_valve_ve4": 'VE4 ОТКРЫТ ⭢ ЗАКРЫТ',
            "open_valve_ve01": 'VE01 ОТКРЫТ ⭢ ЗАКРЫТ',
            "close_valve_ve1": 'VE1 ЗАКРЫТ ⭢ ОТКРЫТ',
            "close_valve_ve2": 'VE2 ЗАКРЫТ ⭢ ОТКРЫТ',
            "close_valve_ve3": 'VE3 ЗАКРЫТ ⭢ ОТКРЫТ',
            "close_valve_ve4": 'VE4 ЗАКРЫТ ⭢ ОТКРЫТ',
            "close_valve_ve01": 'VE01 ЗАКРЫТ ⭢ ОТКРЫТ',
            "close_valve_ve02": 'VE01 ЗАКРЫТ ⭢ ОТКРЫТ',
            'on_pump': 'Насос ОТКЛЮЧЕН ⭢ ВКЛЮЧЕН',
            'off_pump': 'Насос ВКЛЮЧЕН ⭢ ОТКЛЮЧЕН',
            'on_ps': 'БП ОТКЛЮЧЕН ⭢ ВКЛЮЧЕН',
            'off_ps': 'БП ВКЛЮЧЕН ⭢ ОТКЛЮЧЕН',
            'on_buzz': 'Бузер ОТКЛЮЧЕН ⭢ ВКЛЮЧЕН',
            'off_buzz': 'Бузер ВКЛЮЧЕН ⭢ ОТКЛЮЧЕН',
            'on_plasma': 'Плазма ОТКЛЮЧЕНА ⭢ ВКЛЮЧЕНА',
            'off_plasma': 'Плазма ВКЛЮЧЕНА ⭢ ОТКЛЮЧЕНА',
        }

        try:
            command = None

            if sender == 'VE1ButtonS':
                command = 'open_valve_ve1' if getattr(self, sender).isChecked() else 'close_valve_ve1'
            if sender == 'VE2ButtonS':
                command = 'open_valve_ve2' if getattr(self, sender).isChecked() else 'close_valve_ve2'
            if sender == 'VE3ButtonS':
                command = 'open_valve_ve3' if getattr(self, sender).isChecked() else 'close_valve_ve3'
            if sender == 'VE4ButtonS':
                command = 'open_valve_ve4' if getattr(self, sender).isChecked() else 'close_valve_ve4'
            if sender == 'VE01ButtonS':
                command = 'open_valve_ve01' if getattr(self, sender).isChecked() else 'close_valve_ve01'
            if sender == 'NIButtonS':
                command = 'on_pump' if getattr(self, sender).isChecked() else 'off_pump'
            if sender == 'BuzzButtonS':
                command = 'on_buzz' if getattr(self, sender).isChecked() else 'off_buzz'
            if sender == 'HFButtonS':
                command = 'on_plasma' if getattr(self, sender).isChecked() else 'off_plasma'

            logging.info(logs_text[command])
            self.controller.handle_command(command=command)

        except Exception as e:
            logging.error(str(sender) + ':' + str(e))
            
    def closeEvent(self, event):
        self.plasma_process.cleanup()
        
        if self._venting_executor is not None:
            self._venting_executor.shutdown(wait=False)
            self._venting_executor = None
        
        self.controller.handle_command('off_plasma')
                
        for i in all_gases:
            self.controller.handle_command(f"close_valve_ve{i}")
                
        self.controller.handle_command('off_pump')
            
        self.timer_read_flows.stop()
        self.timer_stop_gases.stop()
        self.timer_plasma.stop()
        
        self._stopping_gases = False
        
        if self._stop_gas_executor:
            try:
                self._stop_gas_executor.shutdown(wait=False)
            except:
                pass
            self._stop_gas_executor = None
        
        if hasattr(self, '_rf_operations_executor') and self._rf_operations_executor:
            try:
                self._rf_operations_executor.shutdown(wait=False)
            except:
                pass
            self._rf_operations_executor = None
        
        try:
            self.stop_rf_reading(wait=True)
        except Exception as e:
            logging.error(f"Error stopping RF reading thread: {e}")
        
        def stop_flow_final():
            try:
                with self._flow_thread_lock:
                    if self.flow_thread is not None and self.flow_thread.isRunning():
                        self.stop_flow_thread(wait=True)
            except Exception as e:
                logging.error(f"Error stopping flow thread: {e}")
        
        if self._flow_thread_lock.acquire(blocking=False):
            try:
                stop_flow_final()
            finally:
                self._flow_thread_lock.release()
        
        event.accept()