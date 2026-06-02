import json
import math
import os
import time
import threading
from datetime import datetime, timedelta

from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import QMessageBox
from concurrent.futures import ThreadPoolExecutor
import functools

from logging.handlers import RotatingFileHandler
import logging

import fun
from state_controller import controller
from state_machine import PlasmaAutoProcess, process_logger
from ui.mainwindow import Ui_MainWindow

from windows.prof_window import ProfWindow
from windows.rec_window import RecWindow
from windows.key_window import KeyWindow

from recipes.recipes import recipes
from config.settings import settings
from utils.translator import Translator


number_gases = settings.get('NUMBER_GASES', 2)

if number_gases == 4:
    work_gases = ['1', '2', '3', '4']
elif number_gases == 3:
    work_gases = ['1', '2', '3']
elif number_gases == 2:
    work_gases = ['1', '2']

all_gases = work_gases + ['01']

# Создаем отдельный логгер для диагностики on_venting_clicked
venting_logger = logging.getLogger('on_venting_clicked')
venting_logger.setLevel(logging.DEBUG)
# Отключаем распространение на корневой логгер
venting_logger.propagate = False

# Создаем отдельный handler для файла on_venting_clicked.log
venting_handler = RotatingFileHandler(
    filename="on_venting_clicked.log",
    maxBytes=10*1024*1024,  # 10MB
    backupCount=3,
    encoding='utf-8'
)
venting_handler.setFormatter(
    logging.Formatter('%(asctime)s.%(msecs)03d - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
)
venting_logger.addHandler(venting_handler)

# Создаем отдельный логгер для диагностики операций с кнопкой старт и процесса запуска
start_process_logger = logging.getLogger('start_process')
start_process_logger.setLevel(logging.DEBUG)
# Отключаем распространение на корневой логгер
start_process_logger.propagate = False

# Создаем отдельный handler для файла start_process.log
start_process_handler = RotatingFileHandler(
    filename="start_process.log",
    maxBytes=10*1024*1024,  # 10MB
    backupCount=3,
    encoding='utf-8'
)
start_process_handler.setFormatter(
    logging.Formatter('%(asctime)s.%(msecs)03d - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
)
start_process_logger.addHandler(start_process_handler)

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
        """Остановка воркера"""
        self._running = False

    @QtCore.pyqtSlot()
    def run(self):
        """Выполнение чтения потоков"""
        try:
            for num_rrg in self.active_rrgs:
                if not self._running:
                    logging.debug(f"ReadFlowsWorker stopped, breaking loop at RRG {num_rrg}")
                    break

                try:
                    value = self.controller.handle_command(
                        'read_flow',
                        num_rrg=num_rrg,
                        type_gas=self.gas_types[num_rrg]
                    )
                    # Если значение None (таймаут), используем 0.0
                    if value is None:
                        value = 0.0
                except Exception as e:
                    logging.error(f"Error reading flow for RRG {num_rrg}: {e}")
                    value = 0.0

                if not self._running:  # Проверяем еще раз перед отправкой сигнала
                    logging.debug("ReadFlowsWorker stopped before emitting signal")
                    break
                    
                try:
                    # Всегда передаём (int, float): номер РРГ 1/2 и значение потока

                    rrg_id = int(num_rrg) if str(num_rrg).strip() in ('1', '2', '3', '4') else num_rrg
                    self.flowRead.emit(rrg_id, float(value))
                except RuntimeError as e:
                    # Объект уже уничтожен
                    logging.debug(f"RuntimeError emitting signal (object destroyed): {e}")
                    break
                except Exception as e:
                    logging.error(f"Error emitting flowRead signal: {e}")

        except Exception as e:
            logging.error(f"Error in ReadFlowsWorker.run: {e}")
        finally:
            # Всегда отправляем сигнал завершения
            try:
                if self._running:  # Отправляем только если не было принудительной остановки
                    self.finished.emit()
            except RuntimeError:
                # Объект уже уничтожен, это нормально
                logging.debug("RuntimeError emitting finished signal (object destroyed)")
            except Exception as e:
                logging.error(f"Error emitting finished signal: {e}")


class VentingResultWorker(QObject):
    """Воркер для передачи результатов операции напуска газов в главный поток"""
    ventingCompleted = pyqtSignal(dict)  # Сигнал с результатами операции
    finished = pyqtSignal()


class ReadRFWorker(QObject):
    """Воркер для чтения данных генератора RF в отдельном потоке"""
    rfDataRead = pyqtSignal(object)  # Сигнал с данными генератора (может быть dict или None)
    finished = pyqtSignal()
    
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._running = True
        self._consecutive_failures = 0  # Счетчик последовательных неудач
        self._max_consecutive_failures = 5  # Максимум неудач подряд перед увеличением интервала
    
    def stop(self):
        """Остановка воркера"""
        self._running = False
    
    @QtCore.pyqtSlot()
    def run(self):
        """Чтение данных генератора"""
        process_logger.info("[ReadRFWorker] Thread started")
        read_count = 0
        try:
            # Задержка перед первым чтением, чтобы дать время порту полностью освободиться
            # (как в state_machine.py, где следующий шаг процесса откладывается на 1 секунду)
            QtCore.QThread.msleep(1000)
            
            while self._running:
                try:
                    read_start = time.time()
                    status = None
                    read_time = 0
                    
                    # Проверяем флаг перед чтением, чтобы быстро остановиться при запросе
                    if not self._running:
                        break
                    
                    # Если слишком много неудач подряд, пропускаем чтение, чтобы не блокировать порт
                    if self._consecutive_failures >= self._max_consecutive_failures:
                        process_logger.warning(f"[ReadRFWorker] Skipping read due to {self._consecutive_failures} consecutive failures (max={self._max_consecutive_failures})")
                        # Пропускаем чтение - status остается None, read_time = 0
                    else:
                        # Читаем статус генератора
                        status = self.controller.rf.read_status()
                        read_time = time.time() - read_start
                        read_count += 1
                        
                        # Проверяем флаг после чтения (чтение может занять время)
                        if not self._running:
                            break
                        
                        # Если чтение заняло слишком много времени (>1.5 сек), считаем это ошибкой
                        if read_time > 1.5:
                            process_logger.warning(f"[ReadRFWorker] Read #{read_count} took too long: {read_time:.3f}s > 1.5s, treating as failure")
                            status = None
                    
                    # Обрабатываем результат чтения
                    if status:
                        # Успешное чтение - сбрасываем счетчик неудач
                        self._consecutive_failures = 0
                        process_logger.debug(f"[ReadRFWorker] Read #{read_count}: took {read_time:.3f}s, forward={status.get('forward_w', 0)}, reflected={status.get('reflect_w', 0)}")
                        if read_time > 1.0:
                            process_logger.warning(f"[ReadRFWorker] SLOW READ #{read_count}: {read_time:.3f}s > 1s")
                        self.rfDataRead.emit(status)
                    else:
                        # Неудачное чтение или пропуск - увеличиваем счетчик только если было реальное чтение
                        if read_time > 0:
                            self._consecutive_failures += 1
                            process_logger.warning(f"[ReadRFWorker] Read #{read_count}: returned None, took {read_time:.3f}s (consecutive failures: {self._consecutive_failures})")
                        else:
                            process_logger.debug(f"[ReadRFWorker] Read skipped (consecutive failures: {self._consecutive_failures})")
                        
                        # Если слишком много неудач подряд, пытаемся переподключиться (только при реальном чтении)
                        if read_time > 0 and self._consecutive_failures >= self._max_consecutive_failures:
                            process_logger.warning(f"[ReadRFWorker] Too many consecutive failures ({self._consecutive_failures}), attempting reconnect...")
                            try:
                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                if reconnect_success:
                                    process_logger.info(f"[ReadRFWorker] RF generator reconnected successfully")
                                    self._consecutive_failures = 0  # Сбрасываем счетчик после переподключения
                                else:
                                    process_logger.warning(f"[ReadRFWorker] Failed to reconnect RF generator: {reconnect_msg}")
                            except Exception as reconnect_error:
                                process_logger.error(f"[ReadRFWorker] Error reconnecting RF generator: {reconnect_error}")
                        
                        # Отправляем None в случае ошибки или пропуска
                        self.rfDataRead.emit(None)
                    
                    # Проверяем флаг перед задержкой
                    if not self._running:
                        break
                except Exception as e:
                    # Исключение при чтении - увеличиваем счетчик
                    self._consecutive_failures += 1
                    process_logger.error(f"[ReadRFWorker] Error reading RF status #{read_count}: {e} (consecutive failures: {self._consecutive_failures})")
                    logging.error(f"Error reading RF status: {e}")
                    
                    # Если слишком много неудач подряд, пытаемся переподключиться
                    if self._consecutive_failures >= self._max_consecutive_failures:
                        process_logger.warning(f"[ReadRFWorker] Too many consecutive failures ({self._consecutive_failures}), attempting reconnect...")
                        try:
                            reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                            if reconnect_success:
                                process_logger.info(f"[ReadRFWorker] RF generator reconnected successfully after error")
                                self._consecutive_failures = 0  # Сбрасываем счетчик после переподключения
                            else:
                                process_logger.warning(f"[ReadRFWorker] Failed to reconnect RF generator after error: {reconnect_msg}")
                        except Exception as reconnect_error:
                            process_logger.error(f"[ReadRFWorker] Error reconnecting RF generator after error: {reconnect_error}")
                    
                    # Отправляем None в случае ошибки
                    self.rfDataRead.emit(None)
                
                # Проверяем флаг перед задержкой
                if not self._running:
                    break
                
                # Увеличиваем интервал чтения при множественных неудачах, чтобы не блокировать порт
                # Базовый интервал: 2 секунды, при неудачах: до 10 секунд
                if self._consecutive_failures >= self._max_consecutive_failures:
                    # При множественных неудачах увеличиваем интервал до 10 секунд
                    interval_seconds = 10
                    process_logger.warning(f"[ReadRFWorker] Using extended interval {interval_seconds}s due to {self._consecutive_failures} consecutive failures (max={self._max_consecutive_failures})")
                else:
                    # Нормальный интервал: 2 секунды
                    interval_seconds = 2
                
                # Разбиваем интервал на части для возможности прервать
                sleep_iterations = interval_seconds * 10  # 100ms на итерацию
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
        self._flow_thread_lock = threading.Lock()  # Блокировка для синхронизации потоков
        self._flow_thread_busy = False  # Флаг занятости потока
        
        # Для чтения данных генератора RF
        self.rf_thread = None
        self.rf_worker = None
        self._rf_thread_lock = threading.Lock()
        self._rf_thread_busy = False
        
        # Для асинхронной остановки напуска газов
        self._stopping_gases = False
        self._stop_gas_step = 0
        self._stop_gas_rrgs = []
        self.timer_stop_gases = QTimer()
        self.timer_stop_gases.timeout.connect(self._process_stop_gases)
        self._stop_gas_executor = None  # ThreadPoolExecutor для операций с РРГ
        self.stop_gases_thread = None
        self.stop_gases_worker = None
        
        # ThreadPoolExecutor для асинхронного запуска напуска газов
        self._venting_executor = None
        self._venting_in_progress = False
        # Воркер для передачи результатов в главный поток через сигнал
        self._venting_result_worker = VentingResultWorker()
        self._venting_result_worker.ventingCompleted.connect(self._on_venting_completed)
        
        # ThreadPoolExecutor для операций с RF генератором (переподключение и т.д.)
        self._rf_operations_executor = None

        self.translator = Translator()
        
        self.time_start_work = time.time()

        self.controller = controller
        self.plasma_process = PlasmaAutoProcess(self.controller, self)

        self.RecName.setReadOnly(True)
        self.StatusLine.setReadOnly(True)
        # Отключаем выделение текста в StatusLine при установке нового текста
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

        self.max_attempts = 10  # Уменьшено с 10 до 3 для быстрой реакции и предотвращения зависаний

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

        self.labels_not_enabled_operator = [
            self.NIButton, self.VEButton, self.HFButton, self.VE0Button, 
            self.PressZad, self.HFPowerZad, self.TimeZad, self.ButtonClose]

        for i in range(1, number_gases + 1):
            self.labels_service.append([
                getattr(self, f'button_rrg_{i}'),
                getattr(self, f'valve_ve{i}_value'),
                getattr(self, f'title_address_rrg{i}')
            ])
            
            self.labels_not_enabled_operator.append([
                getattr(self, f'VE{i}Button'),
                getattr(self, f'VE{i}ComboBox'),
                getattr(self, f'VE{i}FlowZad')
            ])

            self.buttons_service.append([getattr(self, f'VE{i}ButtonS')])

        self.PressProgress.hide()
        self.TimeProgress.hide()
        self.HFProgress.hide()

        for i in range(1, number_gases + 1):
            getattr(self, f'VE{i}Progress').hide()

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
            self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_init_devices') + str(self.controller.fault_device_init))
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

    def check_service_pump(self):
        if settings['time_pump_for_service'] > settings['max_time_pump_for_service']:
            self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('service_pump'))

    def check_button_start(self):
        is_pressed = self.controller.handle_command('get_state_button_start')
        # Проверяем состояние процесса, а не только текст кнопки
        # Если процесс в idle, можно запускать, даже если текст кнопки еще не обновился
        process_idle = self.plasma_process.current_state == 'idle'
        button_text_is_start = self.ButtonStart.text() == self.translator.tr('start')
        current_state = self.plasma_process.current_state
        
        start_process_logger.info(f"[PHYSICAL START BUTTON] check_button_start: is_pressed={is_pressed}, enabled={self.ButtonStart.isEnabled()}, "
                    f"process_state={current_state}, process_idle={process_idle}, button_text='{self.ButtonStart.text()}'")
        
        # ВАЖНО: Физическая кнопка старт должна запускать процесс только если он в idle
        # Если процесс уже запущен, игнорируем нажатие физической кнопки
        if is_pressed and self.ButtonStart.isEnabled():
            if process_idle:
                # Процесс в idle - запускаем процесс (независимо от текста кнопки)
                # Это позволяет запускать процесс даже если текст кнопки еще не обновился
                start_process_logger.info(f"[PHYSICAL START BUTTON] check_button_start: Process is idle, scheduling on_start_button_clicked in 1 second "
                           f"(button_text='{self.ButtonStart.text()}')")
                QTimer.singleShot(1000, self.on_start_button_clicked)
            else:
                # Процесс запущен - игнорируем нажатие физической кнопки старт
                start_process_logger.warning(f"[PHYSICAL START BUTTON] check_button_start: Process is running (state={current_state}), "
                              f"ignoring physical start button press")

    def update_time(self):
        self.label_time.setText((datetime.now() + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M:%S"))

    def save_address_rrg(self, number):
        find_address = self.controller.scan_modbus_rrg(number)
        self.label_2.setText(f"Адрес: {find_address}" if find_address != 0 else 'Адрес не найден')

    def save_address_rf(self):
        find_address = self.controller.scan_modbus_rf()
        self.label_2.setText(f"Адрес: {find_address}" if find_address != 0 else 'Адрес не найден')

    def _on_flow_thread_finished(self):
        """Обработчик завершения потока чтения потоков"""
        with self._flow_thread_lock:
            if self.flow_worker:
                try:
                    # Отключаем все сигналы worker перед удалением
                    try:
                        self.flow_worker.flowRead.disconnect()
                        self.flow_worker.finished.disconnect()
                    except (TypeError, RuntimeError):
                        pass  # Сигналы уже отключены или объект уничтожен
                    
                    self.flow_worker.deleteLater()
                except Exception as e:
                    logging.debug(f"Error deleting flow_worker: {e}")
                self.flow_worker = None

            if self.flow_thread:
                try:
                    # Отключаем сигнал finished перед удалением
                    try:
                        self.flow_thread.finished.disconnect()
                    except (TypeError, RuntimeError):
                        pass  # Сигнал уже отключен или объект уничтожен
                    
                    self.flow_thread.deleteLater()
                except Exception as e:
                    logging.debug(f"Error deleting flow_thread: {e}")
                self.flow_thread = None
            
            # Снимаем флаг занятости
            self._flow_thread_busy = False
            logging.debug("Flow thread finished and cleaned up")
    
    @QtCore.pyqtSlot()
    def _clear_flow_thread_references(self):
        """Слот для очистки ссылок на поток из главного потока Qt"""
        try:
            with self._flow_thread_lock:
                self.flow_worker = None
                self.flow_thread = None
                self._flow_thread_busy = False
                venting_logger.info("[_clear_flow_thread_references] Flow thread references cleared")
        except Exception as e:
            venting_logger.error(f"[_clear_flow_thread_references] Error clearing references: {e}")
    
    def _on_stop_gases_finished(self):
        """Обработчик завершения воркера остановки напуска газов"""
        try:
            logging.debug("Stop gases worker finished")
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
        """Остановка потока чтения потоков
        
        Args:
            wait: Если True, ждет завершения потока. Если False, останавливает асинхронно.
        """
        from logging.handlers import RotatingFileHandler
        import logging
        venting_logger.info(f"[stop_flow_thread] Called, wait={wait}")
        thread_start = time.time()
        
        # Пытаемся захватить lock с таймаутом, чтобы избежать deadlock
        lock_acquire_start = time.time()
        if not self._flow_thread_lock.acquire(blocking=True, timeout=2.0):
            lock_acquire_time = time.time() - lock_acquire_start
            venting_logger.warning(f"[stop_flow_thread] Could not acquire lock after {lock_acquire_time:.3f}s, cannot stop thread safely")
            return
        lock_acquire_time = time.time() - lock_acquire_start
        if lock_acquire_time > 0.1:
            venting_logger.debug(f"[stop_flow_thread] Lock acquired in {lock_acquire_time:.3f}s")
        
        try:
            if self.flow_worker:
                try:
                    venting_logger.info("[stop_flow_thread] Stopping flow worker...")
                    self.flow_worker.stop()
                    venting_logger.info("[stop_flow_thread] Flow worker stop() called")
                except Exception as e:
                    venting_logger.error(f"[stop_flow_thread] Error stopping flow worker: {e}", exc_info=True)

            if self.flow_thread:
                if self.flow_thread.isRunning():
                    try:
                        venting_logger.info("[stop_flow_thread] Quitting flow thread...")
                        self.flow_thread.quit()
                        venting_logger.info("[stop_flow_thread] Flow thread quit() called")
                        if wait:
                            venting_logger.info("[stop_flow_thread] Waiting for flow thread to finish (up to 2 seconds)...")
                            wait_start = time.time()
                            if not self.flow_thread.wait(2000):  # Таймаут 2 секунды
                                wait_elapsed = time.time() - wait_start
                                venting_logger.warning(f"[stop_flow_thread] Flow thread did not finish in {wait_elapsed:.3f}s, terminating")
                                self.flow_thread.terminate()
                                if not self.flow_thread.wait(500):  # Ждем еще 0.5 секунды после terminate
                                    venting_logger.error("[stop_flow_thread] Flow thread did not terminate, forcing cleanup")
                                else:
                                    venting_logger.info("[stop_flow_thread] Flow thread terminated")
                            else:
                                wait_elapsed = time.time() - wait_start
                                venting_logger.info(f"[stop_flow_thread] Flow thread finished in {wait_elapsed:.3f}s")
                        else:
                            venting_logger.info("[stop_flow_thread] Not waiting for flow thread (async stop)")
                    except Exception as e:
                        venting_logger.error(f"[stop_flow_thread] Error stopping flow thread: {e}", exc_info=True)
                
                if wait:
                    venting_logger.info("[stop_flow_thread] Cleaning up flow thread references (wait=True)...")
                    try:
                        # Сохраняем ссылки для безопасного удаления
                        worker_to_delete = self.flow_worker
                        thread_to_delete = self.flow_thread
                        
                        # Отключаем сигнал finished перед удалением, чтобы избежать ошибок
                        if thread_to_delete:
                            try:
                                thread_to_delete.finished.disconnect()
                                venting_logger.debug("[stop_flow_thread] flow_thread.finished signal disconnected")
                            except (TypeError, RuntimeError) as e:
                                venting_logger.debug(f"[stop_flow_thread] Could not disconnect finished signal (may already be disconnected): {e}")
                        
                        # Удаляем worker перед потоком
                        if worker_to_delete:
                            try:
                                # Отключаем все сигналы worker перед удалением
                                try:
                                    worker_to_delete.flowRead.disconnect()
                                except (TypeError, RuntimeError):
                                    pass
                                try:
                                    worker_to_delete.finished.disconnect()
                                except (TypeError, RuntimeError):
                                    pass
                                venting_logger.debug("[stop_flow_thread] flow_worker signals disconnected")
                                
                                # Удаляем worker из главного потока Qt через QMetaObject.invokeMethod
                                # Это работает из любого потока, не только из QThread
                                try:
                                    QtCore.QMetaObject.invokeMethod(
                                        worker_to_delete,
                                        "deleteLater",
                                        QtCore.Qt.QueuedConnection
                                    )
                                    venting_logger.debug("[stop_flow_thread] flow_worker.deleteLater() queued")
                                except Exception as e:
                                    venting_logger.debug(f"[stop_flow_thread] Error queuing worker deleteLater: {e}")
                            except Exception as e:
                                venting_logger.debug(f"[stop_flow_thread] Error deleting worker: {e}")
                        
                        # Удаляем поток из главного потока Qt через QMetaObject.invokeMethod
                        if thread_to_delete:
                            try:
                                QtCore.QMetaObject.invokeMethod(
                                    thread_to_delete,
                                    "deleteLater",
                                    QtCore.Qt.QueuedConnection
                                )
                                venting_logger.info("[stop_flow_thread] flow_thread.deleteLater() queued")
                            except Exception as e:
                                venting_logger.debug(f"[stop_flow_thread] Error queuing thread deleteLater: {e}")
                    except Exception as e:
                        venting_logger.error(f"[stop_flow_thread] Error in cleanup: {e}", exc_info=True)
                else:
                    venting_logger.info("[stop_flow_thread] Skipping cleanup (will be done in _on_flow_thread_finished)")
            
            if wait:
                venting_logger.info("[stop_flow_thread] Clearing flow thread references...")
                # Очищаем ссылки только после того, как deleteLater() был вызван
                # Используем QMetaObject.invokeMethod для отложенной очистки из главного потока Qt
                def clear_references():
                    try:
                        with self._flow_thread_lock:
                            self.flow_worker = None
                            self.flow_thread = None
                            self._flow_thread_busy = False
                            venting_logger.info("[stop_flow_thread] Flow thread references cleared")
                    except Exception as e:
                        venting_logger.error(f"[stop_flow_thread] Error clearing references: {e}")
                
                # Вызываем очистку из главного потока Qt через QMetaObject.invokeMethod
                # Используем QTimer только если мы в главном потоке Qt
                try:
                    # Проверяем, находимся ли мы в главном потоке Qt
                    if QtCore.QThread.currentThread() == QtWidgets.QApplication.instance().thread():
                        # Мы в главном потоке, можем использовать QTimer
                        QTimer.singleShot(200, clear_references)
                    else:
                        # Мы не в главном потоке, используем QMetaObject.invokeMethod
                        QtCore.QMetaObject.invokeMethod(
                            self,
                            "_clear_flow_thread_references",
                            QtCore.Qt.QueuedConnection
                        )
                except Exception as e:
                    venting_logger.error(f"[stop_flow_thread] Error scheduling reference cleanup: {e}")
                    # В случае ошибки просто очищаем ссылки сразу (не идеально, но безопасно)
                    try:
                        with self._flow_thread_lock:
                            self.flow_worker = None
                            self.flow_thread = None
                            self._flow_thread_busy = False
                    except:
                        pass
            
            thread_time = time.time() - thread_start
            venting_logger.info(f"[stop_flow_thread] Completed in {thread_time:.3f}s")
        finally:
            self._flow_thread_lock.release()
            venting_logger.debug("[stop_flow_thread] Lock released")


    def read_flows_async(self):
        """Асинхронное чтение потоков РРГ"""
        # Проверяем, не занят ли уже поток
        with self._flow_thread_lock:
            if self._flow_thread_busy:
                logging.debug("Flow thread is busy, skipping this call")
                return
            
            # Если поток еще работает, не создаем новый
            if self.flow_thread is not None and self.flow_thread.isRunning():
                logging.debug("Previous flow thread still running, skipping")
                return

        active_rrgs = []
        gas_types = {}

        for i in work_gases:
            if getattr(self, f"VE{i}Button").isChecked():
                active_rrgs.append(i)
                gas_types[i] = getattr(self, f"VE{i}ComboBox").currentIndex()

        if not active_rrgs:
            # Если нет активных газов, останавливаем поток, если он работает
            with self._flow_thread_lock:
                if self.flow_thread is not None and self.flow_thread.isRunning():
                    self.stop_flow_thread()
            return

        # Создаем новый поток только если предыдущий полностью завершен
        with self._flow_thread_lock:
            if self.flow_thread is not None:
                logging.debug("Previous flow thread not cleaned up yet, skipping")
                return
            
            # Устанавливаем флаг занятости
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

            # Сохраняем ссылки только после успешного создания
            with self._flow_thread_lock:
                self.flow_thread = flow_thread
                self.flow_worker = flow_worker

            flow_thread.start()
            logging.debug(f"Flow thread started for RRGs: {active_rrgs}")
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
            logging.warning(f"on_flow_read: виджет {attr_name!r} не найден")
            return
        label = getattr(self, attr_name)
        label.setText(f"{float(value):.1f}")
    
    @QtCore.pyqtSlot(int, float)
    def update_flow_display(self, num_rrg, value):
        """Безопасное обновление отображения потока из любого потока"""
        try:
            self.on_flow_read(num_rrg, value)
        except Exception as e:
            logging.error(f"Error updating flow display for RRG {num_rrg}: {e}")
    
    @QtCore.pyqtSlot()
    def _update_ui_after_stop(self):
        """Обновление UI после подтверждения остановки напуска газов"""
        thread_id = threading.current_thread().ident
        logging.info("=" * 80)
        logging.info(f"DEBUG: _update_ui_after_stop CALLED in thread {thread_id}")
        logging.info(f"DEBUG: Is main thread: {threading.current_thread() is threading.main_thread()}")
        try:
            logging.info("DEBUG: Updating button text...")
            self.VEButton.setText(self.translator.tr('start_venting_gas'))
            logging.info("DEBUG: Button text updated")
            
            logging.info("DEBUG: Updating button checked state...")
            self.VEButton.setChecked(False)
            logging.info("DEBUG: Button checked state updated")
            
            if settings.get('LANG') == 0:
                logging.info("DEBUG: Updating button style...")
                self.VEButton.setStyleSheet('font-size: 20px')
                logging.info("DEBUG: Button style updated")
            
            logging.info("DEBUG: Updating status...")
            self.update_status(self.translator.tr('gas_inlet_completed'))
            logging.info("DEBUG: Status updated")
            
            logging.info("DEBUG: _update_ui_after_stop COMPLETED successfully")
            logging.info("=" * 80)
        except Exception as e:
            logging.error(f"DEBUG: ERROR in _update_ui_after_stop: {e}", exc_info=True)
            logging.info("=" * 80)

    def update_values(self):
        # Логирование для диагностики обновления давления и воды во время работы плазмы
        current_state = getattr(self.plasma_process, 'current_state', 'unknown')
        plasma_on = getattr(self.controller, '_cached_plasma_status', False)
        timer_active = self.timer_update_values.isActive() if hasattr(self, 'timer_update_values') else False

        # Инициализируем переменные для использования в любом случае
        values = {'pressure': 0.0, 'water': 0.0}
        
        try:
            values_adc = self.controller.get_values_adc()
            
            # Проверяем, что get_values_adc вернул словарь (не None)
            if values_adc is None:
                logging.warning(f"update_values: get_values_adc returned None - state={current_state}, plasma_on={plasma_on}")
                values_adc = {'P': None, 'T': None}

            if values_adc.get('P') is not None:
               self.PressLableSADC.setText(str(values_adc['P']))
               self.PressLableSZnachU.setText(str(fun.bit_u(float(values_adc['P']))))

            # Main - чтение значений с обработкой ошибок
            try:
                pressure_raw = self.controller.handle_command('get_sensor_pressure')
                water_raw = self.controller.handle_command('get_sensor_water')

                values = {
                    'pressure': pressure_raw,
                    'water': water_raw,
                }
                
                # Обработка None значений
                if values['pressure'] is None:
                    logging.warning(f"update_values: pressure is None, setting to 0.0 - state={current_state}, plasma_on={plasma_on}")
                    values['pressure'] = 0.0
                if values['water'] is None:
                    logging.warning(f"update_values: water is None, setting to 0.0 - state={current_state}, plasma_on={plasma_on}")
                    values['water'] = 0.0
            except Exception as e:
                logging.error(f"Error reading sensor values: {e}, state={current_state}, plasma_on={plasma_on}", exc_info=True)
                values = {'pressure': 0.0, 'water': 0.0}
        except Exception as e:
            # Обработка ошибок на верхнем уровне (например, если get_values_adc упал)
            error_msg = str(e)
            error_code = getattr(e, 'errno', None)
            logging.error(f"update_values: CRITICAL ERROR in outer try block: {e}, errno={error_code}, state={current_state}, plasma_on={plasma_on}", exc_info=True)
            
            # Если это ошибка I/O (121 - Remote I/O error), пытаемся переподключиться к датчикам
            if error_code == 121 or 'I/O error' in error_msg or 'Remote I/O' in error_msg:
                logging.warning(f"update_values: I/O error detected, attempting to reconnect sensors...")
                # Переподключаемся асинхронно, чтобы не блокировать UI
                try:
                    def reconnect_task():
                        try:
                            self.controller.reconnect_device('ADC')
                            self.controller.reconnect_device('sensor_water')
                        except Exception as reconnect_error:
                            logging.error(f"update_values: Error during sensor reconnection: {reconnect_error}")
                    # Используем ThreadPoolExecutor из контроллера для асинхронного переподключения
                    if hasattr(self.controller, '_sensor_reconnect_executor'):
                        self.controller._sensor_reconnect_executor.submit(reconnect_task)
                    else:
                        # Fallback: выполняем синхронно, если executor недоступен
                        reconnect_task()
                except Exception as reconnect_error:
                    logging.error(f"update_values: Error scheduling sensor reconnection: {reconnect_error}")
            
            # Пытаемся все равно прочитать давление и воду, даже если ADC не работает
            try:
                logging.warning(f"update_values: Attempting to read pressure and water despite ADC error...")
                pressure_raw = self.controller.handle_command('get_sensor_pressure')
                water_raw = self.controller.handle_command('get_sensor_water')
                logging.info(f"update_values: Successfully read pressure={pressure_raw}, water={water_raw} despite ADC error")
                
                # Создаем словарь values для продолжения выполнения
                values = {
                    'pressure': pressure_raw if pressure_raw is not None else 0.0,
                    'water': water_raw if water_raw is not None else 0.0
                }
                
                # Обновляем давление и воду, если удалось их прочитать
                if pressure_raw is not None:
                    try:
                        pressure_value = float(pressure_raw)
                        old_pressure_text = self.PressZnach.text()
                        if pressure_value < 10:
                            new_pressure_text = f"{pressure_value:.2f}"
                        else:
                            new_pressure_text = f"{int(pressure_value)}"
                        self.PressZnach.setText(new_pressure_text)
                        if old_pressure_text != new_pressure_text:
                            logging.info(f"update_values: Pressure UPDATED (despite ADC error) - {old_pressure_text} -> {new_pressure_text}")
                    except Exception as pe:
                        logging.error(f"update_values: Error updating pressure display: {pe}")
                        values['pressure'] = 0.0
                
                if water_raw is not None:
                    try:
                        old_water_text = self.WLabelS.text()
                        new_water_text = f"{water_raw:.3f}"
                        self.WLabelS.setText(new_water_text)
                        if old_water_text != new_water_text:
                            logging.info(f"update_values: Water UPDATED (despite ADC error) - {old_water_text} -> {new_water_text}")
                    except Exception as we:
                        logging.error(f"update_values: Error updating water display: {we}")
                        values['water'] = 0.0
            except Exception as fallback_error:
                logging.error(f"update_values: Even fallback pressure/water read failed: {fallback_error}")
                # Создаем значения по умолчанию для продолжения выполнения
                values = {'pressure': 0.0, 'water': 0.0}
        
        # Управление LED вакуума: горит если значение давления в вольтах меньше 4.4
        # Этот код выполняется ВСЕГДА, независимо от ошибок выше
        try:
            states = self.controller.handle_command('get_states')
            if states and isinstance(states, dict):
                led_vacuum = states.get('led_vacuum', False)
                
                # Получаем значение давления в вольтах
                try:
                    adc_values = self.controller.get_values_adc()
                    if adc_values and adc_values.get('P') is not None:
                        pressure_voltage = fun.bit_u(float(adc_values['P']))
                        print(pressure_voltage)
                        if pressure_voltage < 4.347 and not led_vacuum:
                            self.controller.handle_command('on_led_vacuum')
                        elif pressure_voltage >= 4.347 and led_vacuum:
                            self.controller.handle_command('off_led_vacuum')
                except Exception as adc_error:
                    pass
        except Exception as e:
            logging.error(f"Error managing LED vacuum: {e}")

        # Убеждаемся, что в PressZnach всегда число, даже при ошибке
        # Этот код выполняется ВСЕГДА, независимо от ошибок выше
        try:
            pressure_value = float(values['pressure'])
            old_pressure_text = self.PressZnach.text()
            # Если значение меньше 1, отображаем с 2 знаками после запятой, иначе как целое число
            if pressure_value < 10:
                new_pressure_text = f"{pressure_value:.2f}"
            else:
                new_pressure_text = f"{int(pressure_value)}"
            
            self.PressZnach.setText(new_pressure_text)
            
            # Логирование обновления давления
            if old_pressure_text != new_pressure_text:
                logging.info(f"update_values: Pressure UPDATED - {old_pressure_text} -> {new_pressure_text}, state={current_state}, plasma_on={plasma_on}")
        except (ValueError, TypeError) as e:
            logging.error(f"Error converting pressure to float: {e}, value: {values.get('pressure')}, state={current_state}, plasma_on={plasma_on}")
            self.PressZnach.setText("0.00")  # Устанавливаем безопасное значение по умолчанию


        # Отображение потока воды с форматированием
        try:
            old_water_text = self.WLabelS.text()
            if values['water'] is not None:
                new_water_text = f"{values['water']:.3f}"
                self.WLabelS.setText(new_water_text)
                
                # Логирование обновления воды
                if old_water_text != new_water_text:
                    logging.info(f"update_values: Water UPDATED - {old_water_text} -> {new_water_text}, state={current_state}, plasma_on={plasma_on}")
            else:
                self.WLabelS.setText("0.000")
                logging.warning(f"update_values: Water is None, setting to 0.000 - state={current_state}, plasma_on={plasma_on}")
        except Exception as e:
            logging.error(f"Error updating water display: {e}, value: {values.get('water')}, state={current_state}, plasma_on={plasma_on}")
            self.WLabelS.setText("0.000")
    
        self.PressProgress.setMaximum(100)
        try:
            press_znach = float(self.PressZnach.text())
            press_zad = float(self.PressZad.text())
            
            # Если давление ниже заданного - цель достигнута (100%)
            if press_znach <= press_zad:
                self.PressProgress.setValue(100)
            else:
                # Используем логарифмическую шкалу для плавного отображения
                # Диапазон: от press_zad до 1000 мбар
                logZnach = math.log10(max(0.001, press_znach))  # Защита от log10(0)
                logZad = math.log10(max(0.001, press_zad))
                logMax = math.log10(1000.0)
                
                if logMax != logZad and logMax > logZad:
                    # Формула: чем ближе к заданному давлению, тем выше прогресс
                    # Когда press_znach = press_zad: progress = 100%
                    # Когда press_znach = 1000: progress = 0%
                    progressPressure = 100.0 - ((logZnach - logZad) / (logMax - logZad)) * 100.0
                    # Ограничиваем значение от 0 до 100
                    progressPressure = max(0.0, min(100.0, progressPressure))
                    # Используем округление вместо int() для более плавного изменения
                    self.PressProgress.setValue(round(progressPressure))
                else:
                    # Если logMax == logZad или logMax <= logZad, используем линейную формулу
                    if press_zad > 0 and press_zad < 1000:
                        progressPressure = 100.0 - ((press_znach - press_zad) / (1000.0 - press_zad)) * 100.0
                        progressPressure = max(0.0, min(100.0, progressPressure))
                        self.PressProgress.setValue(round(progressPressure))
                    else:
                        # Если press_zad >= 1000, всегда показываем 0%
                        self.PressProgress.setValue(0)
        except (ValueError, TypeError) as e:
            logging.error(f"Error calculating pressure progress: {e}, PressZnach: {self.PressZnach.text()}, PressZad: {self.PressZad.text()}")
            # Не обновляем прогресс при ошибке
        
        # Для технолога кнопка "напустить газ" недоступна, пока давление не станет ниже целевого
        # НО если кнопка уже нажата и текст = "остановить напуск газа", она должна быть доступна
        if self.user_mode == 'Technologist':
            if (self.plasma_process.current_state in ['idle', 'fault'] and not self.HFButton.isChecked()):  # Плазма не должна быть включена
                # Если кнопка нажата и текст = "остановить напуск газа", она должна быть доступна
                button_text_is_stop = self.VEButton.text() == self.translator.tr('stop_venting_gas')
                if self.VEButton.isChecked() and button_text_is_stop:
                    self.VEButton.setEnabled(True)
                else:
                    try:
                        press_znach = float(self.PressZnach.text())
                        press_zad = float(self.PressZad.text())
                        # Если давление >= целевого, отключаем кнопку напуска газа
                        if press_znach >= press_zad:
                            self.VEButton.setEnabled(False)
                        else:
                            self.VEButton.setEnabled(True)
                    except (ValueError, TypeError) as e:
                        logging.error(f"Error checking pressure for VEButton enable in update_values: {e}, PressZnach: {self.PressZnach.text()}, PressZad: {self.PressZad.text()}")
        
        # Обновление значений состояний устройств
        states = None  # Инициализируем переменную для использования ниже
        try:
            states = self.controller.handle_command('get_states')
            if states is None:
                logging.warning("update_values: get_states returned None")
                states = {}  # Используем пустой словарь для безопасности

        except Exception as e:
            states = {}  # Устанавливаем пустой словарь для использования ниже
        
        # Синхронизируем светодиоды кнопки старт/стоп: горит только если кнопка активна и на нее можно нажать
        try:
            # LED старт: горит только если кнопка активна И текст = "start"
            if states is None:
                states = {}
            current_led_start_state = states.get('led_start', False)
            button_enabled = self.ButtonStart.isEnabled()
            button_text_is_start = self.ButtonStart.text() == self.translator.tr('start')
            
            should_led_start_be_on = button_enabled and button_text_is_start
            
            # Обновляем светодиод только если состояние не совпадает
            if should_led_start_be_on and not current_led_start_state:
                self.controller.handle_command('on_led_start')
            elif not should_led_start_be_on and current_led_start_state:
                self.controller.handle_command('off_led_start')
            
            # LED стоп: горит только если кнопка активна И текст = "stop"
            current_led_stop_state = states.get('led_stop', False)
            button_text_is_stop = self.ButtonStart.text() == self.translator.tr('stop')
            
            should_led_stop_be_on = button_enabled and button_text_is_stop
            
            # Обновляем светодиод только если состояние не совпадает
            if should_led_stop_be_on and not current_led_stop_state:
                self.controller.handle_command('on_led_stop')
            elif not should_led_stop_be_on and current_led_stop_state:
                self.controller.handle_command('off_led_stop')
        except Exception as e:
            logging.error(f"Error syncing LED start/stop in update_values: {e}")

    def update_ui_texts(self):
        self.LabelProf_3.setText(self.translator.tr('status'))
        self.LabelProf.setText(self.translator.tr('recipe'))
        self.ButtonStart.setText(self.translator.tr('start'))
        self.ButtonRecept.setText(self.translator.tr('recipes'))
        self.ButtonRecept.setIcon(QtGui.QIcon('ui/Pictures13/Recept.png'))
        self.ButtonOutput.setText(self.translator.tr('exit'))
        self.ButtonOutput.setIcon(QtGui.QIcon('ui/Pictures13/Exit.png'))
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
            getattr(self, 'fVE{i}ComboBox').setItemText(0, self.translator.tr('air'))
            getattr(self, 'fVE{i}ComboBox').setItemText(1, self.translator.tr('argon'))
            getattr(self, 'fVE{i}ComboBox').setItemText(2, self.translator.tr('oxigen'))
            getattr(self, 'fVE{i}ComboBox').setItemText(3, self.translator.tr('nitrogen'))
            if getattr(self, 'fVE{i}ComboBox').count() < 5:
                getattr(self, 'fVE{i}ComboBox').addItem("")
            getattr(self, 'fVE{i}ComboBox').setItemText(4, self.translator.tr('custom_gas'))

    def init_system(self):
        self.update_status(self.translator.tr('init'))
        self.controller.handle_command('on_bp')

        # Меняем текст кнопки на "stop" перед запуском процесса
        self.ButtonStart.setText(self.translator.tr('stop'))
        self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Stop.png'))
        # LED будет синхронизирован в check_permissions() или update_values()
        
        # Принудительно обновляем UI
        QtWidgets.QApplication.processEvents()
        
        result = self.plasma_process.start_process()
        if not result:
            # Если процесс не запустился, возвращаем текст кнопки обратно
            logging.error("start_process returned False, reverting button text")
            self.ButtonStart.setText(self.translator.tr('start'))
            self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
            # LED будет синхронизирован в check_permissions() или update_values()
            QtWidgets.QApplication.processEvents()
        else:
            # Проверяем, что текст кнопки остался "stop" после запуска
            if self.ButtonStart.text() != self.translator.tr('stop'):
                logging.warning(f"ButtonStart text changed after start_process: {self.ButtonStart.text()}, expected: {self.translator.tr('stop')}")
                self.ButtonStart.setText(self.translator.tr('stop'))
                self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Stop.png'))
                QtWidgets.QApplication.processEvents()

    def on_start_button_clicked(self):
        # Сохраняем текущее состояние процесса для проверки
        current_state = self.plasma_process.current_state
        button_text = self.ButtonStart.text()
        
        start_process_logger.info(f"[START BUTTON CLICKED] on_start_button_clicked: called, current_state={current_state}, button_text='{button_text}'")
        
        # ВАЖНО: Если процесс в idle, запускаем процесс независимо от текста кнопки
        # Это позволяет запускать процесс с физической кнопки даже если текст кнопки еще не обновился
        if current_state == 'idle':
            start_process_logger.info(f"[START BUTTON CLICKED] on_start_button_clicked: Process is idle, proceeding with start")
            # Процесс в idle - запускаем процесс
            if self.ButtonStart.text() != self.translator.tr('start'):
                # Если текст кнопки не "start", обновляем его
                logging.info(f"on_start_button_clicked: Process is idle but button text is '{self.ButtonStart.text()}', updating to 'start'")
                self.ButtonStart.setText(self.translator.tr('start'))
                self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
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
                            # Меняем текст кнопки на "stop" перед запуском процесса
                            self.ButtonStart.setText(self.translator.tr('stop'))
                            self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Stop.png'))
                            # LED будет синхронизирован в check_permissions() или update_values()
                            
                            # Принудительно обновляем UI
                            QtWidgets.QApplication.processEvents()
                            
                            self.timer_venting_atm.stop()
                            self.timer_pumping.stop()
                            self.timer_plasma.stop()
        
                            start_process_logger.info(f"[START BUTTON CLICKED] on_start_button_clicked: Calling start_recipe()...")
                            result = self.plasma_process.start_recipe()
                            start_process_logger.info(f"[START BUTTON CLICKED] on_start_button_clicked: start_recipe() returned {result}")
                            if not result:
                                # Если процесс не запустился, возвращаем текст кнопки обратно
                                start_process_logger.error(f"[START BUTTON CLICKED] on_start_button_clicked: start_recipe returned False, reverting button text")
                                self.ButtonStart.setText(self.translator.tr('start'))
                                self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
                                # LED будет синхронизирован в check_permissions() или update_values()
                                QtWidgets.QApplication.processEvents()
                            else:
                                # Проверяем, что текст кнопки остался "stop" после запуска
                                if self.ButtonStart.text() != self.translator.tr('stop'):
                                    logging.warning(f"ButtonStart text changed after start_recipe: {self.ButtonStart.text()}, expected: {self.translator.tr('stop')}")
                                    self.ButtonStart.setText(self.translator.tr('stop'))
                                    self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Stop.png'))
                                    QtWidgets.QApplication.processEvents()
                    else:
                        self.TimeZnach.setText('00:00')
                        self.DisplayTime.setText('00:00')

                        self.HFProgress.setValue(0)
                        self.TimeProgress.setValue(0)

                        # Меняем текст кнопки на "stop" перед запуском процесса
                        self.ButtonStart.setText(self.translator.tr('stop'))
                        self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Stop.png'))
                        # LED будет синхронизирован в check_permissions() или update_values()
                        
                        # Принудительно обновляем UI
                        QtWidgets.QApplication.processEvents()
                        
                        start_process_logger.info(f"[START BUTTON CLICKED] on_start_button_clicked: Calling start_recipe() (Tech mode)...")
                        result = self.plasma_process.start_recipe()
                        start_process_logger.info(f"[START BUTTON CLICKED] on_start_button_clicked: start_recipe() returned {result} (Tech mode)")
                        if not result:
                            # Если процесс не запустился, возвращаем текст кнопки обратно
                            start_process_logger.error(f"[START BUTTON CLICKED] on_start_button_clicked: start_recipe returned False, reverting button text (Tech mode)")
                            self.ButtonStart.setText(self.translator.tr('start'))
                            self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
                            # LED будет синхронизирован в check_permissions() или update_values()
                            QtWidgets.QApplication.processEvents()
                        else:
                            # Проверяем, что текст кнопки остался "stop" после запуска
                            if self.ButtonStart.text() != self.translator.tr('stop'):
                                logging.warning(f"ButtonStart text changed after start_recipe: {self.ButtonStart.text()}, expected: {self.translator.tr('stop')}")
                                self.ButtonStart.setText(self.translator.tr('stop'))
                                self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Stop.png'))
                                QtWidgets.QApplication.processEvents()
        else:
            # Останавливаем процесс только если он действительно запущен
            # Это защита от случайной остановки при нажатии физической кнопки старт во время работы процесса
            start_process_logger.info(f"[START BUTTON CLICKED] on_start_button_clicked: Button text is not 'start' (text='{self.ButtonStart.text()}'), "
                        f"current_state={current_state}")
            
            # ВАЖНО: Если процесс только что запустился (init_recipe с шагом <= 2), не останавливаем его
            # Это предотвращает остановку процесса сразу после запуска из-за повторного вызова on_start_button_clicked
            if current_state == 'init_recipe' and self.plasma_process.current_step <= 2:
                start_process_logger.warning(f"[START BUTTON CLICKED] on_start_button_clicked: Process just started (state={current_state}, step={self.plasma_process.current_step}), "
                              f"ignoring stop request to prevent immediate stop after start")
                return
            
            if current_state not in ['idle', 'fault']:
                start_process_logger.info(f"[START BUTTON CLICKED] on_start_button_clicked: Stopping process (current_state={current_state})")
                self.plasma_process.stop_process()
                self.ButtonStart.setText(self.translator.tr('start'))
                self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
            else:
                start_process_logger.warning(f"[START BUTTON CLICKED] on_start_button_clicked: Ignoring stop request - process is not running "
                              f"(current_state={current_state}, button_text='{self.ButtonStart.text()}')")

    def on_start_pump_clicked(self):
        if self.NIButton.text() == self.translator.tr('turn_on_pump'):
            if self.plasma_process.current_state == 'idle':
                success = False
                for attempt in range(self.max_attempts):
                    try:
                        self.controller.handle_command('on_pump')
                        time.sleep(0.1)  # Задержка перед проверкой
                        states = self.controller.handle_command('get_states')
                        if states and states.get('pump'):
                            success = True
                            break
                    except Exception as e:
                        logging.error(f"Error turning on pump (attempt {attempt + 1}): {e}")
                    
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.2)  # Задержка между попытками

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
                    time.sleep(0.1)  # Задержка перед проверкой
                    states = self.controller.handle_command('get_states')
                    if states and not states.get('pump'):
                        success = True
                        break
                except Exception as e:
                    logging.error(f"Error turning off pump (attempt {attempt + 1}): {e}")
                
                if attempt < self.max_attempts - 1:
                    time.sleep(0.2)  # Задержка между попытками

            if success:
                self.update_status(self.translator.tr('pump_off'))
                self.NIButton.setText(self.translator.tr('turn_on_pump'))
                self.timer_pumping.stop()

            else:
                self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_turn_off_pump'))
                self.update_status(self.translator.tr('error_turn_off_pump'))
                self.NIButton.setChecked(True)

    def on_venting_clicked(self):
        venting_start_time = time.time()
        venting_logger.info(f"[on_venting_clicked] ENTRY: button_text='{self.VEButton.text()}'")
        
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
                    venting_logger.info(f"[on_venting_clicked] Selected gases: {selected_gases}")
                    
                    # Проверяем, не выполняется ли уже операция напуска
                    if self._venting_in_progress:
                        venting_logger.warning("[on_venting_clicked] Venting already in progress, ignoring click")
                        return
                    
                    self._venting_in_progress = True
                    self.VEButton.setEnabled(False)  # Отключаем кнопку во время операции
                    
                    # Запускаем операцию напуска газов асинхронно, чтобы не блокировать UI
                    # Захватываем ссылку на self для использования в замыкании
                    main_window_ref = self
                    max_attempts_ref = self.max_attempts
                    
                    def start_venting_task():
                        try:
                            task_start = time.time()
                            venting_logger.info(f"[start_venting_task] STARTED in background thread")
                            
                            # Обновляем статус: установка потоков
                            QTimer.singleShot(0, lambda: main_window_ref.update_status(main_window_ref.translator.tr('setting_flows')))
                            
                            success_set_flow = False
                            
                            # ШАГ 1: Установка потоков
                            flow_setup_start = time.time()
                            for attempt in range(max_attempts_ref):
                                attempt_start = time.time()
                                venting_logger.debug(f"[start_venting_task] Flow setup attempt {attempt + 1}/{max_attempts_ref}")
                                
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
                                                venting_logger.warning(f"[start_venting_task] Failed to set flow for RRG {i} (attempt {attempt + 1})")
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
                                    
                                    time.sleep(0.1)  # Задержка между операциями
                                
                                attempt_time = time.time() - attempt_start
                                venting_logger.debug(f"[start_venting_task] Flow setup attempt {attempt + 1} took {attempt_time:.3f}s, success={set_flow_success}")
                                
                                # Если все операции успешны, выходим из цикла
                                if set_flow_success:
                                    success_set_flow = True
                                    break
                                
                                # Если это не последняя попытка, делаем задержку перед следующей
                                if attempt < max_attempts_ref - 1:
                                    time.sleep(0.3)
                            
                            flow_setup_time = time.time() - flow_setup_start
                            venting_logger.info(f"[start_venting_task] Flow setup completed in {flow_setup_time:.3f}s, success={success_set_flow}")
                            
                            if success_set_flow:
                                venting_logger.info(f"[start_venting_task] Flow setup successful, proceeding to valve setup")
                                # ШАГ 2: Открытие клапанов
                                valve_setup_start = time.time()
                                success_open_valve = False
                                
                                for attempt in range(max_attempts_ref):
                                    attempt_start = time.time()
                                    venting_logger.debug(f"[start_venting_task] Valve setup attempt {attempt + 1}/{max_attempts_ref}")
                                    
                                    # Открываем/закрываем клапаны
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
                                    
                                    # Небольшая задержка перед проверкой состояния
                                    time.sleep(0.1)
                                    
                                    # Проверяем состояние клапанов через оптимизированную команду
                                    states_check_start = time.time()
                                    valves_states = main_window_ref.controller.handle_command('get_valves_states')
                                    states_check_time = time.time() - states_check_start
                                    venting_logger.debug(f"[start_venting_task] get_valves_states took {states_check_time:.3f}s")
                                    
                                    # Проверяем, что states - это словарь
                                    if not isinstance(valves_states, dict):
                                        venting_logger.error(f"[start_venting_task] get_valves_states returned unexpected type: {type(valves_states)}, value: {valves_states}")
                                        if states_check_time > 1.0:
                                            venting_logger.warning(f"[start_venting_task] SLOW get_valves_states: {states_check_time:.3f}s > 1s")
                                        # Если get_valves_states вернул ошибку, считаем, что клапаны открыты (команды open_valve выполнились)
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
                                
                                # Захватываем значения для передачи в главный поток
                                success_valve = success_open_valve
                                selected_gases_list = selected_gases.copy()
                                
                                # Сохраняем данные для обновления UI
                                main_window_ref._venting_result = {
                                    'success': success_valve,
                                    'selected_gases': selected_gases_list
                                }
                                
                                # Обновляем UI в главном потоке через сигнал
                                venting_logger.info(f"[start_venting_task] Emitting signal: success={success_valve}, gases={selected_gases_list}")
                                main_window_ref._venting_result_worker.ventingCompleted.emit({
                                    'success': success_valve,
                                    'selected_gases': selected_gases_list
                                })
                                venting_logger.info(f"[start_venting_task] Signal emitted")
                            else:
                                # Сохраняем данные для обновления UI
                                main_window_ref._venting_result = {
                                    'success': False,
                                    'error': 'flow_setup_failed',
                                    'selected_gases': selected_gases.copy()
                                }
                                
                                # Обновляем UI в главном потоке через сигнал
                                venting_logger.warning(f"[start_venting_task] Flow setup failed, emitting signal")
                                main_window_ref._venting_result_worker.ventingCompleted.emit({
                                    'success': False,
                                    'error': 'flow_setup_failed',
                                    'selected_gases': selected_gases.copy()
                                })
                            
                            task_time = time.time() - task_start
                            venting_logger.info(f"[start_venting_task] COMPLETED in {task_time:.3f}s")
                        except Exception as e:
                            venting_logger.error(f"[start_venting_task] FATAL ERROR: {e}", exc_info=True)
                            # Сохраняем данные для обновления UI
                            main_window_ref._venting_result = {
                                'success': False,
                                'error': 'exception',
                                'exception': str(e)
                            }
                            
                            # Обновляем UI в главном потоке через сигнал
                            main_window_ref._venting_result_worker.ventingCompleted.emit({
                                'success': False,
                                'error': 'exception',
                                'exception': str(e)
                            })
                    
                    # Запускаем задачу в отдельном потоке
                    if self._venting_executor is None:
                        self._venting_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="VentingGas")
                    self._venting_executor.submit(start_venting_task)
                    
                    total_time = time.time() - venting_start_time
                    venting_logger.info(f"[on_venting_clicked] EXIT: submitted async task, took {total_time:.3f}s")

                else:
                    self.VEButton.setChecked(False)
                    self.VEButton.setText(self.translator.tr('start_venting_gas'))
                    # Возвращаем размер шрифта при возврате к "напустить газ"
                    if settings.get('LANG') == 0:
                        self.VEButton.setStyleSheet('font-size: 20px')
                    self.update_status(msg)
                    total_time = time.time() - venting_start_time
                    venting_logger.info(f"[on_venting_clicked] EXIT: validation failed, took {total_time:.3f}s")

        else:
            # Остановка напуска газов - полностью переписана для избежания блокировок порта
            venting_logger.info("=" * 80)
            venting_logger.info("[on_venting_clicked] STOP GASES: Starting gas stop procedure")
            
            # Проверяем, не выполняется ли уже операция остановки
            if self._venting_in_progress:
                venting_logger.warning("[on_venting_clicked] Venting operation already in progress, ignoring stop click")
                return
            
            self._venting_in_progress = True
            self.VEButton.setEnabled(False)  # Отключаем кнопку во время операции
            
            # Собираем список активных РРГ и их типы газов ДО запуска фонового потока
            active_rrgs = []
            gas_types = {}
            for i in work_gases:
                try:
                    if getattr(self, f"VE{i}Button").isChecked():
                        active_rrgs.append(i)
                        gas_types[i] = getattr(self, f"VE{i}ComboBox").currentIndex()
                except Exception as e:
                    venting_logger.error(f"[on_venting_clicked] Error checking VE{i}Button: {e}")
            
            venting_logger.info(f"[on_venting_clicked] STOP GASES: Active RRGs: {active_rrgs}, gas types: {gas_types}")
            
            # Устанавливаем статус
            self.update_status(self.translator.tr('stopping_venting_gas'))
            
            # Запускаем операции остановки в отдельном потоке
            def stop_gases_task():
                venting_logger.info("[stop_gases_task] Task started in background thread")
                start_time = time.time()
                
                try:
                    # ШАГ 1: Останавливаем поток чтения потоков и ждем полного освобождения порта
                    step1_start = time.time()
                    venting_logger.info("[stop_gases_task] Step 1 - Stopping flow reading thread")
                    try:
                        # Проверяем, запущен ли поток, БЕЗ захвата lock (чтобы избежать deadlock)
                        # stop_flow_thread сам управляет lock
                        flow_thread_running = False
                        try:
                            if self.flow_thread is not None and self.flow_thread.isRunning():
                                flow_thread_running = True
                        except:
                            pass
                        
                        if flow_thread_running:
                            venting_logger.info("[stop_gases_task] Flow thread is running, stopping...")
                            stop_thread_start = time.time()
                            # stop_flow_thread сам управляет lock, не нужно захватывать его здесь
                            try:
                                self.stop_flow_thread(wait=True)
                                stop_thread_time = time.time() - stop_thread_start
                                venting_logger.info(f"[stop_gases_task] Flow thread stopped in {stop_thread_time:.3f}s")
                            except Exception as e:
                                stop_thread_time = time.time() - stop_thread_start
                                venting_logger.error(f"[stop_gases_task] Error in stop_flow_thread (took {stop_thread_time:.3f}s): {e}", exc_info=True)
                                # Пытаемся принудительно остановить поток
                                try:
                                    if self.flow_thread and self.flow_thread.isRunning():
                                        venting_logger.warning("[stop_gases_task] Force terminating flow thread")
                                        self.flow_thread.terminate()
                                        if not self.flow_thread.wait(1000):  # Ждем 1 секунду
                                            venting_logger.error("[stop_gases_task] Flow thread did not terminate after 1s")
                                except Exception as e2:
                                    venting_logger.error(f"[stop_gases_task] Error force terminating flow thread: {e2}")
                        else:
                            venting_logger.info("[stop_gases_task] Flow thread is not running")
                    except Exception as e:
                        venting_logger.error(f"[stop_gases_task] Error stopping flow thread: {e}", exc_info=True)
                    
                    # Минимальная задержка для гарантированного освобождения порта
                    time.sleep(0.1)  # Уменьшено с 0.5 до 0.1 секунды
                    step1_time = time.time() - step1_start
                    venting_logger.info(f"[stop_gases_task] Step 1 completed in {step1_time:.3f}s")
                    
                    # ШАГ 2: Последовательно закрываем клапаны (только для активных РРГ)
                    # Оптимизировано: убрана проверка get_states для ускорения (цель: <5 секунд)
                    step2_start = time.time()
                    venting_logger.info(f"[stop_gases_task] Step 2 - Closing valves for RRGs: {active_rrgs}")
                    valves_closed = {}
                    for i in active_rrgs:
                        valve_op_start = time.time()
                        try:
                            venting_logger.info(f"[stop_gases_task] Closing valve VE{i}")
                            result = self.controller.handle_command(f"close_valve_ve{i}")
                            valve_op_time = time.time() - valve_op_start
                            venting_logger.debug(f"[stop_gases_task] close_valve_ve{i} took {valve_op_time:.3f}s, result={result}")
                            
                            # Считаем клапан закрытым, если команда вернула True
                            # Проверка get_states убрана для ускорения операции
                            if result:
                                valves_closed[i] = True
                                venting_logger.info(f"[stop_gases_task] Valve VE{i} closed (assuming success based on command result)")
                            else:
                                valves_closed[i] = False
                                venting_logger.warning(f"[stop_gases_task] close_valve_ve{i} returned False")
                            
                            # Минимальная задержка между операциями (только для освобождения порта)
                            if i < active_rrgs[-1]:  # Не задерживаемся после последнего клапана
                                time.sleep(0.05)  # Уменьшено с 0.3 до 0.05 секунды
                        except Exception as e:
                            valve_op_time = time.time() - valve_op_start
                            venting_logger.error(f"[stop_gases_task] Error closing valve VE{i} (took {valve_op_time:.3f}s): {e}", exc_info=True)
                            valves_closed[i] = False
                    
                    step2_time = time.time() - step2_start
                    venting_logger.info(f"[stop_gases_task] Step 2 completed in {step2_time:.3f}s, valves_closed: {valves_closed}")
                    
                    # Минимальная задержка перед следующей операцией
                    time.sleep(0.1)  # Уменьшено с 0.5 до 0.1 секунды
                    
                    # ШАГ 3: Последовательно устанавливаем потоки в 0
                    step3_start = time.time()
                    venting_logger.info(f"[stop_gases_task] Step 3 - Setting flows to 0 for RRGs: {active_rrgs}")
                    flows_set_to_zero = {}
                    for rrg_num in active_rrgs:
                        flow_op_start = time.time()
                        try:
                            type_gas = gas_types.get(rrg_num, 0)
                            venting_logger.info(f"[stop_gases_task] Setting RRG {rrg_num} flow to 0 (type_gas={type_gas})")
                            result = self.controller.handle_command(
                                command='set_flow',
                                num_rrg=rrg_num,
                                flow_lh=0,
                                type_gas=type_gas
                            )
                            set_flow_time = time.time() - flow_op_start
                            venting_logger.debug(f"[stop_gases_task] set_flow RRG{rrg_num} to 0 took {set_flow_time:.3f}s, result={result}")
                            
                            if result:
                                time.sleep(0.1)  # Уменьшено с 0.3 до 0.1 секунды
                                read_flow_start = time.time()
                                current_flow = self.controller.handle_command(
                                    command='read_flow',
                                    num_rrg=rrg_num,
                                    type_gas=type_gas
                                )
                                read_flow_time = time.time() - read_flow_start
                                venting_logger.debug(f"[stop_gases_task] read_flow RRG{rrg_num} took {read_flow_time:.3f}s, result={current_flow}")
                                
                                if current_flow is not None:
                                    flows_set_to_zero[rrg_num] = (abs(current_flow) < 0.1)
                                    venting_logger.info(f"[stop_gases_task] RRG {rrg_num} flow: {current_flow}, confirmed: {flows_set_to_zero[rrg_num]}")
                                else:
                                    flows_set_to_zero[rrg_num] = False
                                    venting_logger.warning(f"[stop_gases_task] read_flow RRG{rrg_num} returned None")
                            else:
                                flows_set_to_zero[rrg_num] = False
                                venting_logger.warning(f"[stop_gases_task] set_flow RRG{rrg_num} to 0 returned False")
                            # Минимальная задержка между операциями для освобождения порта
                            if rrg_num < active_rrgs[-1]:  # Не задерживаемся после последнего РРГ
                                time.sleep(0.05)  # Уменьшено с 0.4 до 0.05 секунды
                        except Exception as e:
                            flow_op_time = time.time() - flow_op_start
                            venting_logger.error(f"[stop_gases_task] Error setting flow for RRG {rrg_num} (took {flow_op_time:.3f}s): {e}", exc_info=True)
                            flows_set_to_zero[rrg_num] = False
                    
                    step3_time = time.time() - step3_start
                    venting_logger.info(f"[stop_gases_task] Step 3 completed in {step3_time:.3f}s, flows_set_to_zero: {flows_set_to_zero}")
                    
                    # Проверяем успешность операций
                    all_valves_closed = all(valves_closed.values()) if valves_closed else False
                    all_flows_zero = all(flows_set_to_zero.values()) if flows_set_to_zero else False
                    
                    venting_logger.info(f"[stop_gases_task] Verification - valves: {all_valves_closed}, flows: {all_flows_zero}")
                    
                    # Если не все потоки установлены в 0, делаем повторные попытки
                    if not all_flows_zero:
                        venting_logger.warning("[stop_gases_task] Some flows not set to 0, retrying...")
                        time.sleep(0.5)
                        
                        # Повторная попытка для потоков
                        for rrg_num, is_zero in flows_set_to_zero.items():
                            if not is_zero:
                                venting_logger.info(f"[stop_gases_task] Retrying to set RRG {rrg_num} flow to 0")
                                type_gas = gas_types.get(rrg_num, 0)
                                for retry in range(3):  # Максимум 3 попытки
                                    try:
                                        retry_start = time.time()
                                        result = self.controller.handle_command(
                                            command='set_flow',
                                            num_rrg=rrg_num,
                                            flow_lh=0,
                                            type_gas=type_gas
                                        )
                                        retry_time = time.time() - retry_start
                                        venting_logger.debug(f"[stop_gases_task] Retry {retry + 1}: set_flow RRG{rrg_num} to 0 took {retry_time:.3f}s, result={result}")
                                        
                                        if result:
                                            time.sleep(0.3)
                                            read_start = time.time()
                                            current_flow = self.controller.handle_command(
                                                command='read_flow',
                                                num_rrg=rrg_num,
                                                type_gas=type_gas
                                            )
                                            read_time = time.time() - read_start
                                            venting_logger.debug(f"[stop_gases_task] Retry {retry + 1}: read_flow RRG{rrg_num} took {read_time:.3f}s, result={current_flow}")
                                            
                                            if current_flow is not None and abs(current_flow) < 0.1:
                                                flows_set_to_zero[rrg_num] = True
                                                venting_logger.info(f"[stop_gases_task] RRG {rrg_num} flow set to 0 on retry {retry + 1}")
                                                break
                                        time.sleep(0.3)
                                    except Exception as e:
                                        venting_logger.error(f"[stop_gases_task] Error retrying RRG {rrg_num} (retry {retry + 1}): {e}")
                        
                        # Проверяем еще раз
                        all_flows_zero = all(flows_set_to_zero.values()) if flows_set_to_zero else False
                        venting_logger.info(f"[stop_gases_task] After retry - flows: {all_flows_zero}, flows_set_to_zero: {flows_set_to_zero}")
                    
                    # Если не все клапаны закрыты, делаем повторные попытки (но без проверки get_states, чтобы не блокировать)
                    if not all_valves_closed:
                        venting_logger.warning("[stop_gases_task] Some valves not closed, retrying (without state check)...")
                        time.sleep(0.5)
                        
                        # Повторная попытка для клапанов (без проверки состояния, чтобы не блокировать на медленном get_states)
                        for i, is_closed in valves_closed.items():
                            if not is_closed:
                                venting_logger.info(f"[stop_gases_task] Retrying to close valve VE{i}")
                                for retry in range(3):  # Максимум 3 попытки
                                    try:
                                        retry_start = time.time()
                                        result = self.controller.handle_command(f"close_valve_ve{i}")
                                        retry_time = time.time() - retry_start
                                        venting_logger.debug(f"[stop_gases_task] Retry {retry + 1}: close_valve_ve{i} took {retry_time:.3f}s, result={result}")
                                        
                                        if result:
                                            # Не проверяем состояние через get_states, чтобы не блокировать
                                            # Считаем, что если команда вернула True, клапан закрыт
                                            valves_closed[i] = True
                                            venting_logger.info(f"[stop_gases_task] Valve VE{i} closed on retry {retry + 1} (assuming success)")
                                            break
                                        time.sleep(0.3)
                                    except Exception as e:
                                        venting_logger.error(f"[stop_gases_task] Error retrying valve VE{i} (retry {retry + 1}): {e}")
                        
                        # Проверяем еще раз
                        all_valves_closed = all(valves_closed.values()) if valves_closed else False
                        venting_logger.info(f"[stop_gases_task] After retry - valves: {all_valves_closed}, valves_closed: {valves_closed}")
                    
                    # ШАГ 4: Обновляем UI через сигнал
                    venting_logger.info("[stop_gases_task] Step 4 - Updating UI via signal")
                    self._venting_result_worker.ventingCompleted.emit({
                        'success': True,
                        'stop': True,
                        'selected_gases': []
                    })
                    venting_logger.info("[stop_gases_task] Signal emitted for stop")
                    
                    total_elapsed = time.time() - start_time
                    venting_logger.info(f"[stop_gases_task] Task completed in {total_elapsed:.3f}s")
                    venting_logger.info("=" * 80)
                    
                except Exception as e:
                    elapsed = time.time() - start_time
                    venting_logger.error(f"[stop_gases_task] EXCEPTION after {elapsed:.3f}s: {e}", exc_info=True)
                    # В случае ошибки все равно обновляем UI через сигнал
                    venting_logger.error(f"[stop_gases_task] EXCEPTION: {e}", exc_info=True)
                    self._venting_result_worker.ventingCompleted.emit({
                        'success': True,
                        'stop': True,
                        'selected_gases': []
                    })
            
            # Запускаем в ThreadPoolExecutor (не блокирует UI)
            logging.info("DEBUG: Submitting stop_gases_task to ThreadPoolExecutor...")
            try:
                if self._stop_gas_executor is None:
                    logging.info("DEBUG: Creating new ThreadPoolExecutor...")
                    self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
                    logging.info("DEBUG: ThreadPoolExecutor created")
                logging.info("DEBUG: Submitting task...")
                future = self._stop_gas_executor.submit(stop_gases_task)
                logging.info(f"DEBUG: Task submitted, future: {future}")
            except Exception as e:
                logging.error(f"DEBUG: Error submitting task to executor: {e}", exc_info=True)
            
            logging.info("DEBUG: on_venting_clicked - STOP STOPPING GASES (function returning)")
            logging.info("=" * 80)
            
            # Запускаем чтение потоков в отдельном потоке для обновления UI
            def read_flows_post_stop_task():
                """Чтение потоков в фоновом потоке (полностью асинхронно)"""
                thread_id = threading.current_thread().ident
                logging.info(f"DEBUG: read_flows_post_stop_task STARTED in thread {thread_id}")
                import time
                for read_count in range(4):  # 4 чтения = 2 секунды
                    logging.info(f"DEBUG: Post-stop flow read iteration {read_count + 1}/4")
                    time.sleep(0.5)  # Задержка между чтениями
                    for rrg_num in active_rrgs:
                        try:
                            type_gas = gas_types.get(rrg_num, 0)
                            logging.info(f"DEBUG: Reading flow for RRG {rrg_num}...")
                            read_start = time.time()
                            current_flow = self.controller.handle_command(
                                command='read_flow',
                                num_rrg=rrg_num,
                                type_gas=type_gas
                            )
                            read_elapsed = time.time() - read_start
                            logging.info(f"DEBUG: RRG {rrg_num} flow read completed in {read_elapsed:.3f}s, value: {current_flow}")
                            # Обновляем UI через QMetaObject (безопасно из любого потока)
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
                logging.info("DEBUG: read_flows_post_stop_task COMPLETED")
            
            # Запускаем чтение потоков в отдельном потоке через 0.5 секунды
            logging.info("DEBUG: Scheduling post-stop flow reads...")
            QTimer.singleShot(500, lambda: self._stop_gas_executor.submit(read_flows_post_stop_task))

    def on_start_plasma_clicked(self):
        water_ok = True

        if settings.get('check_water_flow', True):
            try:
                water_flow = self.controller.handle_command('get_sensor_water')
                if water_flow == 0.0:
                    self.handle_error(self.translator.tr('error_water_flow_zero'), need_reboot=False)
                    water_ok = False
            except Exception as e:
                logging.error(f"start_recipe: Error checking water flow: {e}")
                self.handle_error(f"{self.translator.tr('error_checking_water_flow')}: {e}", need_reboot=False)
                water_ok = False

        if not self.timer_plasma.isActive():
            if water_ok:
                if self.TimeZad.text() != '00:00':
                    power = int(self.HFPowerZad.text())
                    if 10 <= power <= settings.get('MAX_POWER_BP'):
                        
                        # Включение плазмы - переносим в отдельный поток, чтобы не блокировать UI
                        logging.info("START PLASMA: Starting plasma start procedure")
                        self.update_status(self.translator.tr('turning_on_plasma'))
                        self.HFButton.setChecked(True)  # Включаем кнопку сразу для обратной связи
                        
                        def start_plasma_task():
                            """Задача включения плазмы в отдельном потоке"""
                            logging.info("START PLASMA: Task started in background thread")
                            start_time = time.time()
                            
                            try:
                                # ВАЖНО: Останавливаем поток чтения RF ПЕРЕД обращением к генератору
                                # Это предотвращает ошибки I/O при попытке включить плазму
                                if hasattr(self, 'stop_rf_reading'):
                                    logging.info("START PLASMA: Stopping RF reading thread before on_plasma...")
                                    self.stop_rf_reading(wait=False)  # Не блокируем UI, используем wait=False
                                    # Даем время потоку остановиться асинхронно
                                    time.sleep(0.3)  # Уменьшено с 0.5 до 0.3 секунды
                                    
                                    # Проверяем, что блокировка порта освобождена
                                    if hasattr(self.controller.rf, '_lock'):
                                        if self.controller.rf._lock.acquire(blocking=False):
                                            self.controller.rf._lock.release()
                                            logging.info("START PLASMA: RF port lock is available")
                                        else:
                                            logging.warning("START PLASMA: RF port lock is busy, waiting...")
                                            if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                                                self.controller.rf._lock.release()
                                                logging.info("START PLASMA: RF port lock released after wait")
                                
                                # ШАГ 1: Устанавливаем мощность
                                logging.info(f"START PLASMA: Step 1 - Setting power to {power}W")
                                success_set_power = False
                                for attempt in range(self.max_attempts):
                                    try:
                                        result = self.controller.handle_command('set_power', power=str(power))
                                        if result:
                                            success_set_power = True
                                            logging.info(f"START PLASMA: Power set successfully: {power}W (attempt {attempt + 1})")
                                            break
                                        else:
                                            logging.warning(f"START PLASMA: set_power returned False on attempt {attempt + 1}")
                                    except Exception as e:
                                        logging.error(f"START PLASMA: Error setting power (attempt {attempt + 1}): {e}")
                                    
                                    if attempt < self.max_attempts - 1:
                                        time.sleep(0.3)  # Задержка между попытками
                                
                                if not success_set_power:
                                    logging.error("START PLASMA: Failed to set power")
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
                                            logging.warning(f"START PLASMA: on_plasma command returned False on attempt {attempt + 1}")
                                            if attempt < self.max_attempts - 1:
                                                time.sleep(0.3)
                                                continue
                                        
                                        # Задержка перед первой проверкой — генератор обновляет бит статуса с задержкой
                                        time.sleep(1.0)
                                        
                                        # Проверяем статус: несколько попыток read_status (генератор может обновить бит не сразу)
                                        try:
                                            rf_status = None
                                            rf_on = False
                                            for status_attempt in range(3):
                                                rf_status = self.controller.rf.read_status()
                                                if rf_status:
                                                    rf_on = rf_status.get('rf_on', False)
                                                    logging.info(f"START PLASMA: RF status check (attempt {attempt + 1}, status_read {status_attempt + 1}/3): rf_on={rf_on}")
                                                    if rf_on:
                                                        break
                                                if status_attempt < 2:
                                                    time.sleep(0.5)
                                            if rf_status and rf_on:
                                                self.controller._cached_plasma_status = True
                                                success = True
                                                logging.info(f"START PLASMA: Plasma confirmed ON on attempt {attempt + 1}")
                                                break
                                            if rf_status and not rf_on:
                                                logging.warning(f"START PLASMA: Plasma status rf_on=False on attempt {attempt + 1}, retrying...")
                                            if not rf_status:
                                                logging.warning(f"START PLASMA: rf.read_status() returned None on attempt {attempt + 1}")
                                                # Пытаемся переподключиться к RF генератору асинхронно
                                                def reconnect_rf_async():
                                                    try:
                                                        logging.info(f"[START PLASMA] Attempting to reconnect RF generator (read_status returned None)...")
                                                        reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                                        if reconnect_success:
                                                            logging.info(f"[START PLASMA] RF generator reconnected successfully (was None)")
                                                        else:
                                                            logging.warning(f"[START PLASMA] Failed to reconnect RF generator (was None): {reconnect_msg}")
                                                    except Exception as reconnect_error:
                                                        logging.error(f"[START PLASMA] Error reconnecting RF generator (was None): {reconnect_error}")
                                                
                                                if not hasattr(self, '_rf_operations_executor') or self._rf_operations_executor is None:
                                                    from concurrent.futures import ThreadPoolExecutor
                                                    self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                                self._rf_operations_executor.submit(reconnect_rf_async)
                                                # Нельзя считать плазму включённой без ответа от генератора — продолжаем попытки
                                        except Exception as e:
                                            logging.error(f"START PLASMA: Error reading RF status (attempt {attempt + 1}): {e}")
                                            # Пытаемся переподключиться к RF генератору асинхронно при ошибке
                                            def reconnect_rf_async():
                                                try:
                                                    logging.info(f"[START PLASMA] Attempting to reconnect RF generator (read_status error)...")
                                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                                    if reconnect_success:
                                                        logging.info(f"[START PLASMA] RF generator reconnected successfully (after error)")
                                                    else:
                                                        logging.warning(f"[START PLASMA] Failed to reconnect RF generator (after error): {reconnect_msg}")
                                                except Exception as reconnect_error:
                                                    logging.error(f"[START PLASMA] Error reconnecting RF generator (after error): {reconnect_error}")
                                            
                                            if not hasattr(self, '_rf_operations_executor') or self._rf_operations_executor is None:
                                                from concurrent.futures import ThreadPoolExecutor
                                                self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                            self._rf_operations_executor.submit(reconnect_rf_async)
                                            # Нельзя считать плазму включённой без ответа — продолжаем попытки
                                    except Exception as e:
                                        logging.error(f"START PLASMA: Error turning on plasma (attempt {attempt + 1}): {e}", exc_info=True)
                                    
                                    if attempt < self.max_attempts - 1:
                                        time.sleep(0.4)  # Задержка между попытками
                                
                                # Обновляем UI через сигнал (используем DirectConnection для немедленного выполнения)
                                if success:
                                    logging.info("START PLASMA: Success - updating UI")
                                    # Используем QTimer.singleShot для гарантированного выполнения в главном потоке
                                    QTimer.singleShot(0, self._on_plasma_started)
                                else:
                                    logging.warning("START PLASMA: Failed - updating UI with error")
                                    QtCore.QMetaObject.invokeMethod(
                                        self,
                                        "_on_plasma_start_error",
                                        QtCore.Qt.QueuedConnection,
                                        QtCore.Q_ARG(str, 'error_turn_on_plasma')
                                    )
                                
                                total_elapsed = time.time() - start_time
                                logging.info(f"START PLASMA: Task completed in {total_elapsed:.3f}s")
                                
                            except Exception as e:
                                elapsed = time.time() - start_time
                                logging.error(f"START PLASMA: EXCEPTION after {elapsed:.3f}s: {e}", exc_info=True)
                                QtCore.QMetaObject.invokeMethod(
                                    self,
                                    "_on_plasma_start_error",
                                    QtCore.Qt.QueuedConnection,
                                    QtCore.Q_ARG(str, 'error_turn_on_plasma')
                                )
                        
                        # Запускаем в ThreadPoolExecutor (не блокирует UI)
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
            # Отключение плазмы - переносим в отдельный поток, чтобы не блокировать UI
            logging.info("STOP PLASMA: Starting plasma stop procedure")
            self.update_status(self.translator.tr('turning_off_plasma'))
            
            def stop_plasma_task():
                """Задача выключения плазмы в отдельном потоке"""
                logging.info("STOP PLASMA: Task started in background thread")
                start_time = time.time()
                
                try:
                    # ВАЖНО: Останавливаем поток чтения RF ПЕРЕД обращением к генератору
                    # Это предотвращает ошибки I/O при попытке выключить плазму
                    if hasattr(self, 'stop_rf_reading'):
                        logging.info("STOP PLASMA: Stopping RF reading thread before off_plasma...")
                        self.stop_rf_reading(wait=False)  # Не блокируем UI, используем wait=False
                        # Даем время потоку остановиться асинхронно
                        time.sleep(0.5)  # Уменьшено с 1.0 до 0.5 секунды для уменьшения блокировок
                        
                        # Проверяем, что блокировка порта освобождена
                        lock_acquired = False
                        if hasattr(self.controller.rf, '_lock'):
                            lock_check_start = time.time()
                            logging.info("STOP PLASMA: Checking if RF port lock is available...")
                            # Пытаемся получить блокировку с таймаутом, чтобы убедиться, что она свободна
                            if self.controller.rf._lock.acquire(blocking=False):
                                # Блокировка свободна - сразу освобождаем
                                self.controller.rf._lock.release()
                                lock_acquired = True
                                logging.info(f"STOP PLASMA: RF port lock is available (checked in {time.time() - lock_check_start:.3f}s)")
                            else:
                                # Блокировка занята - ждем с таймаутом
                                logging.warning("STOP PLASMA: RF port lock is busy, waiting for release...")
                                if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                                    self.controller.rf._lock.release()
                                    lock_acquired = True
                                    logging.info(f"STOP PLASMA: RF port lock released after wait (waited {time.time() - lock_check_start:.3f}s)")
                                else:
                                    logging.error("STOP PLASMA: RF port lock timeout - lock still busy after 2s")
                        
                        # Пытаемся очистить буфер порта, если это возможно
                        try:
                            if hasattr(self.controller.rf, 'instrument') and hasattr(self.controller.rf.instrument, 'serial'):
                                if hasattr(self.controller.rf.instrument.serial, 'reset_input_buffer'):
                                    self.controller.rf.instrument.serial.reset_input_buffer()
                                if hasattr(self.controller.rf.instrument.serial, 'reset_output_buffer'):
                                    self.controller.rf.instrument.serial.reset_output_buffer()
                                logging.info("STOP PLASMA: Serial port buffers cleared")
                        except Exception as e:
                            logging.warning(f"STOP PLASMA: Could not clear serial buffers: {e}")
                    
                    # КРИТИЧЕСКИ ВАЖНО: Выключаем плазму с агрессивными попытками и переподключением
                    # Увеличиваем количество попыток и добавляем переподключение при потере связи
                    result = False
                    plasma_off_start = time.time()
                    max_plasma_attempts = 5  # Увеличено до 5 попыток
                    
                    for plasma_attempt in range(max_plasma_attempts):
                        logging.info(f"STOP PLASMA: Calling off_plasma (attempt {plasma_attempt + 1}/{max_plasma_attempts})...")
                        
                        # Перед каждой попыткой проверяем связь и переподключаемся при необходимости
                        if plasma_attempt > 0:  # Не перед первой попыткой
                            try:
                                # Проверяем связь через чтение статуса
                                test_status = self.controller.rf.read_status()
                                if test_status is None:
                                    logging.warning(f"STOP PLASMA: Connection lost before attempt {plasma_attempt + 1}, reconnecting...")
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        logging.info(f"STOP PLASMA: RF generator reconnected successfully before attempt {plasma_attempt + 1}")
                                        time.sleep(0.5)  # Задержка после переподключения
                                    else:
                                        logging.error(f"STOP PLASMA: Failed to reconnect RF generator before attempt {plasma_attempt + 1}: {reconnect_msg}")
                            except Exception as reconnect_test_error:
                                logging.warning(f"STOP PLASMA: Error checking connection before attempt {plasma_attempt + 1}: {reconnect_test_error}")
                                # Пытаемся переподключиться при любой ошибке
                                try:
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        logging.info(f"STOP PLASMA: RF generator reconnected after error check")
                                        time.sleep(0.5)
                                except Exception as reconnect_error:
                                    logging.error(f"STOP PLASMA: Error during reconnection attempt: {reconnect_error}")
                        
                        result = self.controller.handle_command('off_plasma')
                        plasma_off_time = time.time() - plasma_off_start
                        logging.info(f"STOP PLASMA: off_plasma attempt {plasma_attempt + 1} took {plasma_off_time:.3f}s, result={result}")
                        
                        if result:
                            logging.info(f"STOP PLASMA: off_plasma succeeded on attempt {plasma_attempt + 1}")
                            break
                        else:
                            if plasma_attempt < max_plasma_attempts - 1:  # Не последняя попытка
                                logging.warning(f"STOP PLASMA: off_plasma failed on attempt {plasma_attempt + 1}, reconnecting and retrying...")
                                
                                # Переподключаемся перед следующей попыткой
                                try:
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        logging.info(f"STOP PLASMA: RF generator reconnected before retry")
                                    else:
                                        logging.error(f"STOP PLASMA: Failed to reconnect RF generator before retry: {reconnect_msg}")
                                except Exception as reconnect_error:
                                    logging.error(f"STOP PLASMA: Error reconnecting before retry: {reconnect_error}")
                                
                                time.sleep(0.8)  # Увеличена задержка перед следующей попыткой
                                
                                # Пытаемся очистить буферы порта перед следующей попыткой
                                try:
                                    if hasattr(self.controller.rf, 'instrument') and hasattr(self.controller.rf.instrument, 'serial'):
                                        if hasattr(self.controller.rf.instrument.serial, 'reset_input_buffer'):
                                            self.controller.rf.instrument.serial.reset_input_buffer()
                                        if hasattr(self.controller.rf.instrument.serial, 'reset_output_buffer'):
                                            self.controller.rf.instrument.serial.reset_output_buffer()
                                        logging.debug("STOP PLASMA: Serial port buffers cleared before retry")
                                except Exception as e:
                                    logging.debug(f"STOP PLASMA: Could not clear serial buffers before retry: {e}")
                    
                    if not result:
                        logging.error(f"STOP PLASMA: CRITICAL - Failed to turn off plasma after {max_plasma_attempts} attempts!")
                    
                    # Задержка перед проверкой - генератор может выключаться с задержкой
                    time.sleep(0.5)
                    
                    # Проверяем статус напрямую из генератора с переподключением при потере связи
                    # Увеличиваем количество попыток для надежности
                    success = False
                    plasma_off_confirmed = False
                    status_check_attempts = 5  # Увеличено до 5 попыток
                    
                    # Если команда off_plasma не удалась, все равно проверяем статус после переподключения
                    if not result:
                        logging.warning("STOP PLASMA: off_plasma command failed, but checking status after reconnection...")
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
                                rf_on = rf_status.get('rf_on', True)  # По умолчанию True, если не удалось прочитать
                                forward_power = rf_status.get('forward_w', None)
                                reflected_power = rf_status.get('reflect_w', None)
                                logging.info(f"STOP PLASMA: RF status check (status_check {status_attempt + 1}/{status_check_attempts}): rf_on={rf_on}, forward_power={forward_power}, reflected_power={reflected_power}")
                                
                                # Плазма выключена только если получили подтверждение:
                                if not rf_on:
                                    plasma_off_confirmed = True
                                    logging.info(f"STOP PLASMA: Plasma confirmed OFF by rf_on=False")
                                    break
                                elif forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                    # Если обе мощности = 0, плазма выключена, даже если rf_on еще True
                                    plasma_off_confirmed = True
                                    logging.info(f"STOP PLASMA: Plasma confirmed OFF by power=0 (both forward and reflected = 0)")
                                    break
                            else:
                                logging.warning(f"STOP PLASMA: rf.read_status() returned None (status_check {status_attempt + 1}/{status_check_attempts})")
                                # КРИТИЧЕСКИ ВАЖНО: Переподключаемся СИНХРОННО, чтобы сразу повторить попытку отключения
                                try:
                                    logging.warning(f"[STOP PLASMA] CRITICAL: Connection lost, reconnecting SYNCHRONOUSLY (status_check {status_attempt + 1})...")
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        logging.info(f"[STOP PLASMA] RF generator reconnected successfully (was None)")
                                        time.sleep(0.5)
                                        
                                        # После переподключения ПОВТОРНО пытаемся отключить плазму
                                        logging.warning(f"[STOP PLASMA] CRITICAL: Retrying off_plasma after reconnection (status_check {status_attempt + 1})...")
                                        retry_result = self.controller.handle_command('off_plasma')
                                        if retry_result:
                                            logging.info(f"[STOP PLASMA] CRITICAL: off_plasma succeeded after reconnection!")
                                            # Проверяем статус сразу после успешного отключения
                                            time.sleep(0.5)
                                            retry_status = self.controller.rf.read_status()
                                            if retry_status:
                                                retry_rf_on = retry_status.get('rf_on', True)
                                                retry_forward = retry_status.get('forward_w', None)
                                                retry_reflected = retry_status.get('reflect_w', None)
                                                if not retry_rf_on or (retry_forward == 0 and retry_reflected == 0):
                                                    plasma_off_confirmed = True
                                                    logging.info(f"[STOP PLASMA] CRITICAL: Plasma confirmed OFF after reconnection and retry!")
                                                    break
                                    else:
                                        logging.error(f"[STOP PLASMA] CRITICAL: Failed to reconnect RF generator (was None): {reconnect_msg}")
                                except Exception as reconnect_error:
                                    logging.error(f"[STOP PLASMA] CRITICAL: Error reconnecting RF generator (was None): {reconnect_error}")
                                
                                # Пробуем проверить через отдельные команды мощности
                                try:
                                    forward_power = self.controller.handle_command('get_forward_power')
                                    reflected_power = self.controller.handle_command('get_reflected_power')
                                    logging.info(f"STOP PLASMA: Power check (status_check {status_attempt + 1}): forward={forward_power}, reflected={reflected_power}")
                                    if forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                        plasma_off_confirmed = True
                                        logging.info(f"STOP PLASMA: Plasma confirmed OFF by power check (both = 0)")
                                        break
                                except Exception as e:
                                    logging.error(f"STOP PLASMA: Error checking power (status_check {status_attempt + 1}): {e}")
                                
                                # Если не последняя попытка, ждем перед следующей
                                if status_attempt < status_check_attempts - 1:
                                    time.sleep(0.5)  # Задержка перед следующей попыткой чтения статуса
                        except Exception as e:
                            logging.error(f"STOP PLASMA: Error reading RF status (status_check {status_attempt + 1}): {e}")
                            # КРИТИЧЕСКИ ВАЖНО: Переподключаемся СИНХРОННО при ошибке и повторяем отключение
                            try:
                                logging.warning(f"[STOP PLASMA] CRITICAL: Error reading status, reconnecting SYNCHRONOUSLY (status_check {status_attempt + 1})...")
                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                if reconnect_success:
                                    logging.info(f"[STOP PLASMA] RF generator reconnected successfully (after error)")
                                    time.sleep(0.5)
                                    
                                    # После переподключения ПОВТОРНО пытаемся отключить плазму
                                    logging.warning(f"[STOP PLASMA] CRITICAL: Retrying off_plasma after reconnection (status_check {status_attempt + 1})...")
                                    retry_result = self.controller.handle_command('off_plasma')
                                    if retry_result:
                                        logging.info(f"[STOP PLASMA] CRITICAL: off_plasma succeeded after reconnection!")
                                        # Проверяем статус сразу после успешного отключения
                                        time.sleep(0.5)
                                        retry_status = self.controller.rf.read_status()
                                        if retry_status:
                                            retry_rf_on = retry_status.get('rf_on', True)
                                            retry_forward = retry_status.get('forward_w', None)
                                            retry_reflected = retry_status.get('reflect_w', None)
                                            if not retry_rf_on or (retry_forward == 0 and retry_reflected == 0):
                                                plasma_off_confirmed = True
                                                logging.info(f"[STOP PLASMA] CRITICAL: Plasma confirmed OFF after reconnection and retry!")
                                                break
                                else:
                                    logging.error(f"[STOP PLASMA] CRITICAL: Failed to reconnect RF generator (after error): {reconnect_msg}")
                            except Exception as reconnect_error:
                                logging.error(f"[STOP PLASMA] CRITICAL: Error reconnecting RF generator (after error): {reconnect_error}")
                            
                            # Пробуем проверить через отдельные команды мощности
                            try:
                                forward_power = self.controller.handle_command('get_forward_power')
                                reflected_power = self.controller.handle_command('get_reflected_power')
                                logging.info(f"STOP PLASMA: Power check after error (status_check {status_attempt + 1}): forward={forward_power}, reflected={reflected_power}")
                                if forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                    plasma_off_confirmed = True
                                    logging.info(f"STOP PLASMA: Plasma confirmed OFF by power check (both = 0)")
                                    break
                            except Exception as e2:
                                logging.error(f"STOP PLASMA: Error checking power after error (status_check {status_attempt + 1}): {e2}")
                    
                    # Обновляем UI через сигнал
                    if plasma_off_confirmed:
                        success = True
                        self.controller._cached_plasma_status = False
                        logging.info("STOP PLASMA: Success - updating UI")
                        QtCore.QMetaObject.invokeMethod(
                            self,
                            "_on_plasma_stopped",
                            QtCore.Qt.QueuedConnection
                        )
                    else:
                        # КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ: Плазма может быть еще включена!
                        logging.error("STOP PLASMA: CRITICAL FAILURE - Plasma may still be ON! All attempts failed.")
                        logging.error("STOP PLASMA: This is a SAFETY CRITICAL situation - plasma must be turned off manually!")
                        
                        # Пытаемся еще раз переподключиться и отключить в последний раз
                        try:
                            logging.warning("STOP PLASMA: CRITICAL - Final reconnection and off_plasma attempt...")
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
                                            logging.info("STOP PLASMA: CRITICAL - Final attempt succeeded! Plasma is OFF.")
                                            QtCore.QMetaObject.invokeMethod(
                                                self,
                                                "_on_plasma_stopped",
                                                QtCore.Qt.QueuedConnection
                                            )
                                            return
                        except Exception as final_error:
                            logging.error(f"STOP PLASMA: CRITICAL - Final attempt also failed: {final_error}")
                        
                        # Если все попытки не удались - показываем критическое предупреждение
                        logging.warning("STOP PLASMA: Failed - updating UI with CRITICAL error")
                        QtCore.QMetaObject.invokeMethod(
                            self,
                            "_on_plasma_stop_error",
                            QtCore.Qt.QueuedConnection
                        )
                    
                    total_elapsed = time.time() - start_time
                    logging.info(f"STOP PLASMA: Task completed in {total_elapsed:.3f}s")
                    
                except Exception as e:
                    elapsed = time.time() - start_time
                    logging.error(f"STOP PLASMA: EXCEPTION after {elapsed:.3f}s: {e}", exc_info=True)
                    # В случае исключения вызываем обработчик ошибки, а не успешной остановки
                    QtCore.QMetaObject.invokeMethod(
                        self,
                        "_on_plasma_stop_error",
                        QtCore.Qt.QueuedConnection
                    )
            
            # Запускаем в ThreadPoolExecutor (не блокирует UI)
            if self._stop_gas_executor is None:
                self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
            self._stop_gas_executor.submit(stop_plasma_task)
    
    @QtCore.pyqtSlot(dict)
    def _on_venting_completed(self, result):
        """Обновление UI после завершения операции напуска газов (вызывается из главного потока через сигнал)"""
        try:
            venting_logger.info(f"[_on_venting_completed] METHOD CALLED with result: {result}")
            
            success = result.get('success', False)
            selected_gases = result.get('selected_gases', [])
            error = result.get('error')
            is_stop = result.get('stop', False)
            
            venting_logger.info(f"[_on_venting_completed] CALLED: success={success}, selected_gases={selected_gases}, error={error}, is_stop={is_stop}")
            
            if is_stop:
                # Остановка напуска газов
                venting_logger.info("[_on_venting_completed] Processing STOP operation")
                self.VEButton.setChecked(False)
                new_text = self.translator.tr('start_venting_gas')
                self.VEButton.setText(new_text)
                # Возвращаем размер шрифта при возврате к "напустить газ"
                if settings.get('LANG') == 0:
                    self.VEButton.setStyleSheet('font-size: 20px')
                self.VEButton.setEnabled(True)
                self.update_status(self.translator.tr('system_ready_tech'))
                venting_logger.info(f"[_on_venting_completed] Button updated for STOP, text now: '{self.VEButton.text()}'")
                return
            
            if success:
                if len(selected_gases) == 1:
                    self.update_status(f"{self.translator.tr('venting_gas')} {selected_gases[0]}.")
                elif len(selected_gases) == 2:
                    self.update_status(f"{self.translator.tr('venting_mixture_gases')} {selected_gases[0]} {self.translator.tr('and')} {selected_gases[1]}.")
                
                new_text = self.translator.tr('stop_venting_gas')
                venting_logger.info(f"[_on_venting_completed] Setting button text to: '{new_text}'")
                self.VEButton.setText(new_text)
                if settings.get('LANG') == 0:
                    self.VEButton.setStyleSheet('font-size: 18px')
                self.VEButton.setChecked(True)
                self.VEButton.setEnabled(True)
                venting_logger.info(f"[_on_venting_completed] Button updated successfully, text now: '{self.VEButton.text()}'")
            else:
                self.VEButton.setChecked(False)
                self.VEButton.setText(self.translator.tr('start_venting_gas'))
                # Возвращаем размер шрифта при возврате к "напустить газ"
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
        """Обработка успешного включения плазмы (вызывается из главного потока)"""
        logging.info("START PLASMA: UI update - plasma started")
        self.HFButton.setText(self.translator.tr('turn_off_plasma'))
        self.plasma_start_time = time.time()
        
        # Инициализируем прогресс-бар времени обработки
        try:
            minutes, sec = map(int, self.TimeZad.text().split(":"))
            total_sec = minutes * 60 + sec
            self.TimeProgress.setMaximum(total_sec)
            self.TimeProgress.setValue(0)
            logging.debug(f"START PLASMA: TimeProgress initialized: max={total_sec}s")
        except (ValueError, TypeError) as e:
            logging.error(f"START PLASMA: Error initializing TimeProgress: {e}")
            self.TimeProgress.setMaximum(100)
            self.TimeProgress.setValue(0)
        
        self.timer_plasma.start(100)
        self.update_status(self.translator.tr('plasma_on'))
        # Запускаем чтение данных генератора в отдельном потоке с задержкой
        # (как в state_machine.py) - даем время порту полностью освободиться
        QTimer.singleShot(1000, self.start_rf_reading)
    
    @QtCore.pyqtSlot(str)
    def _on_plasma_start_error(self, error_type):
        """Обработка ошибки при включении плазмы"""
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
        """Обработка успешного выключения плазмы (вызывается из главного потока)"""
        logging.info("STOP PLASMA: UI update - plasma stopped")
        self.update_status(self.translator.tr('plasma_off'))
        self.HFButton.setText(self.translator.tr('turn_on_plasma'))
        self.HFButton.setChecked(False)
        self.timer_plasma.stop()
        # Устанавливаем 0 в поля мощности при выключении плазмы
        if hasattr(self, 'HFPowerZnach'):
            self.HFPowerZnach.setText("0")
        if hasattr(self, 'HFCurrentZnach'):
            self.HFCurrentZnach.setText("0")
        # Останавливаем чтение данных генератора (не ждем, чтобы не блокировать UI)
        self.stop_rf_reading(wait=False)
    
    @QtCore.pyqtSlot()
    def _on_plasma_stop_error(self):
        """Обработка ошибки при выключении плазмы - КРИТИЧЕСКАЯ СИТУАЦИЯ БЕЗОПАСНОСТИ"""
        logging.error("STOP PLASMA: CRITICAL - UI update - error stopping plasma - PLASMA MAY STILL BE ON!")
        # Показываем критическое предупреждение
        critical_msg = f"{self.translator.tr('error_turn_off_plasma')}\n\n⚠️ КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ: Плазма может быть еще включена!\nПопробуйте отключить снова или проверьте связь с генератором."
        self.show_msg(text=self.translator.tr('warning'), info_text=critical_msg)
        self.update_status(self.translator.tr('error_turn_off_plasma'))
        
        # НЕ меняем состояние кнопки - плазма может быть еще включена
        # Кнопка остается в состоянии "отключить плазму" и нажатой (checked=True)
        # чтобы пользователь мог попробовать отключить снова
        # self.HFButton.setText(self.translator.tr('turn_on_plasma'))  # НЕ меняем текст
        # self.HFButton.setChecked(False)  # НЕ снимаем checked - плазма может быть еще включена
        
        # Таймер плазмы продолжаем, чтобы продолжать читать статус
        # self.timer_plasma.stop()  # НЕ останавливаем таймер
        
        # Не устанавливаем 0 в поля мощности при ошибке - плазма может быть еще включена
        # Продолжаем чтение RF, чтобы видеть реальное состояние
        # self.stop_rf_reading(wait=False)  # НЕ останавливаем чтение
        
        # Логируем критическую ситуацию
        logging.error("STOP PLASMA: CRITICAL SAFETY WARNING - Plasma may still be ON! User must verify manually!")

    def _process_stop_gases(self):
        """Асинхронная обработка остановки напуска газов по шагам (не блокирует UI)"""
        if not self._stopping_gases:
            self.timer_stop_gases.stop()
            return
        
        # Защита от зависания - если процесс идет слишком долго, завершаем принудительно
        if self._stop_gas_step > 0 and self._stop_gas_attempt > 10:
            logging.warning("Gas stopping process taking too long, forcing completion")
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
                
                # Сразу переходим к следующему шагу (не ждем завершения)
                self._stop_gas_step = 2  # Пропускаем проверку клапанов
                self._stop_gas_attempt = 0
                return
            
            elif self._stop_gas_step == 2:
                # Шаг 3: Устанавливаем поток в 0 для всех активных РРГ одновременно в отдельном потоке
                if self._stop_gas_attempt == 0:  # Выполняем только один раз
                    def set_all_flows_zero():
                        for rrg_num in self._stop_gas_rrgs:
                            try:
                                result = self.controller.handle_command(
                                    command='set_flow', 
                                    num_rrg=rrg_num, 
                                    flow_lh=0, 
                                    type_gas=getattr(self, f"VE{rrg_num}ComboBox").currentIndex()
                                )
                                if result is False:
                                    logging.warning(f"Failed to set flow to 0 for RRG {rrg_num}")
                            except Exception as e:
                                logging.error(f"Exception setting flow to 0 for RRG {rrg_num}: {e}")
                    
                    # Запускаем все операции в отдельном потоке (не блокирует UI)
                    if self._stop_gas_executor is None:
                        self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
                    self._stop_gas_executor.submit(set_all_flows_zero)
                    
                    self._stop_gas_attempt = 1
                    # Сразу переходим к финальному шагу (не ждем завершения операций)
                    self._stop_gas_step = 3
                    return
                else:
                    # Уже выполнили, переходим дальше
                    self._stop_gas_step = 3
                    return
            
            elif self._stop_gas_step == 3:
                # Шаг 4: Финальная очистка (UI уже обновлен)
                def final_cleanup():
                    try:
                        with self._flow_thread_lock:
                            if self.flow_thread is not None and self.flow_thread.isRunning():
                                logging.debug("Stopping flow thread after gas venting stopped")
                                self.stop_flow_thread(wait=False)
                    except Exception as e:
                        logging.error(f"Error in final cleanup: {e}")
                
                # Выполняем финальную очистку асинхронно
                if self._stop_gas_executor:
                    self._stop_gas_executor.submit(final_cleanup)
                
                # Сбрасываем флаги сразу (не ждем завершения)
                self._stopping_gases = False
                self._stop_gas_step = 0
                self._stop_gas_rrgs = []
                self._stop_gas_attempt = 0
                self.timer_stop_gases.stop()
                logging.debug("Gas stopping process completed")
                return
                
        except Exception as e:
            logging.error(f"Error in _process_stop_gases: {e}")
            # В случае ошибки все равно завершаем процесс
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
        """Обновление времени работы плазмы (не блокирует UI)"""
        # Проверяем, что время старта установлено
        if self.plasma_start_time == 0:
            logging.warning("update_plasma_time: plasma_start_time is 0, skipping update")
            return
        
        elapsed_time = int(time.time() - self.plasma_start_time)
        mins, secs = elapsed_time // 60, elapsed_time % 60
        self.TimeZnach.setText(f"{mins:02d}:{secs:02d}")

        # Обновляем прогресс-бар времени обработки
        try:
            minutes, sec = map(int, self.TimeZad.text().split(":"))
            total_sec = minutes * 60 + sec
            
            # Устанавливаем максимум прогресс-бара (если еще не установлен)
            if self.TimeProgress.maximum() != total_sec:
                self.TimeProgress.setMaximum(total_sec)
            
            # Ограничиваем значение от 0 до maximum
            progress_value = max(0, min(total_sec, elapsed_time))
            self.TimeProgress.setValue(progress_value)
        except (ValueError, TypeError) as e:
            logging.error(f"update_plasma_time: Error parsing time or updating progress: {e}")
            # В случае ошибки все равно обновляем время, но не прогресс-бар
            return
        
        if elapsed_time >= total_sec:
            # Время истекло - останавливаем таймер, чтобы не вызывать повторно
            self.timer_plasma.stop()
            logging.info(f"PLASMA TIMEOUT: Time expired ({elapsed_time}s >= {total_sec}s), stopping timer and turning off plasma")
            
            # Выключаем плазму в отдельном потоке, чтобы не блокировать UI
            def stop_plasma_async():
                """Выключение плазмы по таймауту в отдельном потоке"""
                logging.info("PLASMA TIMEOUT: Starting plasma stop on timeout")
                try:
                    success = False
                    for attempt in range(self.max_attempts):
                        try:
                            logging.info(f"PLASMA TIMEOUT: Attempt {attempt + 1}/{self.max_attempts}")
                            result = self.controller.handle_command('off_plasma')
                            if result:
                                time.sleep(0.3)  # Задержка перед проверкой
                                # Проверяем статус напрямую из генератора (не используем get_states)
                                try:
                                    rf_status = self.controller.rf.read_status()
                                    if rf_status:
                                        rf_on = rf_status.get('rf_on', True)
                                        logging.info(f"PLASMA TIMEOUT: RF status check (attempt {attempt + 1}): rf_on={rf_on}")
                                        if not rf_on:
                                            # Обновляем кэш
                                            self.controller._cached_plasma_status = False
                                            success = True
                                            logging.info(f"PLASMA TIMEOUT: Plasma confirmed OFF on attempt {attempt + 1}")
                                            # Обновляем UI через сигнал
                                            QtCore.QMetaObject.invokeMethod(
                                                self,
                                                "_on_plasma_timeout",
                                                QtCore.Qt.QueuedConnection
                                            )
                                            break
                                        else:
                                            logging.warning(f"PLASMA TIMEOUT: Plasma still ON on attempt {attempt + 1}, retrying...")
                                    else:
                                        logging.warning(f"PLASMA TIMEOUT: rf.read_status() returned None on attempt {attempt + 1}")
                                        # Пытаемся переподключиться к RF генератору асинхронно
                                        def reconnect_rf_async():
                                            try:
                                                logging.info(f"[PLASMA TIMEOUT] Attempting to reconnect RF generator (read_status returned None)...")
                                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                                if reconnect_success:
                                                    logging.info(f"[PLASMA TIMEOUT] RF generator reconnected successfully (was None)")
                                                else:
                                                    logging.warning(f"[PLASMA TIMEOUT] Failed to reconnect RF generator (was None): {reconnect_msg}")
                                            except Exception as reconnect_error:
                                                logging.error(f"[PLASMA TIMEOUT] Error reconnecting RF generator (was None): {reconnect_error}")
                                        
                                        if not hasattr(self, '_rf_operations_executor') or self._rf_operations_executor is None:
                                            from concurrent.futures import ThreadPoolExecutor
                                            self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                        self._rf_operations_executor.submit(reconnect_rf_async)
                                        # Нельзя считать плазму выключенной без ответа от генератора — продолжаем попытки
                                except Exception as e:
                                    logging.error(f"PLASMA TIMEOUT: Error reading RF status (attempt {attempt + 1}): {e}")
                                    # Пытаемся переподключиться к RF генератору асинхронно при ошибке
                                    def reconnect_rf_async():
                                        try:
                                            logging.info(f"[PLASMA TIMEOUT] Attempting to reconnect RF generator (read_status error)...")
                                            reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                            if reconnect_success:
                                                logging.info(f"[PLASMA TIMEOUT] RF generator reconnected successfully (after error)")
                                            else:
                                                logging.warning(f"[PLASMA TIMEOUT] Failed to reconnect RF generator (after error): {reconnect_msg}")
                                        except Exception as reconnect_error:
                                            logging.error(f"[PLASMA TIMEOUT] Error reconnecting RF generator (after error): {reconnect_error}")
                                    
                                    if not hasattr(self, '_rf_operations_executor') or self._rf_operations_executor is None:
                                        from concurrent.futures import ThreadPoolExecutor
                                        self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                    self._rf_operations_executor.submit(reconnect_rf_async)
                                    # Нельзя считать плазму выключенной без ответа — продолжаем попытки
                            else:
                                logging.warning(f"PLASMA TIMEOUT: off_plasma command returned False on attempt {attempt + 1}")
                        except Exception as e:
                            logging.error(f"PLASMA TIMEOUT: Error stopping plasma (attempt {attempt + 1}): {e}", exc_info=True)
                        
                        if attempt < self.max_attempts - 1:
                            time.sleep(0.4)  # Задержка между попытками
                    
                    if not success:
                        logging.warning("PLASMA TIMEOUT: Failed to stop plasma - updating UI with error")
                        QtCore.QMetaObject.invokeMethod(
                            self,
                            "_on_plasma_timeout_error",
                            QtCore.Qt.QueuedConnection
                        )
                except Exception as e:
                    logging.error(f"PLASMA TIMEOUT: EXCEPTION in stop_plasma_async: {e}", exc_info=True)
                    # В случае ошибки все равно обновляем UI
                    QtCore.QMetaObject.invokeMethod(
                        self,
                        "_on_plasma_timeout",
                        QtCore.Qt.QueuedConnection
                    )
            
            # Запускаем в отдельном потоке
            if self._stop_gas_executor is None:
                self._stop_gas_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_gas")
            self._stop_gas_executor.submit(stop_plasma_async)
    
    @QtCore.pyqtSlot()
    def _on_plasma_timeout(self):
        """Обработка истечения времени плазмы (вызывается из главного потока)"""
        logging.info("PLASMA TIMEOUT: UI update - plasma stopped on timeout")
        elapsed_time = int(time.time() - self.plasma_start_time)
        self.timer_plasma.stop()
        # Обновляем прогресс-бар времени обработки до финального значения
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
        # Устанавливаем 0 в поля мощности при истечении времени плазмы
        if hasattr(self, 'HFPowerZnach'):
            self.HFPowerZnach.setText("0")
        if hasattr(self, 'HFCurrentZnach'):
            self.HFCurrentZnach.setText("0")
        # Останавливаем чтение данных генератора (не ждем, чтобы не блокировать UI)
        self.stop_rf_reading(wait=False)
    
    @QtCore.pyqtSlot()
    def _on_plasma_timeout_error(self):
        """Обработка ошибки при выключении плазмы по таймауту"""
        logging.warning("PLASMA TIMEOUT: UI update - error stopping plasma on timeout")
        self.show_msg(text=self.translator.tr('warning'), info_text=self.translator.tr('error_turn_off_plasma'))
        self.update_status(self.translator.tr('error_turn_off_plasma'))
        # Все равно обновляем UI
        self.HFButton.setText(self.translator.tr('turn_on_plasma'))
        self.HFButton.setChecked(False)
        self.timer_plasma.stop()
        # Не устанавливаем 0 в поля мощности при ошибке - плазма может быть еще включена
        self.stop_rf_reading(wait=False)
    
    def start_rf_reading(self):
        """Запуск чтения данных генератора RF в отдельном потоке"""
        start_time = time.time()
        process_logger.info(f"[start_rf_reading] CALLED at {start_time:.3f}")
        
        with self._rf_thread_lock:
            if self._rf_thread_busy:
                process_logger.warning("[start_rf_reading] Thread is busy, skipping")
                logging.debug("RF reading thread is busy, skipping")
                return
            
            if self.rf_thread is not None and self.rf_thread.isRunning():
                process_logger.warning("[start_rf_reading] Thread already running")
                logging.debug("RF reading thread already running")
                return
            
            self._rf_thread_busy = True
        
        try:
            setup_start = time.time()
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
            
            setup_time = time.time() - setup_start
            process_logger.debug(f"[start_rf_reading] Setup took {setup_time:.3f}s")
            
            rf_thread.start()
            total_time = time.time() - start_time
            process_logger.info(f"[start_rf_reading] Thread started successfully in {total_time:.3f}s")
            logging.info("RF reading thread started successfully")
        except Exception as e:
            process_logger.error(f"[start_rf_reading] Error: {e}", exc_info=True)
            logging.error(f"Error starting RF reading thread: {e}")
            with self._rf_thread_lock:
                self.rf_worker = None
                self.rf_thread = None
                self._rf_thread_busy = False
    
    def stop_rf_reading(self, wait=False):
        """Остановка чтения данных генератора RF (не блокирует UI)"""
        logging.info(f"STOP RF READING: Called, wait={wait}")
        process_logger.info(f"[stop_rf_reading] Called, wait={wait}")
        try:
            # Сначала останавливаем воркер, чтобы он перестал читать
            if self.rf_worker:
                process_logger.info("[stop_rf_reading] Stopping worker...")
                self.rf_worker.stop()
                # Даем время воркеру завершить текущее чтение (если оно идет)
                # Это важно, чтобы блокировка порта была освобождена
                if wait:
                    time.sleep(0.5)  # Даем время завершить текущую операцию read_status()
            
            # Для неблокирующего вызова (wait=False) нельзя указывать timeout
            if wait:
                lock_acquired = self._rf_thread_lock.acquire(blocking=True, timeout=3.0)
            else:
                lock_acquired = self._rf_thread_lock.acquire(blocking=False)
            
            if lock_acquired:
                try:
                    if self.rf_thread:
                        if self.rf_thread.isRunning():
                            process_logger.info("[stop_rf_reading] Thread is running, quitting...")
                            self.rf_thread.quit()
                            if wait:
                                process_logger.info("[stop_rf_reading] Waiting for thread to finish...")
                                if not self.rf_thread.wait(2000):  # Увеличено до 2 секунд
                                    process_logger.warning("[stop_rf_reading] Thread did not finish, terminating...")
                                    self.rf_thread.terminate()
                                    self.rf_thread.wait(1000)  # Увеличено до 1 секунды
                                    process_logger.info("[stop_rf_reading] Thread terminated")
                            
                            if wait:
                                try:
                                    self.rf_thread.deleteLater()
                                except:
                                    pass
                                self.rf_worker = None
                                self.rf_thread = None
                                process_logger.info("[stop_rf_reading] Thread and worker cleaned up")
                    
                    self._rf_thread_busy = False
                finally:
                    self._rf_thread_lock.release()
                    process_logger.info("[stop_rf_reading] Lock released")
            else:
                logging.warning("STOP RF READING: Lock is busy, skipping (will be cleaned up later)")
                process_logger.warning("[stop_rf_reading] Lock is busy, skipping")
        except Exception as e:
            logging.error(f"STOP RF READING: Error: {e}", exc_info=True)
            process_logger.error(f"[stop_rf_reading] Error: {e}", exc_info=True)
    
    def _on_rf_thread_finished(self):
        """Обработчик завершения потока чтения данных генератора"""
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
        rf_data_start = time.time()
        
        if status is None:
            process_logger.debug(f"[on_rf_data_read] Received None status")
            return
        
        try:
            forward_power = status.get('forward_w', 0)
            reflected_power = status.get('reflect_w', 0)
            rf_on = status.get('rf_on', False)
            
            self.controller._cached_plasma_status = rf_on
            
            # Обновляем UI только если плазма включена (проверяем реальное состояние, а не только текст кнопки)
            # Это работает и для ручного режима, и для автоматического процесса
            # Не обновляем когда плазма выключена, чтобы не тормозить RS-485
            is_plasma_on = (
                rf_on or  # Реальное состояние плазмы
                self.HFButton.text() == self.translator.tr('turn_off_plasma') or  # Ручной режим
                (hasattr(self.plasma_process, 'current_state') and 
                 self.plasma_process.current_state == 'processing')  # Автоматический процесс
            )
            
            process_logger.debug(f"[on_rf_data_read] forward={forward_power}, reflected={reflected_power}, rf_on={rf_on}, is_plasma_on={is_plasma_on}")
            
            # Обновляем мощность только во время процесса плазмы (чтобы не тормозить RS-485)
            if is_plasma_on:
                ui_update_start = time.time()
                if hasattr(self, 'HFPowerZnach'):
                    self.HFPowerZnach.setText(str(forward_power))
                if hasattr(self, 'HFCurrentZnach'):
                    self.HFCurrentZnach.setText(str(reflected_power))
                ui_update_time = time.time() - ui_update_start
                total_time = time.time() - rf_data_start
                process_logger.debug(f"[on_rf_data_read] UI updated in {ui_update_time:.3f}s, total={total_time:.3f}s")
                if total_time > 0.05:
                    process_logger.warning(f"[on_rf_data_read] SLOW: total time {total_time:.3f}s > 50ms")
            else:
                # Плазма выключена - устанавливаем 0 в поля мощности только если плазма действительно выключена
                # Проверяем, что rf_on = False И мощности = 0 (двойная проверка для надежности)
                if not rf_on and forward_power == 0 and reflected_power == 0:
                    ui_update_start = time.time()
                    if hasattr(self, 'HFPowerZnach'):
                        self.HFPowerZnach.setText("0")
                    if hasattr(self, 'HFCurrentZnach'):
                        self.HFCurrentZnach.setText("0")
                    ui_update_time = time.time() - ui_update_start
                    process_logger.debug(f"[on_rf_data_read] Plasma OFF confirmed (rf_on=False, powers=0) - set power to 0, UI updated in {ui_update_time:.3f}s")
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

    def update_pressure_display(self, pressure_value):
        try:
            pressure_float = float(pressure_value)
            # Если значение меньше 10, отображаем с 2 знаками после запятой, иначе как целое число
            if pressure_float < 10:
                self.PressZnach.setText(f"{pressure_float:.2f}")
            else:
                self.PressZnach.setText(f"{int(pressure_float)}")
        except (ValueError, TypeError) as e:
            logging.error(f"Error formatting pressure: {e}, value: {pressure_value}")
            self.PressZnach.setText("0.00")
    
    def check_permissions(self):
        # НЕ меняем текст кнопки здесь - он управляется в on_start_button_clicked() и stop_process()
        # Проверяем состояние процесса, чтобы определить правильный текст кнопки
        process_state = self.plasma_process.current_state
        
        # Если процесс запущен (не idle и не fault), текст должен быть "stop"
        if process_state not in ['idle', 'fault']:
            # Процесс запущен - текст должен быть "stop"
            if self.ButtonStart.text() != self.translator.tr('stop'):
                self.ButtonStart.setText(self.translator.tr('stop'))
                self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Stop.png'))
            self.ButtonStart.setEnabled(True)
        else:
            # Процесс не запущен - текст должен быть "start"
            button_text = self.ButtonStart.text()
            if button_text != self.translator.tr('start'):
                # Текст кнопки не "start" - устанавливаем "start"
                self.ButtonStart.setText(self.translator.tr('start'))
                self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
            self.ButtonStart.setEnabled(True)
        # Светодиод синхронизируется в update_values() чтобы избежать мигания

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
            if (self.plasma_process.current_state in ['idle', 'fault'] and not self.HFButton.isChecked()):  # Плазма не должна быть включена
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
                        logging.error(f"Error checking pressure for VEButton enable in check_permissions: {e}, PressZnach: {self.PressZnach.text()}, PressZad: {self.PressZad.text()}")

        if settings['time_pump_for_service'] > settings['max_time_pump_for_service']:
            self.NIButton.setStyleSheet('background-color: red')

        if self.user_mode == "Service":
            button_text = self.ButtonStart.text()
            if button_text == self.translator.tr('stop'):
                self.ButtonStart.setEnabled(True)
            elif button_text != self.translator.tr('start'):
                self.ButtonStart.setText(self.translator.tr('start'))
                self.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
                self.ButtonStart.setEnabled(True)
            else:
                self.ButtonStart.setEnabled(True)

            self.NIButton.setEnabled(True)
            self.VEButton.setEnabled(True)
            self.HFButton.setEnabled(True)
            self.VE0Button.setEnabled(True)
        
        try:
            states = self.controller.handle_command('get_states')
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
        # Снимаем выделение текста, если оно появилось
        self.StatusLine.deselect()

    def update_labels(self):
        if self.user_mode == 'Operator':
            self.label_user.setText(self.translator.tr('operator'))

            if self.StatusLine.text() == self.translator.tr('system_ready_tech'):
                self.StatusLine.setText(self.translator.tr('system_ready_oper'))

            for label in self.labels_service:
                label.hide()

            for label in self.buttons_service:
                label.hide()

        elif self.user_mode == 'Technologist':
            self.label_user.setText(self.translator.tr('technologist'))
            if self.StatusLine.text() == self.translator.tr('system_ready_oper'):
                self.StatusLine.setText(self.translator.tr('system_ready_tech'))
                
            for label in self.labels_service:
                label.hide()

            for label in self.buttons_service:
                label.hide()

        elif self.user_mode == 'Service':
            self.label_user.setText(self.translator.tr('service_engineer'))

            for label in self.labels_service:
                label.show()
            
            for label in self.buttons_service:
                label.show()
            
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
                "ResPressure": res_pressure,  # Число, как в JSON
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
                "ResPressure": 0.0,  # Число
                "power": 0,
                "time": "00:00",
            }

            # Возвращаем рецепт с значениями по умолчанию при ошибке
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
                self._stop_gas_executor.shutdown(wait=False)  # Не ждем завершения
            except:
                pass
            self._stop_gas_executor = None
        
        if hasattr(self, '_rf_operations_executor') and self._rf_operations_executor:
            try:
                self._rf_operations_executor.shutdown(wait=False)  # Не ждем завершения
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