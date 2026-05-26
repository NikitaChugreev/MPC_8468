import os
import time
import json
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from logging.handlers import RotatingFileHandler

from PyQt5 import QtGui
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QMessageBox

from config.settings import settings
from utils.translator import Translator


work_gases = ['1', '2']
all_gases = ['1', '2', '01']

# Создаем отдельный логгер для диагностики process_processing
process_logger = logging.getLogger('process_processing')
process_logger.setLevel(logging.DEBUG)
# Отключаем распространение на корневой логгер
process_logger.propagate = False

# Создаем отдельный handler для файла process_processing.log
process_handler = RotatingFileHandler(
    filename="process_processing.log",
    maxBytes=10*1024*1024,  # 10MB
    backupCount=3,
    encoding='utf-8'
)
process_handler.setFormatter(
    logging.Formatter('%(asctime)s.%(msecs)03d - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
)
process_logger.addHandler(process_handler)

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

class PlasmaAutoProcess:
    def __init__(self, controller, parent):
        logging.info('Инициализация PlasmaAutoProcess')
        self.controller = controller
        self.parent = parent

        self.translator = Translator()
        
        self.current_state = "idle"
        self.recipe_params = None
        self.buzzer_activated = False  # Флаг для однократного включения бузера
        
        # ИЗМЕНЯЕМЫЕ
        self.pressure_history = []
        self.current_step = 0
        self.pumping_start_time = 0
        self.processing_start_time = 0
        self.venting_atm_start_time = 0
        self.attempt = 0

        # НЕИЗМЕНЯЕМЫЕ
        self.pressure_stable_threshold = 0.1
        self.percent_pressure = 0.1
        self.percent_flow = 0.1
        self.waiting_heating_rrg_sec = 10
        self.max_attempts = 150

        self.FLOW_TOLERANCE = 1
        self.TEMP_TOLERANCE = 1
        self.POWER_TOLERANCE = 10

        self.max_reflected_power = 10

        self.check_fault_timer = QTimer()
        self.check_fault_timer.timeout.connect(self.check_fault)

        self.check_stop_timer = QTimer()
        self.check_stop_timer.timeout.connect(self.check_stop_button)
        
        # Таймер для обновления времени обработки (работает независимо от основного цикла)
        self.processing_time_timer = QTimer()
        self.processing_time_timer.timeout.connect(self._update_processing_time)
        self.processing_time_timer.setSingleShot(False)
        
        # Отдельный таймер для точной проверки окончания времени (не блокируется другими операциями)
        self.processing_time_check_timer = QTimer()
        self.processing_time_check_timer.timeout.connect(self._check_processing_time_expired)
        self.processing_time_check_timer.setSingleShot(False)
        
        # ThreadPoolExecutor для асинхронного выключения плазмы при ошибках
        self._stop_plasma_executor = None
        # ThreadPoolExecutor для асинхронной проверки отраженной мощности (чтобы не блокировать основной цикл)
        self._reflected_power_executor = None
        self._reflected_power_checking = False
        # ThreadPoolExecutor для асинхронной проверки потоков (чтобы не блокировать основной цикл)
        self._flow_check_executor = None
        self._flow_checking = False
        # Отдельный executor для чтения RRG с таймаутом (чтобы не зависнуть если RRG не отвечает)
        self._rrg_read_executor = None
        # Отдельный executor для операций с RF генератором (переподключение и т.д.)
        self._rf_operations_executor = None
        
        # Прямой доступ к кнопке СТОП для немедленной реакции через gpiozero callback
        self._stop_button = None
        self._stop_button_callback_set = False
        if hasattr(self.controller, 'button_stop') and self.controller.button_stop is not None:
            self._stop_button = self.controller.button_stop
            # Устанавливаем callback для немедленной реакции на нажатие
            try:
                def stop_button_handler():
                    # Используем threading для гарантии выполнения даже если event loop заблокирован
                    import threading
                    def call_stop():
                        # Вызываем stop_process через QTimer для выполнения в главном потоке Qt
                        QTimer.singleShot(0, self._on_stop_button_pressed)
                    thread = threading.Thread(target=call_stop, daemon=True)
                    thread.start()
                
                self._stop_button.when_pressed = stop_button_handler
                self._stop_button_callback_set = True
                logging.info("Stop button callback установлен через gpiozero")
            except Exception as e:
                logging.error(f"Error setting stop button callback: {e}")
                self._stop_button_callback_set = False
        else:
            logging.warning("button_stop не доступен в controller, callback не установлен")
    
    def _on_stop_button_pressed(self):
        """Callback для немедленной реакции на нажатие физической кнопки СТОП"""
        current_state = self.current_state
        
        logging.info(f'_on_stop_button_pressed вызван: state={current_state}')
        
        # Простая логика: если процесс не в idle или fault - останавливаем
        if current_state not in ['idle', 'fault']:
            logging.info(f'Нажата физическая кнопка СТОП - остановка процесса (state={current_state})')
            self.parent.update_status(self.translator.tr('emergency_stop'))
            self.stop_process()
        else:
            logging.debug(f'_on_stop_button_pressed: процесс в состоянии {current_state}, игнорируем')
    
    def safe_get_states(self, default=None):
        """Безопасное получение состояний с проверкой на None"""
        states = self.controller.handle_command('get_states')
        if states is None:
            logging.warning("safe_get_states: get_states returned None, using empty dict")
            return {} if default is None else default
        return states
    
    def safe_get_valves_states(self, default=None):
        """Безопасное получение состояний клапанов с проверкой на None"""
        valves_states = self.controller.handle_command('get_valves_states')
        if valves_states is None:
            logging.warning("safe_get_valves_states: get_valves_states returned None, using empty dict")
            return {} if default is None else default
        return valves_states

    def get_recipe_gas(self, num_rrg):
        if self.recipe_params is None:
            return {}
        return self.recipe_params.get(f"VE{num_rrg}", {})

    def ensure_state(self, expected_states):
        if isinstance(expected_states, str):
            expected_states = [expected_states]
            
        if self.current_state not in expected_states:
            # Если состояние idle, а ожидалось init_recipe или init - это может быть нормально
            # (процесс вернулся в idle из-за ранней ошибки инициализации)
            # Логируем как warning, а не error
            if self.current_state == 'idle' and any(state in ['init_recipe', 'init'] for state in expected_states):
                logging.debug(f"ensure_state: Процесс вернулся в idle (ожидалось: {expected_states}), это нормально при ранней ошибке инициализации")
            else:
                logging.error(f"Недопустимое состояние: {self.current_state}, ожидалось: {expected_states}")
            return False
        return True

    def check_fault(self):
        start_process_logger.debug(f"[CHECK_FAULT] check_fault: current_state={self.current_state}")
        if self.current_state == 'fault':
            start_process_logger.info(f"[CHECK_FAULT] check_fault: Процесс в состоянии fault, останавливаем таймер и вызываем process_fault через 1 секунду")
            self.check_fault_timer.stop()
            QTimer.singleShot(1000, self.process_fault)
    
    def check_stop_button(self):
        """Резервная проверка кнопки через таймер (используется только если callback не работает)"""
        current_state = self.current_state
        
        # Проверяем кнопку напрямую через gpiozero
        button_pressed = False
        if self._stop_button is not None:
            try:
                button_pressed = self._stop_button.is_pressed
            except Exception as e:
                start_process_logger.error(f"[CHECK_STOP_BUTTON] check_stop_button: Error reading stop button directly from gpiozero: {e}")
                return False
        else:
            # Если кнопка не инициализирована, таймер не должен работать
            start_process_logger.warning("[CHECK_STOP_BUTTON] check_stop_button: _stop_button is None, stopping timer")
            self.check_stop_timer.stop()
            return False

        start_process_logger.debug(f"[CHECK_STOP_BUTTON] check_stop_button: button_pressed={button_pressed}, current_state={current_state}")

        # Простая логика: если кнопка нажата и процесс не в idle или fault - останавливаем
        if button_pressed and current_state not in ['idle', 'fault']:
            start_process_logger.warning(f'[CHECK_STOP_BUTTON] check_stop_button: Нажата кнопка СТОП (через таймер) - остановка процесса (state={current_state})')
            self.parent.update_status(self.translator.tr('emergency_stop'))
            self.stop_process()
            return True
        
        # Если кнопка стоп нажата, но процесс в idle - это означает, что кнопка осталась нажатой после остановки
        # Игнорируем это, чтобы не блокировать следующий запуск
        if button_pressed and current_state == 'idle':
            start_process_logger.debug(f'[CHECK_STOP_BUTTON] check_stop_button: кнопка стоп нажата, но процесс в idle - игнорируем (кнопка осталась нажатой после остановки)')
            return False
        
        return False

    def process_fault(self):
        if not self.ensure_state('fault'):
            return 

        try:
            logging.info(f"Выполнение process_fault: Current_state: {self.current_state}, current_step: {self.current_step}, attempt: {self.attempt}")

            states = self.safe_get_states()

            if self.current_step == 0:
                self.parent.ButtonStart.setEnabled(False)
                self.parent.RecName.deselect()

                self.attempt = 0
                self.current_step += 1
                QTimer.singleShot(1000, self.process_fault)

            elif self.current_step == 1:
                # ВАЖНО: Останавливаем поток чтения RF ПЕРЕД обращением к генератору
                # Это предотвращает ошибки I/O при попытке выключить плазму
                if hasattr(self.parent, 'stop_rf_reading'):
                    logging.info("[process_fault] STEP 1: Stopping RF reading thread before off_plasma...")
                    self.parent.stop_rf_reading(wait=True)  # Ждем завершения, чтобы порт точно освободился
                    time.sleep(1.0)  # Задержка для гарантии полного освобождения порта
                    
                    # Проверяем, что блокировка порта освобождена
                    if self.controller.rf is not None and hasattr(self.controller.rf, '_lock'):
                        if self.controller.rf._lock.acquire(blocking=False):
                            self.controller.rf._lock.release()
                            logging.info("[process_fault] STEP 1: RF port lock is available")
                        else:
                            logging.warning("[process_fault] STEP 1: RF port lock is busy, waiting...")
                            if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                                self.controller.rf._lock.release()
                                logging.info("[process_fault] STEP 1: RF port lock released after wait")
                
                # Проверяем реальный статус плазмы напрямую из генератора
                try:
                    rf_status = self.controller.rf.read_status() if self.controller.rf is not None else None
                except Exception:
                    rf_status = None
                try:
                    if rf_status:
                        rf_on = rf_status.get('rf_on', False)
                        if rf_on:
                            # Плазма включена - выключаем
                            self.controller.handle_command('off_plasma')
                            logging.info(self.translator.tr('plasma_off'))
                            self.parent.update_status(self.translator.tr('plasma_off'))
                            time.sleep(0.5)  # Задержка перед проверкой
                    else:
                        # Не удалось прочитать статус - пытаемся переподключиться
                        def reconnect_rf_async():
                            try:
                                logging.info("[process_fault] Attempting to reconnect RF generator (read_status returned None)...")
                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                if reconnect_success:
                                    logging.info("[process_fault] RF generator reconnected successfully")
                                else:
                                    logging.warning(f"[process_fault] Failed to reconnect RF generator: {reconnect_msg}")
                            except Exception as reconnect_error:
                                logging.error(f"[process_fault] Error reconnecting RF generator: {reconnect_error}")
                        
                        if self._rf_operations_executor is None:
                            self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                        self._rf_operations_executor.submit(reconnect_rf_async)
                        
                        # Пытаемся выключить на всякий случай
                        self.controller.handle_command('off_plasma')
                        logging.info(self.translator.tr('plasma_off'))
                        self.parent.update_status(self.translator.tr('plasma_off'))
                        time.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error reading RF status in process_fault step 1: {e}")
                    # Пытаемся переподключиться при ошибке
                    def reconnect_rf_async():
                        try:
                            logging.info("[process_fault] Attempting to reconnect RF generator (read_status error)...")
                            reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                            if reconnect_success:
                                logging.info("[process_fault] RF generator reconnected successfully after error")
                            else:
                                logging.warning(f"[process_fault] Failed to reconnect RF generator after error: {reconnect_msg}")
                        except Exception as reconnect_error:
                            logging.error(f"[process_fault] Error reconnecting RF generator after error: {reconnect_error}")
                    
                    if self._rf_operations_executor is None:
                        self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                    self._rf_operations_executor.submit(reconnect_rf_async)
                    
                    # При ошибке пытаемся выключить на всякий случай
                    self.controller.handle_command('off_plasma')
                    time.sleep(0.5)

                # Проверяем статус после выключения
                try:
                    rf_status = self.controller.rf.read_status() if self.controller.rf is not None else None
                    if rf_status:
                        rf_on = rf_status.get('rf_on', True)  # По умолчанию True, если не удалось прочитать
                        if not rf_on:
                            # Обновляем кэш
                            self.controller._cached_plasma_status = False
                            self.attempt = 0
                            self.current_step += 1
                        else:
                            self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_off_plasma')}")
                            self.attempt += 1
                    else:
                        # Не удалось прочитать статус — нельзя считать плазму выключенной
                        def reconnect_rf_async():
                            try:
                                logging.info("[process_fault] Attempting to reconnect RF generator (status check returned None)...")
                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                if reconnect_success:
                                    logging.info("[process_fault] RF generator reconnected successfully (status check)")
                                else:
                                    logging.warning(f"[process_fault] Failed to reconnect RF generator (status check): {reconnect_msg}")
                            except Exception as reconnect_error:
                                logging.error(f"[process_fault] Error reconnecting RF generator (status check): {reconnect_error}")
                        
                        if self._rf_operations_executor is None:
                            self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                        self._rf_operations_executor.submit(reconnect_rf_async)
                        
                        self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_off_plasma')}")
                        self.attempt += 1
                except Exception as e:
                    logging.error(f"Error checking RF status after off_plasma in process_fault step 1: {e}")
                    # При ошибке чтения статуса нельзя считать плазму выключенной — повторяем попытку
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_off_plasma')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_turn_off_plasma'), need_reboot=True)
                    return 
                
                QTimer.singleShot(1000, self.process_fault)

            elif self.current_step == 2:
                self.attempt = 0
                self.current_step += 1

                QTimer.singleShot(1000, self.process_fault)

            elif self.current_step == 3:
                valves_states = self.safe_get_valves_states()
                for i in all_gases:
                    if valves_states.get(f"valve_ve{i}", 'open') == 'open':
                        self.controller.handle_command(f"close_valve_ve{i}")
                
                is_valid = True
                for i in all_gases:
                    valves_check = self.safe_get_valves_states()
                    if valves_check.get(f"valve_ve{i}", 'close') == 'open':
                        is_valid = False
                
                if is_valid:
                    self.attempt = 0
                    self.current_step += 1
                    self.parent.update_status(self.translator.tr('valves_close'))
                    logging.info(self.translator.tr('valves_close'))
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_close_valves')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_close_valves'), need_reboot=True)
                    return

                QTimer.singleShot(1000, self.process_fault)
        
            elif self.current_step == 4:
                if states.get('pump', 1):
                    self.controller.handle_command('off_pump')
                    logging.info(self.translator.tr('pump_off'))
                    self.parent.update_status(self.translator.tr('pump_off'))

                states_check = self.safe_get_states()
                if states_check.get('pump', 1) == 0:
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_off_pump')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_turn_off_pump'), need_reboot=True)
                    return 

                QTimer.singleShot(1000, self.process_fault)

            elif self.current_step == 5:
                # Устанавливаем текст кнопки на "start" только если процесс действительно остановлен
                # Проверяем, что процесс прошел все шаги остановки (current_step == 7)
                # Если процесс только что запустился и сразу перешел в fault, то current_step будет 0
                # и мы не должны менять текст кнопки, чтобы пользователь мог остановить процесс
                button_text = self.parent.ButtonStart.text()
                
                # Меняем текст только если процесс прошел все шаги остановки (current_step == 7)
                # и текст кнопки уже "stop" (значит процесс был запущен)
                if button_text == self.translator.tr('stop'):
                    # Процесс был запущен и остановлен, можно менять текст
                    self.parent.ButtonStart.setText(self.translator.tr('start'))
                    self.parent.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
                    self.parent.ButtonStart.setEnabled(True)
                    self.parent.RecName.deselect()
                    # LED будет включен в update_values() если кнопка активна и текст = "start"
                    logging.info(f"process_fault step 7: Changed button text to 'start' (process was running)")
                else:
                    # Текст кнопки уже "start" или что-то другое - не меняем
                    # Это может быть, если процесс только что запустился и сразу перешел в fault
                    logging.warning(f"process_fault step 7: Not changing button text, button_text='{button_text}' (process may have just started)")

                logging.info(self.translator.tr('emergency_stop_completed'))
                self.parent.update_status(self.translator.tr('emergency_stop_completed'))

                self.attempt = 0
                self.current_step += 1
                QTimer.singleShot(1000, self.process_fault)
            
            elif self.current_step == 6:
                self.current_state = "idle"
                self.attempt = 0
                self.current_step = 0

                self.parent.update_status(self.translator.tr('system_ready'))
                self.parent.ButtonStart.setText(self.translator.tr('start'))
                self.parent.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))

        except Exception as e:
            self.handle_error(f"{self.translator.tr('error_emergency_stop')}: {e}", need_reboot=True)
            return

    def start_process(self):
        if self.current_state == "idle":
            # Проверка потока воды перед запуском процесса (можно отключить в настройках check_water_flow: false)
            if settings.get('check_water_flow', True):
                try:
                    water_flow = self.controller.handle_command('get_sensor_water')
                    # Если None - датчик еще не выполнил первое измерение, пропускаем проверку
                    # Если 0.0 - поток действительно равен нулю, это ошибка
                    if water_flow is None:
                        logging.info(f"start_process: Water sensor not ready yet (first measurement in progress), skipping check")
                    elif water_flow == 0.0:
                        self.handle_error(self.translator.tr('error_water_flow_zero'), need_reboot=False)
                        logging.error(f"start_process: Water flow is zero (flow={water_flow}). Process cannot start.")
                        # Явно возвращаем состояние в idle после ошибки проверки потока,
                        # чтобы можно было повторить попытку запуска после включения воды
                        self.current_state = "idle"
                        return False
                    else:
                        logging.info(f"start_process: Water flow check passed: {water_flow} л/мин")
                except Exception as e:
                    logging.error(f"start_process: Error checking water flow: {e}")
                    self.handle_error(f"{self.translator.tr('error_checking_water_flow')}: {e}", need_reboot=False)
                    # Явно возвращаем состояние в idle после ошибки проверки потока
                    self.current_state = "idle"
                    return False

            # Просто запускаем процесс
            logging.info("start_process: Запуск процесса")
            self.current_state = "init"
            self.attempt = 0
            self.current_step = 0
            self.buzzer_activated = False  # Сбрасываем флаг при старте нового процесса
            self.process_init()
            # НЕ включаем LED здесь - он будет включен в update_values() если кнопка активна и текст = "start"
            return True
        else:
            logging.warning(f"start_process: current_state is not 'idle', it is '{self.current_state}'")
            return False

    def process_init(self):
        if not self.ensure_state('init'):
            return

        def check_i2c_devices():
            return True
            # devices = self.controller.check_devices()
            # if len(devices) == 3:
            #     return True
            # else:
            #     return devices
        
        try:
            logging.info(f"Выполнение process_init: Current_state: {self.current_state}, current_step: {self.current_step}, attempt: {self.attempt}")
            
            if self.current_step == 0:

                is_valid = True

                for i in all_gases:
                    self.controller.handle_command(f"close_valve_ve{i}")

                for i in all_gases:
                    valves_check = self.safe_get_valves_states()
                    if valves_check.get(f"valve_ve{i}", 'close') == 'open':
                        is_valid = False
                
                if is_valid:
                    self.parent.update_status(self.translator.tr('valves_close'))
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_close_valves')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_valve_close'), need_reboot=True)
                    return

                QTimer.singleShot(1000, self.process_init)
                
            elif self.current_step == 1:
                pressure = self.controller.handle_command('get_sensor_pressure')
                if pressure is None:
                    self.attempt += 1
                    if self.attempt > self.max_attempts:
                        self.attempt = 0
                        self.handle_error(self.translator.tr('error_sensor_pressure_not_valid'), need_reboot=True)
                        return
                    QTimer.singleShot(1000, self.process_init)
                    return
                if pressure != 0:
                    self.parent.update_status(self.translator.tr('sensor_pressure_valid'))
                    self.attempt = 0
                    self.current_step += 1
                    QTimer.singleShot(1000, self.process_init)
                else:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_sensor_pressure_not_valid'), need_reboot=True)
                    return
                    
            elif self.current_step == 2:
                devices = check_i2c_devices()

                if devices:
                    self.current_state = "idle"
                    self.attempt = 0
                    self.current_step = 0
                    self.parent.update_time_znach('00:00')

                    if self.parent.user_mode == 'Operator' and self.parent.current_recipe is None:
                        self.parent.update_status(self.translator.tr('system_ready_oper'))
                    else:
                        self.parent.update_status(self.translator.tr('system_ready_tech'))

                    self.parent.ButtonStart.setEnabled(True)
                    # НЕ включаем LED здесь - он будет включен в update_values() если кнопка активна и текст = "start"
                else:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_i2c'), need_reboot=True)
                    logging.error(devices)
                    return
                    
        except Exception as e:
            self.handle_error(f"{self.translator.tr('error_init')}: {e}", need_reboot=True)
            return

    def start_recipe(self):
        if self.current_state == "idle":
            # ВАЖНО: Проверяем кнопку стоп САМЫМ ПЕРВЫМ, до всех проверок и установки времени
            # Это защита от физического замыкания кнопки стоп при нажатии кнопки старт
            if self._stop_button is not None:
                try:
                    # Проверяем несколько раз подряд, чтобы убедиться, что кнопка действительно не нажата
                    # (защита от дребезга контактов)
                    check_count = 0
                    max_checks = 5
                    for i in range(max_checks):
                        if self._stop_button.is_pressed:
                            check_count += 1
                        time.sleep(0.05)  # Небольшая задержка между проверками
                    
                    if check_count > 0:
                        logging.warning(f"start_recipe: Кнопка СТОП обнаружена как нажатая ({check_count}/{max_checks} проверок), ожидаем отпускания...")
                        # Ждем отпускания кнопки стоп (максимум 2 секунды)
                        wait_start = time.time()
                        while self._stop_button.is_pressed and (time.time() - wait_start) < 2.0:
                            time.sleep(0.1)
                        if self._stop_button.is_pressed:
                            logging.error("start_recipe: Кнопка СТОП все еще нажата после ожидания, отменяем запуск")
                            return False
                        logging.info(f"start_recipe: Кнопка СТОП отпущена после {time.time() - wait_start:.3f}s ожидания, продолжаем запуск")
                except Exception as e:
                    logging.warning(f"start_recipe: Не удалось проверить состояние кнопки СТОП: {e}")
            
            # Проверка потока воды перед запуском процесса (можно отключить в настройках check_water_flow: false)
            if settings.get('check_water_flow', True):
                try:
                    water_flow = self.controller.handle_command('get_sensor_water')
                    # Если None - датчик еще не выполнил первое измерение, пропускаем проверку
                    # Если 0.0 - поток действительно равен нулю, это ошибка
                    if water_flow is None:
                        logging.info(f"start_recipe: Water sensor not ready yet (first measurement in progress), skipping check")
                    elif water_flow == 0.0:
                        self.handle_error(self.translator.tr('error_water_flow_zero'), need_reboot=False)
                        logging.error(f"start_recipe: Water flow is zero (flow={water_flow}). Process cannot start.")
                        # Явно возвращаем состояние в idle после ошибки проверки потока,
                        # чтобы можно было повторить попытку запуска после включения воды
                        self.current_state = "idle"
                        return False
                    else:
                        logging.info(f"start_recipe: Water flow check passed: {water_flow} л/мин")
                except Exception as e:
                    logging.error(f"start_recipe: Error checking water flow: {e}")
                    self.handle_error(f"{self.translator.tr('error_checking_water_flow')}: {e}", need_reboot=False)
                    # Явно возвращаем состояние в idle после ошибки проверки потока
                    self.current_state = "idle"
                    return False

            # Просто запускаем процесс
            start_process_logger.info(f"[START_RECIPE] start_recipe: Запуск процесса, устанавливаем состояние init_recipe")
            self.current_state = "init_recipe"
            self.attempt = 0
            self.current_step = 0
            start_process_logger.info(f"[START_RECIPE] start_recipe: current_state={self.current_state}, current_step={self.current_step}, attempt={self.attempt}")
            self.check_fault_timer.start(500)
            # Запускаем таймер проверки кнопки стоп сразу при старте процесса
            if not self.check_stop_timer.isActive():
                if not self._stop_button_callback_set:
                    self.check_stop_timer.start(200)  # Более частая проверка если нет callback
                    logging.warning("check_stop_timer started in start_recipe (callback not set)")
                else:
                    self.check_stop_timer.start(2000)  # Резервная проверка каждые 2 секунды
                    logging.info("check_stop_timer started in start_recipe (callback is set)")

            # LED будет синхронизирован в main_window.check_permissions() или update_values()
            # на основе состояния кнопки (enabled и текст)

            start_process_logger.info(f"[START_RECIPE] start_recipe: Вызываем process_init_recipe()")
            self.process_init_recipe()
            start_process_logger.info(f"[START_RECIPE] start_recipe: process_init_recipe() вызван, возвращаем True")
            return True
        else:
            start_process_logger.warning(f"[START_RECIPE] start_recipe: current_state is not 'idle', it is '{self.current_state}', возвращаем False")
            return False

    def process_init_recipe(self):
        start_process_logger.info(f"[PROCESS_INIT_RECIPE] process_init_recipe: ВХОД, current_state={self.current_state}, current_step={self.current_step}, attempt={self.attempt}")
        if not self.ensure_state('init_recipe'):
            start_process_logger.warning(f"[PROCESS_INIT_RECIPE] process_init_recipe: ensure_state вернул False, выходим")
            return

        try:
            start_process_logger.info(f"[PROCESS_INIT_RECIPE] process_init_recipe: Выполнение, Current_state: {self.current_state}, current_step: {self.current_step}, attempt: {self.attempt}")

            if self.current_step == 0:
                self.recipe_params = self.parent.get_current_recipe()
                self.parent.update_status(self.translator.tr('init'))
                conditions_error = False

                if self.recipe_params is None:
                    conditions_error = True
                else:
                    power_value = float(self.recipe_params.get('power', 0))
                    conditions_error = any([abs(power_value) < self.POWER_TOLERANCE, 
                                            self.recipe_params.get('time', '00:00') == '00:00'])
                    
                if conditions_error:
                    start_process_logger.error(f"[PROCESS_INIT_RECIPE] process_init_recipe step 0: Ошибка валидации рецепта, вызываем handle_error")
                    self.handle_error(self.translator.tr('error_invalide_recipe'))
                    return

                start_process_logger.info(f"[PROCESS_INIT_RECIPE] process_init_recipe step 0: Рецепт валиден, переходим к шагу 1 через 1 секунду")
                self.attempt = 0
                self.current_step += 1
                QTimer.singleShot(1000, self.process_init_recipe)

            elif self.current_step == 1:
                states = self.controller.handle_command('get_states')
                
                # Проверяем, что states не None
                if states is None:
                    logging.error("process_init_recipe: get_states returned None")
                    states = {}

                if states.get('pump', 1):
                    self.controller.handle_command('off_pump')

                valves_states = self.safe_get_valves_states()
                for i in all_gases:
                    if valves_states.get(f"valve_ve{i}", 'open') == 'open':
                        self.controller.handle_command(f"close_valve_ve{i}")

                # ВАЖНО: Останавливаем поток чтения RF ПЕРЕД обращением к генератору (если он запущен)
                # Это предотвращает ошибки I/O при попытке выключить плазму
                if hasattr(self.parent, 'stop_rf_reading'):
                    logging.info("[process_init_recipe] Stopping RF reading thread before off_plasma (if running)...")
                    self.parent.stop_rf_reading(wait=True)  # Ждем завершения, чтобы порт точно освободился
                    time.sleep(0.5)  # Небольшая задержка для гарантии освобождения порта
                
                if states.get('plasma', 1):
                    self.controller.handle_command('off_plasma')

                is_valid = True
                fault_devices = []
                states = self.controller.handle_command('get_states')
                
                # Проверяем, что states не None
                if states is None:
                    logging.error("process_init_recipe: get_states returned None (second call)")
                    states = {}

                if states.get('pump', 1):
                    is_valid = False
                    fault_devices.append('pump')
                
                valves_states_check = self.safe_get_valves_states()
                for i in all_gases:
                    if valves_states_check.get(f"valve_ve{i}", 'close') == 'open':
                        is_valid = False
                        fault_devices.append(f"valve_ve{i}")
                
                if states.get('plasma', 1):
                    is_valid = False
                    fault_devices.append('plasma')

                self.parent.NIButton.setEnabled(False)
                self.parent.VEButton.setEnabled(False)
                self.parent.HFButton.setEnabled(False)
                self.parent.VE0Button.setEnabled(False)
                self.parent.HFButton.setEnabled(False)

                self.pressure_history = []
                self.pumping_start_time = 0
                self.processing_start_time = 0
                self.venting_atm_start_time = 0

                if is_valid:
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_init')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    start_process_logger.error(f"[PROCESS_INIT_RECIPE] process_init_recipe step 1: Превышено максимальное количество попыток, fault_devices={fault_devices}, вызываем handle_error")
                    self.handle_error(self.translator.tr('error_init_devices'), need_reboot=True)
                    return
                
                start_process_logger.info(f"[PROCESS_INIT_RECIPE] process_init_recipe step 1: Продолжаем, attempt={self.attempt}, is_valid={is_valid}, "
                           f"переходим к следующему шагу через 1 секунду")
                QTimer.singleShot(1000, self.process_init_recipe)

            elif self.current_step == 2:
                # Открываем клапаны выбранных рабочих газов для прогрева РРГ
                is_valid = True

                for i in work_gases:
                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                    if recipe_gas.get('switch', 0):
                        self.controller.handle_command(f"open_valve_ve{i}")

                for i in work_gases:
                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                    a = recipe_gas.get('switch', 0)
                    valves_check = self.safe_get_valves_states()
                    b = valves_check.get(f"valve_ve{i}", 'open') == 'close'
                    if a and b:
                        is_valid = False
            
                if is_valid:
                    self.attempt = 0
                    self.current_step += 1
                    self.parent.update_status(self.translator.tr('valves_open'))
                    QTimer.singleShot(1000, lambda: self.parent.update_status(self.translator.tr('waiting_heating_mfc')))
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_open_valves')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_open_valves'), need_reboot=True)
                    return
                
                QTimer.singleShot(1000, self.process_init_recipe)

            elif self.current_step == 3:
                # Выставляем макс поток для прогрева РРГ
                is_valid = True

                for i in work_gases:
                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                    # Обрабатываем None: если gas = None, используем 0 (Air)
                    type_gas = recipe_gas.get('gas') if recipe_gas.get('gas') is not None else 0
                    if recipe_gas.get('switch', 0):
                        self.controller.handle_command('set_flow', 
                                                       num_rrg=i, 
                                                       flow_lh=settings.get('MAX_FLOW_RRG', 0), 
                                                       type_gas=type_gas)
                    else:
                        self.controller.handle_command('set_flow', 
                                                       num_rrg=i, 
                                                       flow_lh=0, 
                                                       type_gas=type_gas)
                    time.sleep(0.05)  # Небольшая задержка между операциями для предотвращения перегрузки serial порта

                time.sleep(0.1)  # Задержка перед чтением для стабилизации

                for i in work_gases:
                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                    if recipe_gas.get('switch', 0):
                        # Обрабатываем None: если gas = None, используем 0 (Air)
                        type_gas = recipe_gas.get('gas') if recipe_gas.get('gas') is not None else 0
                        set_flow = self.controller.handle_command('read_set_flow', num_rrg=i, type_gas=type_gas)
                        if set_flow is None:
                            is_valid = False
                            continue
                        max_flow = float(settings.get('MAX_FLOW_RRG', 0))
                        if abs(float(set_flow) - max_flow) > self.FLOW_TOLERANCE:
                            is_valid = False
                    time.sleep(0.05)  # Небольшая задержка между операциями

                if is_valid:
                    QTimer.singleShot(1000, lambda: self.parent.update_status(self.translator.tr('waiting_heating_mfc')))
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_set_flow')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_setting_flow'), need_reboot=True)
                    return
                
                QTimer.singleShot(1000, self.process_init_recipe)

            elif self.current_step == 4:
                # Ожидание прогрева РРГ
                self.attempt = 0
                self.current_step += 1
                QTimer.singleShot(self.waiting_heating_rrg_sec * 1000, self.process_init_recipe)

            elif self.current_step == 5:
                # Закрываем все клапаны рабочих газов
                is_valid = True

                for i in work_gases:
                    self.controller.handle_command(f"close_valve_ve{i}")

                for i in work_gases:
                    valves_check = self.safe_get_valves_states()
                    if valves_check.get(f"valve_ve{i}", 'close') == 'open':
                        is_valid = False
                
                if is_valid:
                    self.parent.update_status(self.translator.tr('valves_close'))
                    self.attempt = 0
                    self.current_step += 1
                    # Запускаем таймер проверки кнопки стоп только как резервный механизм,
                    # если callback через gpiozero не установлен
                    if not self.check_stop_timer.isActive():
                        if not self._stop_button_callback_set:
                            # Если callback не установлен, используем таймер как основной механизм
                            self.check_stop_timer.start(200)  # Более частая проверка если нет callback
                            logging.warning("check_stop_timer started as primary mechanism (callback not set)")
                        else:
                            # Если callback установлен, запускаем таймер с большим интервалом как резерв
                            self.check_stop_timer.start(2000)  # Резервная проверка каждые 2 секунды
                            logging.info("check_stop_timer started as backup mechanism (callback is set)")
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_close_valves')}")
                    self.attempt += 1
                    
                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_close_valves'), need_reboot=True)
                    return

                QTimer.singleShot(1000, self.process_init_recipe)

            elif self.current_step == 6:
                # Установка потока в РРГ и проверка
                is_valid = True
                self.parent.update_status(self.translator.tr('waiting_setting_flow'))

                # Сначала устанавливаем потоки для всех РРГ с проверкой успешности
                set_flow_success = True
                for i in work_gases:
                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                    if recipe_gas.get('switch', 0):
                        flow_value = float(recipe_gas.get('flow', 0))
                        # Обрабатываем None: если gas = None, используем 0 (Air)
                        type_gas = recipe_gas.get('gas') if recipe_gas.get('gas') is not None else 0
                        result = self.controller.handle_command('set_flow', num_rrg=i, flow_lh=flow_value, type_gas=type_gas)
                        if not result:
                            logging.warning(f"Ошибка установки потока для РРГ {i}: set_flow вернул False")
                            set_flow_success = False
                    else:
                        # Обрабатываем None: если gas = None, используем 0 (Air)
                        type_gas = recipe_gas.get('gas') if recipe_gas.get('gas') is not None else 0
                        result = self.controller.handle_command('set_flow', num_rrg=i, flow_lh=0, type_gas=type_gas)
                        if not result:
                            logging.warning(f"Ошибка установки потока 0 для РРГ {i}: set_flow вернул False")
                            set_flow_success = False
                    time.sleep(0.1)  # Увеличена задержка между операциями для предотвращения перегрузки serial порта
                
                if not set_flow_success:
                    is_valid = False
                    logging.error("Ошибка: не удалось установить потоки в РРГ")
                else:
                    # Увеличена задержка перед чтением для стабилизации
                    time.sleep(0.3)
                    
                    # Проверяем установленные потоки
                    for i in work_gases:
                        recipe_gas = self.recipe_params.get(f"VE{i}", {})
                        recipe_flow = float(recipe_gas.get('flow', 0))
                        
                        # Обрабатываем None: если gas = None, используем 0 (Air)
                        type_gas = recipe_gas.get('gas') if recipe_gas.get('gas') is not None else 0
                        rrg_set_flow = self.controller.handle_command('read_set_flow', num_rrg=i, type_gas=type_gas)
                        
                        if rrg_set_flow is None:
                            is_valid = False
                            logging.error(f"Ошибка чтения потока для РРГ {i}: read_set_flow вернул None")
                            continue
                            
                        try:
                            rrg_set_flow = float(rrg_set_flow)
                        except (ValueError, TypeError):
                            rrg_set_flow = 0
                            is_valid = False
                            logging.error(f"Ошибка преобразования потока для РРГ {i}: {rrg_set_flow}")
                            continue
                            
                        if recipe_gas.get('switch', 0):
                            diff = abs(rrg_set_flow - recipe_flow)
                            if diff > self.FLOW_TOLERANCE: 
                                is_valid = False
                                logging.error(f"Ошибка установки потока для РРГ {i}: recipe_flow: {recipe_flow}, rrg_set_flow: {rrg_set_flow}, diff: {diff:.2f}, tolerance: {self.FLOW_TOLERANCE}")
                        else:
                            if abs(rrg_set_flow - 0.0) > self.FLOW_TOLERANCE:
                                is_valid = False
                                logging.error(f"Ошибка: РРГ {i} должен быть 0, но установлен {rrg_set_flow}")
                        time.sleep(0.1)  # Задержка между операциями
                            
                if is_valid:
                    self.attempt = 0
                    self.current_step += 1
                    self.parent.update_status(self.translator.tr('success_setting_flow'))
                    logging.info("Успешно установлены потоки в РРГ")
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_set_flow')}")
                    self.attempt += 1
                    logging.warning(f"Попытка {self.attempt} установки потока не удалась")

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_setting_flow'), need_reboot=True)
                    return
                
                QTimer.singleShot(1000, self.process_init_recipe)
                
            elif self.current_step == 7:
                recipe_power = self.recipe_params.get('power', 0)
                logging.info(f"Setting power: recipe_power={recipe_power}, type={type(recipe_power)}")
                
                # Проверяем, что мощность валидна
                try:
                    power_value = int(float(recipe_power))
                    if power_value < 10 or power_value > settings.get('MAX_POWER_BP', 1000):
                        logging.error(f"Power value {power_value} is out of valid range (10-{settings.get('MAX_POWER_BP', 1000)})")
                        self.handle_error(self.translator.tr('error_set_power'), need_reboot=True)
                        return
                except (ValueError, TypeError) as e:
                    logging.error(f"Invalid power value: {recipe_power}, error: {e}")
                    self.handle_error(self.translator.tr('error_set_power'), need_reboot=True)
                    return
                
                # Пытаемся установить мощность с проверкой успешности
                success = False
                for attempt in range(3):
                    result = self.controller.handle_command('set_power', power=power_value)
                    if result:
                        success = True
                        logging.info(f"Power set successfully: {power_value}W (attempt {attempt + 1})")
                        break
                    else:
                        logging.warning(f"Failed to set power on attempt {attempt + 1}")
                        if attempt < 2:
                            time.sleep(0.2)  # Задержка между попытками
                
                if not success:
                    logging.error(f"Failed to set power after 3 attempts")
                    self.handle_error(self.translator.tr('error_set_power'), need_reboot=True)
                    return

                self.parent.update_status(self.translator.tr('set_power'))
                self.attempt = 0

                
                self.current_step += 1

                QTimer.singleShot(1000, self.process_init_recipe)

            elif self.current_step == 8:
                self.attempt = 0
                self.current_step += 1
                self.current_state = 'pumping'
                QTimer.singleShot(1000, self.process_pumping)
                return           
                
        except Exception as e:
            self.handle_error(f"{self.translator.tr('error_init_recipe')}: {e}.", need_reboot=True)
            return

    def process_pumping(self):
        if not self.ensure_state('pumping'):
            return 

        if self.check_stop_button():
            return
        
        if self.recipe_params is None:
            self.handle_error(self.translator.tr('error_invalide_recipe'), need_reboot=True)
            return

        try:
            logging.info(f"Выполнение process_pumping: Current_state: {self.current_state}, current_step: {self.current_step}, attempt: {self.attempt}")

            # Переход из init_recipe (step 9): сбрасываем шаг для логики pumping (0 = насос, 1 = ждать давление)
            if self.current_step not in (0, 1):
                self.current_step = 0

            target_pressure = float(self.recipe_params.get('ResPressure', 0))

            if self.current_step == 0:
                self.controller.handle_command('on_pump')

                states = self.safe_get_states()
                if states.get('pump', 0):
                    self.parent.update_status(self.translator.tr('pump_on'))
                    self.attempt = 0
                    self.current_step += 1
                    self.pumping_start_time = time.time()
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_on_pump')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_turn_on_pump'), need_reboot=True)
                    return

                QTimer.singleShot(1000, self.process_pumping)

            elif self.current_step == 1:
                if self.pumping_start_time == 0:
                    self.current_step = 0
                    QTimer.singleShot(1000, self.process_pumping)
                    return
                
                current_pressure = self.controller.handle_command('get_sensor_pressure')
                self.parent.update_pressure_display(current_pressure)

                elapsed_time = int(time.time() - self.pumping_start_time)
                
                if elapsed_time > settings.get('time_pump', 20) * 60:
                    self.handle_error(self.translator.tr('error_pumpdown_time_exceeded'))
                    return
                if current_pressure is None:
                    logging.warning("process_pumping: get_sensor_pressure returned None, retrying")
                    QTimer.singleShot(1000, self.process_pumping)
                    return
                if current_pressure <= target_pressure:
                    self.attempt = 0
                    self.current_step = 0

                    self.parent.update_status(self.translator.tr('pumping_end_start_venting'))
                    self.current_state = 'venting'
                    QTimer.singleShot(1000, self.process_venting)
                else:
                    # Форматирование давления: если < 10, то 2 знака после запятой, иначе целое число
                    if current_pressure < 10:
                        self.parent.PressZnach.setText(f"{current_pressure:.2f}")
                    else:
                        self.parent.PressZnach.setText(f"{int(current_pressure)}")
                    minutes, seconds = elapsed_time // 60, elapsed_time % 60
                    time_str = f"{minutes:02d}:{seconds:02d}"
                    self.parent.update_display_time(time_str)

                    QTimer.singleShot(1000, self.process_pumping)
                    
        except Exception as e:
            self.handle_error(f"{self.translator.tr('error_pumping')}: {e}", need_reboot=True)
            return

    def process_venting(self):
        if not self.ensure_state('venting'):
            return 

        if self.check_stop_button():
            return
        
        if self.recipe_params is None:
            self.handle_error(self.translator.tr('error_invalide_recipe'), need_reboot=True)
            return

        try:
            logging.info(f"Выполнение process_venting: Current_state: {self.current_state}, current_step: {self.current_step}, attempt: {self.attempt}")

            if self.current_step == 0:
                for i in work_gases:
                    self.controller.handle_command(f"close_valve_ve{i}")

                is_valid = True
                for i in work_gases:
                    valves_check = self.safe_get_valves_states()
                    if valves_check.get(f"valve_ve{i}", 'close') == 'open':
                        is_valid = False

                if is_valid:
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_open_valves')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_close_valves'), need_reboot=True)
                    return
                
                QTimer.singleShot(1000, self.process_venting)
                
            elif self.current_step == 1:
                for i in work_gases:
                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                    if recipe_gas.get('switch', 0):
                        self.controller.handle_command(f"open_valve_ve{i}")

                is_valid = True

                valves_states = self.safe_get_valves_states()
                for i in work_gases:
                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                    if recipe_gas.get('switch', 0) and valves_states.get(f"valve_ve{i}", 'open') == 'close':
                        is_valid = False

                if is_valid:
                    self.parent.update_status(self.translator.tr('valves_open'))
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_open_valves')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_open_valves'), need_reboot=True)
                    return

                QTimer.singleShot(1000, self.process_venting)
            
            elif self.current_step == 2:
                is_valid = True

                for i in work_gases:
                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                    
                    try:
                        # Обрабатываем None: если gas = None, используем 0 (Air)
                        type_gas = recipe_gas.get('gas') if recipe_gas.get('gas') is not None else 0
                        flow_rrg = self.controller.handle_command('read_flow', num_rrg=i, type_gas=type_gas)
                        if flow_rrg is None:
                            flow_rrg = 0
                        else:
                            flow_rrg = float(flow_rrg)
                    except (ValueError, TypeError, Exception) as e:
                        flow_rrg = 0
                        logging.error(f"Ошибка чтения потока РРГ {i}: {e}")
                        
                    flow_recipe = float(recipe_gas.get('flow', 0))

                    if recipe_gas.get('switch', 0):
                        if flow_recipe == 0:
                            if abs(flow_rrg - 0.0) > self.FLOW_TOLERANCE:
                                is_valid = False
                        else:
                            if abs(flow_rrg - flow_recipe) > flow_recipe * self.percent_flow:
                                is_valid = False
                    else:
                        if abs(flow_rrg - 0.0) > self.FLOW_TOLERANCE:
                            is_valid = False
                    time.sleep(0.05)  # Небольшая задержка между операциями

                if is_valid:
                    self.parent.update_status(self.translator.tr('success_setting_flow'))
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('waiting_stabilizy_flow')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(f"{self.translator.tr('gas_flow')} {self.translator.tr('not_match')}", need_reboot=True)
                    return

                QTimer.singleShot(5000, self.process_venting)

            elif self.current_step == 3:
                self.parent.update_status(self.translator.tr('waiting_stabilizy_pressure'))

                current_pressure = self.controller.handle_command('get_sensor_pressure')
                if current_pressure is not None:
                    self.pressure_history.append((time.time(), current_pressure))
                self.pressure_history = [(t, p) for t, p in self.pressure_history if time.time() - t <= 5 and p is not None]

                pressures = [p for t, p in self.pressure_history if p is not None]

                if len(pressures) >= 2 and max(pressures) - min(pressures) <= self.pressure_stable_threshold:
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_stabilizy_pressure'))
                    return

                QTimer.singleShot(1000, self.process_venting)

            elif self.current_step == 4:
                self.current_state = 'processing'
                self.attempt = 0
                self.current_step = 0
                QTimer.singleShot(1000, self.process_processing)
                
        except Exception as e:
            self.handle_error(f"{self.translator.tr('error_venting')}: {e}.", need_reboot=True)
            return

    def process_processing(self):
        if not self.ensure_state('processing'):
            return 

        if self.check_stop_button():
            return

        if self.recipe_params is None:
            self.handle_error(self.translator.tr('error_invalide_recipe'), need_reboot=True)
            return

        step_start_time = time.time()
        try:
            process_logger.debug(f"[process_processing] ENTRY: state={self.current_state}, step={self.current_step}, attempt={self.attempt}")
            logging.info(f"Выполнение process_processing: Current_state: {self.current_state}, current_step: {self.current_step}, attempt: {self.attempt}")

            time_str = self.recipe_params.get('time', '00:00')
            if not isinstance(time_str, str) or ':' not in time_str:
                self.handle_error(self.translator.tr('error_invalide_recipe'), need_reboot=True)
                return
            
            recipe_minutes, recipe_seconds = time_str.split(':')
            try:
                recipe_time = int(recipe_minutes) * 60 + int(recipe_seconds)
                process_logger.debug(f"[process_processing] recipe_time={recipe_time}s ({recipe_minutes}:{recipe_seconds})")
            except (ValueError, TypeError) as e:
                self.handle_error(f"{self.translator.tr('error_invalide_recipe')}: {e}", need_reboot=True)
                return
            
            if self.current_step == 0:
                # Используем ту же логику, что и в main_window.py
                # ВАЖНО: Останавливаем поток чтения RF ПЕРЕД обращением к генератору
                # Это предотвращает ошибки I/O при попытке включить плазму
                if hasattr(self.parent, 'stop_rf_reading'):
                    logging.info("[process_processing] STEP 0: Stopping RF reading thread before on_plasma...")
                    self.parent.stop_rf_reading(wait=True)  # Ждем завершения, чтобы порт точно освободился
                    time.sleep(0.5)  # Задержка для гарантии освобождения порта
                    
                    # Проверяем, что блокировка порта освобождена
                    if self.controller.rf is not None and hasattr(self.controller.rf, '_lock'):
                        if self.controller.rf._lock.acquire(blocking=False):
                            self.controller.rf._lock.release()
                            logging.info("[process_processing] STEP 0: RF port lock is available")
                        else:
                            logging.warning("[process_processing] STEP 0: RF port lock is busy, waiting...")
                            if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                                self.controller.rf._lock.release()
                                logging.info("[process_processing] STEP 0: RF port lock released after wait")
                
                success = False
                for attempt in range(self.max_attempts):
                    try:
                        result = self.controller.handle_command('on_plasma')
                        if not result:
                            logging.warning(f"process_processing: on_plasma command returned False on attempt {attempt + 1}")
                            if attempt < self.max_attempts - 1:
                                time.sleep(0.3)
                                continue
                        
                        # Задержка перед проверкой - генератор может включаться с задержкой
                        time.sleep(0.5)
                        
                        # Проверяем статус напрямую из генератора
                        try:
                            rf_status = self.controller.rf.read_status()
                            if rf_status:
                                rf_on = rf_status.get('rf_on', False)
                                logging.info(f"process_processing: RF status check (attempt {attempt + 1}): rf_on={rf_on}")
                                if rf_on:
                                    # Обновляем кэш
                                    self.controller._cached_plasma_status = True
                                    success = True
                                    logging.info(f"process_processing: Plasma confirmed ON on attempt {attempt + 1}")
                                    break
                                else:
                                    logging.warning(f"process_processing: Plasma status is False on attempt {attempt + 1}, retrying...")
                            else:
                                logging.warning(f"process_processing: rf.read_status() returned None on attempt {attempt + 1}")
                                # Пытаемся переподключиться к RF генератору асинхронно
                                def reconnect_rf_async():
                                    try:
                                        logging.info(f"[process_processing] STEP 0: Attempting to reconnect RF generator (read_status returned None)...")
                                        reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                        if reconnect_success:
                                            logging.info(f"[process_processing] STEP 0: RF generator reconnected successfully (was None)")
                                        else:
                                            logging.warning(f"[process_processing] STEP 0: Failed to reconnect RF generator (was None): {reconnect_msg}")
                                    except Exception as reconnect_error:
                                        logging.error(f"[process_processing] STEP 0: Error reconnecting RF generator (was None): {reconnect_error}")
                                
                                # Запускаем переподключение асинхронно
                                if self._rf_operations_executor is None:
                                    self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                self._rf_operations_executor.submit(reconnect_rf_async)
                                # Нельзя считать плазму включённой без ответа от генератора — продолжаем попытки
                        except Exception as e:
                            logging.error(f"process_processing: Error reading RF status (attempt {attempt + 1}): {e}")
                            # Пытаемся переподключиться к RF генератору асинхронно при ошибке
                            def reconnect_rf_async():
                                try:
                                    logging.info(f"[process_processing] STEP 0: Attempting to reconnect RF generator (read_status error)...")
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        logging.info(f"[process_processing] STEP 0: RF generator reconnected successfully (after error)")
                                    else:
                                        logging.warning(f"[process_processing] STEP 0: Failed to reconnect RF generator (after error): {reconnect_msg}")
                                except Exception as reconnect_error:
                                    logging.error(f"[process_processing] STEP 0: Error reconnecting RF generator (after error): {reconnect_error}")
                            
                            # Запускаем переподключение асинхронно
                            if self._rf_operations_executor is None:
                                self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                            self._rf_operations_executor.submit(reconnect_rf_async)
                            # Нельзя считать плазму включённой без ответа — продолжаем попытки
                    except Exception as e:
                        logging.error(f"process_processing: Error turning on plasma (attempt {attempt + 1}): {e}", exc_info=True)
                    
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.4)  # Задержка между попытками (как в ручном режиме)
                
                if success:
                    process_logger.info(f"[process_processing] STEP 0 SUCCESS: Plasma turned ON")
                    self.parent.update_status(self.translator.tr('plasma_on_start_process'))
                    self.processing_start_time = time.time()
                    process_logger.info(f"[process_processing] processing_start_time={self.processing_start_time}")
                    self.attempt = 0
                    self.current_step += 1
                    # Устанавливаем состояние processing
                    self.current_state = 'processing'
                    # Кэшируем recipe_time для быстрого доступа в таймере
                    try:
                        time_str = self.recipe_params.get('time', '00:00')
                        if ':' in time_str:
                            recipe_minutes, recipe_seconds = time_str.split(':')
                            self._cached_recipe_time = int(recipe_minutes) * 60 + int(recipe_seconds)
                        else:
                            self._cached_recipe_time = 0
                    except Exception:
                        self._cached_recipe_time = 0
                    process_logger.info(f"[process_processing] _cached_recipe_time={self._cached_recipe_time}s")
                    # Запускаем чтение данных генератора RF для отображения мощности
                    rf_start_time = time.time()
                    if hasattr(self.parent, 'start_rf_reading'):
                        process_logger.info("[process_processing] Starting RF reading thread...")
                        self.parent.start_rf_reading()
                        process_logger.info(f"[process_processing] RF reading thread started in {time.time() - rf_start_time:.3f}s")
                    else:
                        process_logger.warning("[process_processing] start_rf_reading method not found in parent")
                    # Запускаем таймеры для обновления времени обработки СРАЗУ после включения плазмы
                    timer_start_time = time.time()
                    self.processing_time_timer.start(1000)  # Обновление UI каждую секунду
                    self.processing_time_check_timer.start(100)  # Проверка окончания времени каждые 100ms для точности
                    process_logger.info(f"[process_processing] Timers started: UI_timer={self.processing_time_timer.isActive()}, check_timer={self.processing_time_check_timer.isActive()}, took {time.time() - timer_start_time:.3f}s")
                    logging.info(f"Processing time timers started immediately after plasma ON, state={self.current_state}, step={self.current_step}, start_time={self.processing_start_time}")
                    QTimer.singleShot(1000, self.process_processing)
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_on_plasma')}")
                    self.attempt += 1

                    if self.attempt > self.max_attempts:
                        self.attempt = 0
                        self.handle_error(self.translator.tr('error_turn_on_plasma'), need_reboot=True)
                        return
                    
                    QTimer.singleShot(1000, self.process_processing)

            elif self.current_step == 1:
                step1_entry_time = time.time()
                process_logger.debug(f"[process_processing] STEP 1 ENTRY: elapsed={step1_entry_time - step_start_time:.3f}s since step_start")
                
                if self.processing_start_time == 0:
                    process_logger.warning("[process_processing] STEP 1: processing_start_time is 0, resetting to step 0")
                    self.current_step = 0
                    QTimer.singleShot(1000, self.process_processing)
                    return
                
                # Убеждаемся, что current_state установлен правильно
                if self.current_state != 'processing':
                    process_logger.warning(f"[process_processing] STEP 1: current_state='{self.current_state}', expected 'processing'. Setting it now.")
                    logging.warning(f"process_processing step 1: current_state is '{self.current_state}', expected 'processing'. Setting it now.")
                    self.current_state = 'processing'
                
                # Убеждаемся, что таймеры запущены (перезапускаем если остановлены)
                timer_check_start = time.time()
                if not self.processing_time_timer.isActive():
                    self.processing_time_timer.start(1000)
                    process_logger.warning(f"[process_processing] STEP 1: UI timer was NOT active, restarted")
                    logging.info(f"Processing time timer restarted in step 1, state={self.current_state}, step={self.current_step}")
                if not self.processing_time_check_timer.isActive():
                    self.processing_time_check_timer.start(100)
                    process_logger.warning(f"[process_processing] STEP 1: Check timer was NOT active, restarted")
                    logging.info(f"Processing time check timer restarted in step 1")
                timer_check_time = time.time() - timer_check_start
                if timer_check_time > 0.001:
                    process_logger.warning(f"[process_processing] STEP 1: Timer check took {timer_check_time:.3f}s")

                # Проверка времени теперь происходит в отдельном таймере _check_processing_time_expired
                # Здесь только проверки безопасности и продолжение процесса
                
                # Вычисляем elapsed_time для проверок безопасности (не для проверки окончания времени)
                elapsed_time = time.time() - self.processing_start_time
                elapsed_time_int = int(elapsed_time)
                process_logger.debug(f"[process_processing] STEP 1: elapsed_time={elapsed_time:.3f}s ({elapsed_time_int}s), recipe_time={recipe_time}s, remaining={recipe_time - elapsed_time:.3f}s")
            
                if recipe_time - elapsed_time_int >= 0:
                    # Обновление таймера происходит в отдельном методе _update_processing_time
                    # через QTimer, чтобы не блокировать основной цикл
                    # Здесь только проверки безопасности
                    
                    # ОТКЛЮЧЕНО: Проверка отраженной мощности во время процесса
                    # get_reflected_power может занимать 20+ секунд и блокировать serial порт,
                    # что приводит к блокировке event loop и задержкам в таймерах
                    # Отраженная мощность отображается через отдельный поток RF (ReadRFWorker),
                    # который не блокирует основной процесс
                    # Если нужна проверка безопасности, можно включить, но с очень большими интервалами
                    # (например, каждые 10 секунд) и с таймаутом
                    pass

                    # Проверка натекания отключена по запросу пользователя
                    # current_pressure = float(self.controller.handle_command('get_sensor_pressure'))
                    # target_pressure = float(self.recipe_params.get('ResPressure', 0.1))
                    # 
                    # if current_pressure > (1 + self.percent_pressure) * target_pressure:
                    #     self.handle_error(self.translator.tr('error_pressure_increase_during_process'))
                    #     return

                    # Проверка потоков - делаем реже, чтобы не блокировать цикл
                    # Проверяем потоки АСИНХРОННО каждые 5 секунд (может быть очень медленной, блокирует весь процесс!)
                    # Делаем это асинхронно, чтобы не блокировать цикл и таймеры
                    if elapsed_time_int % 5 == 0 and not self._flow_checking:
                        self._flow_checking = True
                        process_logger.debug(f"[process_processing] STEP 1: Scheduling async flow check at {elapsed_time_int}s...")
                        
                        # Запускаем проверку асинхронно, чтобы не блокировать основной цикл
                        def check_flows_async():
                            try:
                                flow_check_start = time.time()
                                is_flow_valid = True

                                for i in work_gases:
                                    flow_read_start = time.time()
                                    recipe_gas = self.recipe_params.get(f"VE{i}", {})
                                    flow_rrg = 0  # Значение по умолчанию
                                    try:
                                        # Используем таймаут для чтения RRG, чтобы не зависнуть если устройство не отвечает
                                        # Максимум 1.5 секунды на чтение одного RRG, чтобы не блокировать процесс слишком долго
                                        # Создаем отдельный executor для чтения RRG, если его еще нет
                                        if self._rrg_read_executor is None:
                                            self._rrg_read_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="RRGRead")
                                        
                                        # Обрабатываем None: если gas = None, используем 0 (Air)
                                        type_gas = recipe_gas.get('gas') if recipe_gas.get('gas') is not None else 0
                                        future = self._rrg_read_executor.submit(
                                            self.controller.handle_command,
                                            'read_flow',
                                            num_rrg=i,
                                            type_gas=type_gas
                                        )
                                        try:
                                            flow_rrg = future.result(timeout=1.5)  # Таймаут 1.5 секунды
                                            flow_read_time = time.time() - flow_read_start
                                            process_logger.debug(f"[process_processing] STEP 1: read_flow RRG{i} (async) took {flow_read_time:.3f}s, result={flow_rrg}")
                                            if flow_rrg is None:
                                                flow_rrg = 0
                                                process_logger.warning(f"[process_processing] STEP 1: RRG{i} returned None, using 0")
                                                # Пытаемся переподключиться к RRG асинхронно, если вернулся None
                                                def reconnect_rrg_async(rrg_num):
                                                    try:
                                                        process_logger.info(f"[process_processing] STEP 1: Attempting to reconnect RRG{rrg_num} (returned None)...")
                                                        reconnect_success, reconnect_msg = self.controller.reconnect_device(f'rrg_{rrg_num}')
                                                        if reconnect_success:
                                                            process_logger.info(f"[process_processing] STEP 1: RRG{rrg_num} reconnected successfully (was None)")
                                                            logging.info(f"RRG {rrg_num} успешно переподключен (вернул None) во время process_processing")
                                                        else:
                                                            process_logger.warning(f"[process_processing] STEP 1: Failed to reconnect RRG{rrg_num} (was None): {reconnect_msg}")
                                                            logging.warning(f"Не удалось переподключить RRG {rrg_num} (вернул None) во время process_processing: {reconnect_msg}")
                                                    except Exception as reconnect_error:
                                                        process_logger.error(f"[process_processing] STEP 1: Error reconnecting RRG{rrg_num} (was None): {reconnect_error}")
                                                        logging.error(f"Ошибка при переподключении RRG {rrg_num} (вернул None) во время process_processing: {reconnect_error}")
                                                
                                                # Запускаем переподключение асинхронно в отдельном потоке
                                                if self._rrg_read_executor is not None:
                                                    self._rrg_read_executor.submit(reconnect_rrg_async, i)
                                            else:
                                                flow_rrg = float(flow_rrg)
                                        except FutureTimeoutError:
                                            flow_read_time = time.time() - flow_read_start
                                            flow_rrg = 0
                                            process_logger.warning(f"[process_processing] STEP 1: RRG{i} read timeout after {flow_read_time:.3f}s, using 0 to continue process")
                                            logging.warning(f"RRG {i} не ответил в течение 1.5s в process_processing, используем значение 0 для продолжения процесса")
                                            # Пытаемся переподключиться к RRG асинхронно, чтобы не блокировать процесс
                                            def reconnect_rrg_async(rrg_num):
                                                try:
                                                    process_logger.info(f"[process_processing] STEP 1: Attempting to reconnect RRG{rrg_num}...")
                                                    reconnect_success, reconnect_msg = self.controller.reconnect_device(f'rrg_{rrg_num}')
                                                    if reconnect_success:
                                                        process_logger.info(f"[process_processing] STEP 1: RRG{rrg_num} reconnected successfully")
                                                        logging.info(f"RRG {rrg_num} успешно переподключен во время process_processing")
                                                    else:
                                                        process_logger.warning(f"[process_processing] STEP 1: Failed to reconnect RRG{rrg_num}: {reconnect_msg}")
                                                        logging.warning(f"Не удалось переподключить RRG {rrg_num} во время process_processing: {reconnect_msg}")
                                                except Exception as e:
                                                    process_logger.error(f"[process_processing] STEP 1: Error reconnecting RRG{rrg_num}: {e}")
                                                    logging.error(f"Ошибка при переподключении RRG {rrg_num} во время process_processing: {e}")
                                            
                                            # Запускаем переподключение асинхронно в отдельном потоке
                                            if self._rrg_read_executor is not None:
                                                self._rrg_read_executor.submit(reconnect_rrg_async, i)
                                        except Exception as e:
                                            flow_rrg = 0
                                            process_logger.error(f"[process_processing] STEP 1: Exception reading flow RRG{i}: {e}")
                                            logging.error(f"Исключение при чтении потока РРГ {i} в process_processing: {e}")
                                            # Пытаемся переподключиться к RRG асинхронно при ошибке
                                            def reconnect_rrg_async(rrg_num):
                                                try:
                                                    process_logger.info(f"[process_processing] STEP 1: Attempting to reconnect RRG{rrg_num} after error...")
                                                    reconnect_success, reconnect_msg = self.controller.reconnect_device(f'rrg_{rrg_num}')
                                                    if reconnect_success:
                                                        process_logger.info(f"[process_processing] STEP 1: RRG{rrg_num} reconnected successfully after error")
                                                        logging.info(f"RRG {rrg_num} успешно переподключен после ошибки во время process_processing")
                                                    else:
                                                        process_logger.warning(f"[process_processing] STEP 1: Failed to reconnect RRG{rrg_num} after error: {reconnect_msg}")
                                                        logging.warning(f"Не удалось переподключить RRG {rrg_num} после ошибки во время process_processing: {reconnect_msg}")
                                                except Exception as reconnect_error:
                                                    process_logger.error(f"[process_processing] STEP 1: Error reconnecting RRG{rrg_num} after error: {reconnect_error}")
                                                    logging.error(f"Ошибка при переподключении RRG {rrg_num} после ошибки во время process_processing: {reconnect_error}")
                                            
                                            # Запускаем переподключение асинхронно в отдельном потоке
                                            if self._rrg_read_executor is not None:
                                                self._rrg_read_executor.submit(reconnect_rrg_async, i)
                                    except (ValueError, TypeError, Exception) as e:
                                        flow_rrg = 0
                                        process_logger.error(f"[process_processing] STEP 1: Error reading flow RRG{i}: {e}")
                                        logging.error(f"Ошибка чтения потока РРГ {i} в process_processing: {e}")
                                        # Пытаемся переподключиться к RRG асинхронно при ошибке чтения
                                        def reconnect_rrg_async(rrg_num):
                                            try:
                                                process_logger.info(f"[process_processing] STEP 1: Attempting to reconnect RRG{rrg_num} after read error...")
                                                reconnect_success, reconnect_msg = self.controller.reconnect_device(f'rrg_{rrg_num}')
                                                if reconnect_success:
                                                    process_logger.info(f"[process_processing] STEP 1: RRG{rrg_num} reconnected successfully after read error")
                                                    logging.info(f"RRG {rrg_num} успешно переподключен после ошибки чтения во время process_processing")
                                                else:
                                                    process_logger.warning(f"[process_processing] STEP 1: Failed to reconnect RRG{rrg_num} after read error: {reconnect_msg}")
                                                    logging.warning(f"Не удалось переподключить RRG {rrg_num} после ошибки чтения во время process_processing: {reconnect_msg}")
                                            except Exception as reconnect_error:
                                                process_logger.error(f"[process_processing] STEP 1: Error reconnecting RRG{rrg_num} after read error: {reconnect_error}")
                                                logging.error(f"Ошибка при переподключении RRG {rrg_num} после ошибки чтения во время process_processing: {reconnect_error}")
                                        
                                        # Запускаем переподключение асинхронно в отдельном потоке
                                        if self._rrg_read_executor is not None:
                                            self._rrg_read_executor.submit(reconnect_rrg_async, i)
                                    flow_recipe = float(recipe_gas.get('flow', 0))

                                    if recipe_gas.get('switch', 0):
                                        if flow_recipe != 0:
                                            if flow_rrg < (1 - self.percent_flow) * flow_recipe or flow_rrg > (1 + self.percent_flow) * flow_recipe:
                                                is_flow_valid = False
                                    else:
                                        if abs(flow_rrg - 0.0) > self.FLOW_TOLERANCE:
                                            is_flow_valid = False

                                flow_check_time = time.time() - flow_check_start
                                process_logger.debug(f"[process_processing] STEP 1: Flow check (async) took {flow_check_time:.3f}s, is_valid={is_flow_valid}")
                                if flow_check_time > 1.0:
                                    process_logger.warning(f"[process_processing] STEP 1: SLOW flow check: {flow_check_time:.3f}s > 1s")
                                
                                if not is_flow_valid:
                                    process_logger.error(f"[process_processing] STEP 1: Flow mismatch detected!")
                                    # Вызываем handle_error через QTimer с небольшой задержкой, чтобы не блокировать event loop
                                    error_msg = self.translator.tr('error_dismatch_flow_during_process')
                                    QTimer.singleShot(10, lambda msg=error_msg: self.handle_error(msg))
                                    return
                            except Exception as e:
                                process_logger.error(f"[process_processing] STEP 1: Error in async flow check: {e}", exc_info=True)
                            finally:
                                self._flow_checking = False
                        
                        # Запускаем в отдельном потоке через ThreadPoolExecutor
                        if self._flow_check_executor is None:
                            self._flow_check_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="FlowCheck")
                        self._flow_check_executor.submit(check_flows_async)
                
                # Продолжаем процесс
                step1_total_time = time.time() - step1_entry_time
                process_logger.debug(f"[process_processing] STEP 1 EXIT: total_time={step1_total_time:.3f}s, scheduling next call in 500ms")
                # Используем более короткий интервал (500ms) для более точной проверки времени
                # Это позволяет быстрее реагировать на окончание времени, даже если есть блокирующие операции
                QTimer.singleShot(500, self.process_processing)

            elif self.current_step == 2:
                step2_start = time.time()
                process_logger.info(f"[process_processing] STEP 2 ENTRY: Turning off plasma")
                # Останавливаем таймеры обновления времени обработки
                timer_stop_start = time.time()
                if self.processing_time_timer.isActive():
                    self.processing_time_timer.stop()
                    process_logger.info("[process_processing] STEP 2: UI timer stopped")
                    logging.info("Processing time timer stopped in step 2")
                if self.processing_time_check_timer.isActive():
                    self.processing_time_check_timer.stop()
                    process_logger.info("[process_processing] STEP 2: Check timer stopped")
                    logging.info("Processing time check timer stopped in step 2")
                timer_stop_time = time.time() - timer_stop_start
                
                # ВАЖНО: Останавливаем поток чтения RF ПЕРЕД выключением плазмы, чтобы освободить порт
                # Это предотвращает ошибки I/O при попытке выключить плазму
                if hasattr(self.parent, 'stop_rf_reading'):
                    rf_stop_start = time.time()
                    process_logger.info("[process_processing] STEP 2: Stopping RF reading thread before off_plasma...")
                    self.parent.stop_rf_reading(wait=True)  # Ждем завершения, чтобы порт точно освободился
                    rf_stop_time = time.time() - rf_stop_start
                    process_logger.info(f"[process_processing] STEP 2: RF reading thread stopped in {rf_stop_time:.3f}s")
                    
                    # Увеличиваем задержку для гарантии полного освобождения порта и блокировки
                    time.sleep(1.0)  # Увеличено с 0.2 до 1.0 секунды
                    
                    # ВАЖНО: Проверяем, что блокировка порта освобождена перед попыткой выключить плазму
                    # Если поток еще держит блокировку, ждем ее освобождения
                    lock_acquired = False
                    if self.controller.rf is not None and hasattr(self.controller.rf, '_lock'):
                        lock_check_start = time.time()
                        process_logger.info("[process_processing] STEP 2: Checking if RF port lock is available...")
                        # Пытаемся получить блокировку с таймаутом, чтобы убедиться, что она свободна
                        if self.controller.rf._lock.acquire(blocking=False):
                            # Блокировка свободна - сразу освобождаем
                            self.controller.rf._lock.release()
                            lock_acquired = True
                            process_logger.info(f"[process_processing] STEP 2: RF port lock is available (checked in {time.time() - lock_check_start:.3f}s)")
                        else:
                            # Блокировка занята - ждем с таймаутом
                            process_logger.warning("[process_processing] STEP 2: RF port lock is busy, waiting for release...")
                            if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                                self.controller.rf._lock.release()
                                lock_acquired = True
                                process_logger.info(f"[process_processing] STEP 2: RF port lock released after wait (waited {time.time() - lock_check_start:.3f}s)")
                            else:
                                process_logger.error("[process_processing] STEP 2: RF port lock timeout - lock still busy after 2s")
                    
                    # Пытаемся очистить буфер порта, если это возможно
                    try:
                        if hasattr(self.controller.rf, 'instrument') and hasattr(self.controller.rf.instrument, 'serial'):
                            if hasattr(self.controller.rf.instrument.serial, 'reset_input_buffer'):
                                self.controller.rf.instrument.serial.reset_input_buffer()
                            if hasattr(self.controller.rf.instrument.serial, 'reset_output_buffer'):
                                self.controller.rf.instrument.serial.reset_output_buffer()
                            process_logger.info("[process_processing] STEP 2: Serial port buffers cleared")
                    except Exception as e:
                        process_logger.warning(f"[process_processing] STEP 2: Could not clear serial buffers: {e}")
                
                # Выключаем плазму с несколькими попытками, если первая не удалась
                result = False
                plasma_off_start = time.time()
                for plasma_attempt in range(3):  # До 3 попыток выключения
                    process_logger.info(f"[process_processing] STEP 2: Calling off_plasma (attempt {plasma_attempt + 1}/3)...")
                    result = self.controller.handle_command('off_plasma')
                    plasma_off_time = time.time() - plasma_off_start
                    process_logger.info(f"[process_processing] STEP 2: off_plasma attempt {plasma_attempt + 1} took {plasma_off_time:.3f}s, result={result}")
                    
                    if result:
                        process_logger.info(f"[process_processing] STEP 2: off_plasma succeeded on attempt {plasma_attempt + 1}")
                        break
                    else:
                        if plasma_attempt < 2:  # Не последняя попытка
                            process_logger.warning(f"[process_processing] STEP 2: off_plasma failed on attempt {plasma_attempt + 1}, waiting before retry...")
                            time.sleep(0.5)  # Задержка перед следующей попыткой
                            # Пытаемся очистить буферы порта перед следующей попыткой
                            try:
                                if hasattr(self.controller.rf, 'instrument') and hasattr(self.controller.rf.instrument, 'serial'):
                                    if hasattr(self.controller.rf.instrument.serial, 'reset_input_buffer'):
                                        self.controller.rf.instrument.serial.reset_input_buffer()
                                    if hasattr(self.controller.rf.instrument.serial, 'reset_output_buffer'):
                                        self.controller.rf.instrument.serial.reset_output_buffer()
                            except Exception:
                                pass
                
                # Задержка перед проверкой - генератор может выключаться с задержкой
                time.sleep(0.5)
                
                # Проверяем статус напрямую из генератора (как при включении)
                # Делаем несколько попыток чтения статуса с задержками, чтобы получить подтверждение
                plasma_off_confirmed = False
                status_check_attempts = 3
                for status_attempt in range(status_check_attempts):
                    try:
                        rf_status = self.controller.rf.read_status()
                        if rf_status:
                            rf_on = rf_status.get('rf_on', True)  # По умолчанию True, если не удалось прочитать
                            forward_power = rf_status.get('forward_w', None)
                            reflected_power = rf_status.get('reflect_w', None)
                            process_logger.info(f"[process_processing] STEP 2: RF status check (attempt {self.attempt + 1}, status_check {status_attempt + 1}/{status_check_attempts}): rf_on={rf_on}, forward_power={forward_power}, reflected_power={reflected_power}")
                            
                            # Плазма выключена только если получили подтверждение:
                            if not rf_on:
                                plasma_off_confirmed = True
                                process_logger.info(f"[process_processing] STEP 2: Plasma confirmed OFF by rf_on=False")
                                break
                            elif forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                # Если обе мощности = 0, плазма выключена, даже если rf_on еще True
                                plasma_off_confirmed = True
                                process_logger.info(f"[process_processing] STEP 2: Plasma confirmed OFF by power=0 (both forward and reflected = 0)")
                                break
                        else:
                            process_logger.warning(f"[process_processing] STEP 2: rf.read_status() returned None (status_check {status_attempt + 1}/{status_check_attempts})")
                            # Пытаемся переподключиться к RF генератору асинхронно
                            def reconnect_rf_async():
                                try:
                                    process_logger.info(f"[process_processing] STEP 2: Attempting to reconnect RF generator (read_status returned None)...")
                                    reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                    if reconnect_success:
                                        process_logger.info(f"[process_processing] STEP 2: RF generator reconnected successfully (was None)")
                                        logging.info(f"RF генератор успешно переподключен (вернул None) во время process_processing step 2")
                                    else:
                                        process_logger.warning(f"[process_processing] STEP 2: Failed to reconnect RF generator (was None): {reconnect_msg}")
                                        logging.warning(f"Не удалось переподключить RF генератор (вернул None) во время process_processing step 2: {reconnect_msg}")
                                except Exception as reconnect_error:
                                    process_logger.error(f"[process_processing] STEP 2: Error reconnecting RF generator (was None): {reconnect_error}")
                                    logging.error(f"Ошибка при переподключении RF генератора (вернул None) во время process_processing step 2: {reconnect_error}")
                            
                            # Запускаем переподключение асинхронно
                            if self._rf_operations_executor is None:
                                self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                            self._rf_operations_executor.submit(reconnect_rf_async)
                            
                            # Пробуем проверить через отдельные команды мощности
                            try:
                                forward_power = self.controller.handle_command('get_forward_power')
                                reflected_power = self.controller.handle_command('get_reflected_power')
                                process_logger.info(f"[process_processing] STEP 2: Power check (status_check {status_attempt + 1}): forward={forward_power}, reflected={reflected_power}")
                                if forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                    plasma_off_confirmed = True
                                    process_logger.info(f"[process_processing] STEP 2: Plasma confirmed OFF by power check (both = 0)")
                                    break
                            except Exception as e:
                                process_logger.error(f"[process_processing] STEP 2: Error checking power (status_check {status_attempt + 1}): {e}")
                            
                            # Если не последняя попытка, ждем перед следующей
                            if status_attempt < status_check_attempts - 1:
                                time.sleep(0.5)  # Задержка перед следующей попыткой чтения статуса
                    except Exception as e:
                        process_logger.error(f"[process_processing] STEP 2: Error reading RF status (status_check {status_attempt + 1}): {e}")
                        # Пытаемся переподключиться к RF генератору асинхронно при ошибке
                        def reconnect_rf_async():
                            try:
                                process_logger.info(f"[process_processing] STEP 2: Attempting to reconnect RF generator (read_status error)...")
                                reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                if reconnect_success:
                                    process_logger.info(f"[process_processing] STEP 2: RF generator reconnected successfully (after error)")
                                    logging.info(f"RF генератор успешно переподключен (после ошибки) во время process_processing step 2")
                                else:
                                    process_logger.warning(f"[process_processing] STEP 2: Failed to reconnect RF generator (after error): {reconnect_msg}")
                                    logging.warning(f"Не удалось переподключить RF генератор (после ошибки) во время process_processing step 2: {reconnect_msg}")
                            except Exception as reconnect_error:
                                process_logger.error(f"[process_processing] STEP 2: Error reconnecting RF generator (after error): {reconnect_error}")
                                logging.error(f"Ошибка при переподключении RF генератора (после ошибки) во время process_processing step 2: {reconnect_error}")
                        
                        # Запускаем переподключение асинхронно
                        if self._rf_operations_executor is None:
                            self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                        self._rf_operations_executor.submit(reconnect_rf_async)
                        
                        # Пробуем проверить через отдельные команды мощности
                        try:
                            forward_power = self.controller.handle_command('get_forward_power')
                            reflected_power = self.controller.handle_command('get_reflected_power')
                            process_logger.info(f"[process_processing] STEP 2: Power check after error (status_check {status_attempt + 1}): forward={forward_power}, reflected={reflected_power}")
                            if forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                plasma_off_confirmed = True
                                process_logger.info(f"[process_processing] STEP 2: Plasma confirmed OFF by power check (both = 0)")
                                break
                        except Exception as e2:
                            process_logger.error(f"[process_processing] STEP 2: Error checking power after error (status_check {status_attempt + 1}): {e2}")
                        
                        # Если не последняя попытка, ждем перед следующей
                        if status_attempt < status_check_attempts - 1:
                            time.sleep(0.5)  # Задержка перед следующей попыткой чтения статуса
                
                if plasma_off_confirmed:
                    # Обновляем кэш
                    self.controller._cached_plasma_status = False
                    self.parent.update_status(self.translator.tr('plasma_off'))
                    # Поток чтения RF уже остановлен выше, но проверяем на всякий случай
                    if hasattr(self.parent, 'stop_rf_reading'):
                        try:
                            self.parent.stop_rf_reading(wait=False)
                        except:
                            pass  # Игнорируем ошибки, если поток уже остановлен
                    # Обновляем значения мощности после выключения плазмы
                    # Пытаемся прочитать еще несколько раз, затем ставим 0
                    def update_power_after_off():
                        # Пытаемся прочитать мощность еще несколько раз
                        for attempt in range(3):
                            try:
                                forward_power = self.controller.handle_command('get_forward_power')
                                reflected_power = self.controller.handle_command('get_reflected_power')
                                if forward_power is not None and forward_power == 0 and reflected_power is not None and reflected_power == 0:
                                    # Мощность уже 0, обновляем UI
                                    if hasattr(self.parent, 'HFPowerZnach'):
                                        self.parent.HFPowerZnach.setText('0')
                                    if hasattr(self.parent, 'HFCurrentZnach'):
                                        self.parent.HFCurrentZnach.setText('0')
                                    return
                                elif forward_power is not None:
                                    if hasattr(self.parent, 'HFPowerZnach'):
                                        self.parent.HFPowerZnach.setText(str(forward_power))
                                if reflected_power is not None:
                                    if hasattr(self.parent, 'HFCurrentZnach'):
                                        self.parent.HFCurrentZnach.setText(str(reflected_power))
                            except Exception as e:
                                logging.error(f"Error reading power after plasma off (attempt {attempt + 1}): {e}")
                            time.sleep(0.2)
                        # После всех попыток ставим 0
                        if hasattr(self.parent, 'HFPowerZnach'):
                            self.parent.HFPowerZnach.setText('0')
                        if hasattr(self.parent, 'HFCurrentZnach'):
                            self.parent.HFCurrentZnach.setText('0')
                    
                    # Запускаем обновление мощности через небольшую задержку
                    QTimer.singleShot(500, update_power_after_off)
                    self.attempt = 0
                    self.current_step += 1
                    QTimer.singleShot(1000, self.process_processing)
                    return
                else:
                    process_logger.warning(f"[process_processing] STEP 2: Plasma status is still ON on attempt {self.attempt + 1}, retrying...")
                
                # Если дошли сюда, плазма не выключилась
                self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_off_plasma')}")
                self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_turn_off_plasma'), need_reboot=True)
                    return

                QTimer.singleShot(1000, self.process_processing)

            elif self.current_step == 3:
                for i in work_gases:
                    self.controller.handle_command(f"close_valve_ve{i}")

                is_valid = True
                for i in work_gases:
                    valves_check = self.safe_get_valves_states()
                    if valves_check.get(f"valve_ve{i}", 'close') == 'open':
                        is_valid = False

                if is_valid:
                    self.parent.update_status(self.translator.tr('valves_close'))
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_close_valves')}")
                    self.attempt += 1
                
                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_close_valves'), need_reboot=True)
                    return
                
                QTimer.singleShot(1000, self.process_processing)

            elif self.current_step == 4:
                self.controller.handle_command('off_pump')
                states_check = self.safe_get_states()
                if states_check.get('pump', 1) == 0:
                    self.parent.update_status(self.translator.tr('pump_off'))
                    self.attempt = 0
                    self.current_step += 1
                    QTimer.singleShot(1000, self.process_processing)
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_off_pump')}")
                    self.attempt += 1

                    if self.attempt > self.max_attempts:
                        self.attempt = 0
                        self.handle_error(self.translator.tr('error_turn_off_pump'), need_reboot=True)
                        return
                    
                    QTimer.singleShot(1000, self.process_processing)
                    return
                

            elif self.current_step == 5:
                # Переходим к venting_atm
                self.attempt = 0
                self.current_step = 0
                self.current_state = 'venting_atm'
                QTimer.singleShot(1000, self.process_venting_atm)
                return

                
        except Exception as e:
            total_time = time.time() - step_start_time
            process_logger.error(f"[process_processing] EXCEPTION after {total_time:.3f}s: {e}", exc_info=True)
            self.handle_error(f"{self.translator.tr('error_process')}: {e}.", need_reboot=True)
            return

    def process_venting_atm(self):
        if not self.ensure_state('venting_atm'):
            return 

        if self.check_stop_button():
            return
        
        if self.recipe_params is None:
            self.handle_error(self.translator.tr('error_invalide_recipe'), need_reboot=True)
            return

        try:
            logging.info(f"Выполнение process_venting_atm: Current_state: {self.current_state}, current_step: {self.current_step}, attempt: {self.attempt}")

            venting_atm_settings = settings.get('time_venting', 3)

            if self.current_step == 0:
                self.controller.handle_command('open_valve_ve01')

                valves_states = self.safe_get_valves_states()
                if valves_states.get('valve_ve01', 'open') == 'open':
                    self.venting_atm_start_time = time.time()
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_open_ve01')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_open_valve_ve01'), need_reboot=True)
                    return

                QTimer.singleShot(1000, self.process_venting_atm)
                
            elif self.current_step == 1:
                if self.venting_atm_start_time == 0:
                    self.current_step = 0
                    QTimer.singleShot(1000, self.process_venting_atm)
                    return
                
                elapsed_time = int(time.time() - self.venting_atm_start_time)
                
                if elapsed_time < venting_atm_settings:
                    remaining_time = venting_atm_settings - elapsed_time
                    self.parent.update_status(f"{self.translator.tr('venting_atm_end_for')} {remaining_time} {self.translator.tr('sec')}.")

                    QTimer.singleShot(1000, self.process_venting_atm)
                else:
                    self.parent.update_status(self.translator.tr('end_venting_atm'))
                    self.attempt = 0
                    self.current_step += 1
                    QTimer.singleShot(1000, self.process_venting_atm)
                    
            elif self.current_step == 2:
                self.controller.handle_command('close_valve_ve01')
                
                valves_check = self.safe_get_valves_states()
                if valves_check.get('valve_ve01', 'close') == 'close':
                    self.attempt = 0
                    self.current_step += 1
                else:
                    self.parent.update_status(f"{self.translator.tr('attempt')} {self.attempt} {self.translator.tr('attempt_close_ve01')}")
                    self.attempt += 1

                if self.attempt > self.max_attempts:
                    self.attempt = 0
                    self.handle_error(self.translator.tr('error_close_valve_ve01'), need_reboot=True)
                    return

                QTimer.singleShot(1000, self.process_venting_atm)
                
            elif self.current_step == 3:
                # Включаем звук при завершении процесса (только один раз), если включен в настройках
                if not self.buzzer_activated:
                    # Проверяем настройку "включить звук"
                    enable_sound = settings.get('enable_sound', True)  # По умолчанию True для обратной совместимости
                    if enable_sound:
                        self.controller.handle_command('on_buzz')
                        self.buzzer_activated = True
                        logging.info("Process completed: Buzzer turned on (sound enabled in settings)")
                        
                        # Выключаем звук через 2 секунды
                        def turn_off_buzz():
                            self.controller.handle_command('off_buzz')
                            logging.info("Process completed: Buzzer turned off")
                        
                        QTimer.singleShot(2000, turn_off_buzz)
                    else:
                        self.buzzer_activated = True  # Помечаем как активированный, чтобы не пытаться включить снова
                        logging.info("Process completed: Buzzer skipped (sound disabled in settings)")

                def oper():
                    self.parent.StatusLine.setText(self.translator.tr('system_ready_oper'))
                    if hasattr(self.parent, 'ButtonStart'):
                        self.parent.ButtonStart.setText(self.translator.tr('start'))
                        self.parent.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
                        # НЕ устанавливаем setEnabled здесь - это делается в check_permissions
                        # LED будет включен в update_values() если кнопка активна и текст = "start"
                        self.parent.NIButton.setEnabled(False)
                        self.parent.VEButton.setEnabled(False)
                        self.parent.HFButton.setEnabled(False)
                        self.parent.VE0Button.setEnabled(False)
                
                def technologist():
                    self.parent.StatusLine.setText(self.translator.tr('system_ready_tech'))
                    if hasattr(self.parent, 'ButtonStart'):
                        self.parent.ButtonStart.setText(self.translator.tr('start'))
                        self.parent.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
                        # НЕ устанавливаем setEnabled здесь - это делается в check_permissions
                        # LED будет включен в update_values() если кнопка активна и текст = "start"
                        self.parent.NIButton.setEnabled(False)
                        self.parent.VEButton.setEnabled(False)
                        self.parent.HFButton.setEnabled(False)
                        self.parent.VE0Button.setEnabled(False)

                self.parent.update_status(self.translator.tr('end_process'))
                
                msg = QMessageBox()
                    
                msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
                msg.setStyleSheet("""
                                QMessageBox {
                                    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                                        stop:0 rgb(200, 255, 200), stop:1 rgb(150, 255, 150));
                                    border: 3px solid rgb(0, 200, 0);
                                    border-radius: 15px;
                                    padding: 20px;
                                }
                                QLabel {
                                    font: 80 24pt "Lato Semibold"; 
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

                map_num_gas = {
                    '0': self.translator.tr('air'),
                    '1': self.translator.tr('argon'),
                    '2': self.translator.tr('oxigen'),
                    '3': self.translator.tr('nitrogen'),
                    '4': self.translator.tr('custom_gas'),
                }

                text = f"{self.translator.tr('success_process')}\n"
                text += f"{self.translator.tr('final_pressure')}: {self.recipe_params.get('ResPressure', 0.1)} {self.translator.tr('pressure_unit')}\n"

                gas1 = self.recipe_params.get('VE1', {})
                gas2 = self.recipe_params.get('VE2', {})

                if gas1.get('switch', 0):
                    text += f"{self.translator.tr('gas_1')}: {map_num_gas.get(str(gas1.get('gas', 0)))} - {gas1.get('flow', 0)} {self.translator.tr('flow_unit')}\n"
                if gas2.get('switch', 0):
                    text += f"{self.translator.tr('gas_2')}: {map_num_gas.get(str(gas2.get('gas', 0)))} - {gas2.get('flow', 0)} {self.translator.tr('flow_unit')}\n"
                
                text += f"{self.translator.tr('forward_power')}: {self.recipe_params.get('power', 0)} {self.translator.tr('power_unit')}\n"
                text += f"{self.translator.tr('process_time')}: {self.recipe_params.get('time', '00:00')}\n"
                
                msg.setIcon(QMessageBox.NoIcon)
                msg.setText(self.translator.tr('report'))
                msg.setInformativeText(text)
                # Убираем фокус с кнопки ОК для сенсорного экрана
                msg.setDefaultButton(None)
                # Находим кнопку ОК и убираем с неё фокус
                ok_button = msg.button(QMessageBox.Ok)
                if ok_button:
                    ok_button.setFocusPolicy(Qt.NoFocus)
                    ok_button.clearFocus()
                    # Устанавливаем фокус на само окно вместо кнопки
                    msg.setFocus()
                # Показываем окно и устанавливаем фокус на него
                msg.show()
                msg.activateWindow()
                msg.raise_()
                msg.exec()

                self.current_state = 'idle'
                self.attempt = 0
                self.current_step = 0

                if self.parent.user_mode == 'Operator':
                    QTimer.singleShot(1000, oper)
                else:
                    QTimer.singleShot(1000, technologist)

                # LED будет синхронизирован в main_window.check_permissions() или update_values()
                # на основе состояния кнопки (enabled и текст)
                
                # Выключаем звук через 2 секунды после завершения процесса
                def turn_off_buzz():
                    self.controller.handle_command('off_buzz')
                    logging.info("Process completed: Buzzer turned off")
                
                QTimer.singleShot(2000, turn_off_buzz)
                    
        except Exception as e:
            self.handle_error(f"{self.translator.tr('error_venting_atm')}: {e}", need_reboot=True)
            return

    def handle_error(self, error_message, need_reboot=False):
        start_process_logger.info(f"[HANDLE_ERROR] handle_error: ВХОД, error_message='{error_message}', need_reboot={need_reboot}, "
                    f"current_state={self.current_state}, current_step={self.current_step}")
        
        # Сохраняем информацию о том, что процесс только что запустился
        # Это нужно, чтобы не менять текст кнопки обратно на "start", если процесс только что запустился
        # Считаем процесс "только что запущенным" если:
        # - Состояние init_recipe или init (инициализация)
        # - Шаг <= 2 (проверка рецепта, выключение устройств, открытие клапанов - ранние стадии инициализации)
        was_just_started = (self.current_state in ['init_recipe', 'init'] and self.current_step <= 2)
        start_process_logger.info(f"[HANDLE_ERROR] handle_error: was_just_started={was_just_started} "
                    f"(state in ['init_recipe', 'init']: {self.current_state in ['init_recipe', 'init']}, "
                    f"step <= 2: {self.current_step <= 2})")
        
        # Останавливаем таймеры проверки СРАЗУ, чтобы они не вызывали stop_process() во время обработки ошибки
        start_process_logger.info(f"[HANDLE_ERROR] handle_error: Останавливаем таймеры проверки")
        self.check_fault_timer.stop()
        self.check_stop_timer.stop()
        
        # Быстро устанавливаем состояние, чтобы не блокировать event loop
        self.attempt = 0
        self.current_step = 0
        
        # Обновляем статус асинхронно, чтобы не блокировать event loop
        error_status = f"{self.translator.tr('error')}: {error_message}"
        QTimer.singleShot(0, lambda: self.parent.update_status(error_status))

        start_process_logger.error(f"[HANDLE_ERROR] handle_error: {error_message}, was_just_started={was_just_started}")
        
        # Если процесс только что запустился и сразу произошла ошибка, 
        # не запускаем process_fault() - просто возвращаем процесс в idle
        if was_just_started:
            start_process_logger.warning(f"[HANDLE_ERROR] handle_error: Process just started, returning to idle without full fault procedure. "
                          f"current_state будет изменен с '{self.current_state}' на 'idle'")
            self.current_state = "idle"
            start_process_logger.info(f"[HANDLE_ERROR] handle_error: current_state установлен в 'idle', выходим из handle_error")
            # Текст кнопки останется "stop", чтобы пользователь мог остановить процесс
            # или он будет изменен в main_window.py, если start_recipe() вернул False
            return
        
        # Для остальных случаев устанавливаем состояние fault
        start_process_logger.info(f"[HANDLE_ERROR] handle_error: Process was not just started, setting state to 'fault'")
        self.current_state = "fault"
        
        # НЕ меняем текст кнопки здесь - он будет изменен в process_fault() только после полной остановки процесса
        
        # Выключаем плазму асинхронно (как в main_window.py), если она включена
        def stop_plasma_task():
            """Задача выключения плазмы в отдельном потоке при ошибке"""
            logging.info("HANDLE_ERROR: Starting plasma stop procedure")
            start_time = time.time()
            
            try:
                # ВАЖНО: Останавливаем поток чтения RF ПЕРЕД обращением к генератору
                # Это предотвращает ошибки I/O при попытке выключить плазму
                if hasattr(self.parent, 'stop_rf_reading'):
                    logging.info("HANDLE_ERROR STOP PLASMA: Stopping RF reading thread before off_plasma...")
                    self.parent.stop_rf_reading(wait=True)  # Ждем завершения, чтобы порт точно освободился
                    time.sleep(1.0)  # Задержка для гарантии полного освобождения порта
                    
                    # Проверяем, что блокировка порта освобождена
                    if self.controller.rf is not None and hasattr(self.controller.rf, '_lock'):
                        if self.controller.rf._lock.acquire(blocking=False):
                            self.controller.rf._lock.release()
                            logging.info("HANDLE_ERROR STOP PLASMA: RF port lock is available")
                        else:
                            logging.warning("HANDLE_ERROR STOP PLASMA: RF port lock is busy, waiting...")
                            if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                                self.controller.rf._lock.release()
                                logging.info("HANDLE_ERROR STOP PLASMA: RF port lock released after wait")
                
                success = False
                max_attempts = 10  # Используем то же количество попыток, что и в main_window
                for attempt in range(max_attempts):
                    try:
                        logging.info(f"HANDLE_ERROR STOP PLASMA: Attempt {attempt + 1}/{max_attempts}")
                        result = self.controller.handle_command('off_plasma')
                        if result:
                            time.sleep(0.3)  # Задержка перед проверкой
                            # Проверяем статус напрямую из генератора
                            try:
                                rf_status = self.controller.rf.read_status()
                                if rf_status:
                                    rf_on = rf_status.get('rf_on', True)
                                    logging.info(f"HANDLE_ERROR STOP PLASMA: RF status check (attempt {attempt + 1}): rf_on={rf_on}")
                                    if not rf_on:
                                        # Обновляем кэш
                                        self.controller._cached_plasma_status = False
                                        success = True
                                        logging.info(f"HANDLE_ERROR STOP PLASMA: Plasma confirmed OFF on attempt {attempt + 1}")
                                        break
                                    else:
                                        logging.warning(f"HANDLE_ERROR STOP PLASMA: Plasma still ON on attempt {attempt + 1}, retrying...")
                                else:
                                    logging.warning(f"HANDLE_ERROR STOP PLASMA: rf.read_status() returned None on attempt {attempt + 1}")
                                    # Пытаемся переподключиться к RF генератору асинхронно
                                    def reconnect_rf_async():
                                        try:
                                            logging.info(f"[handle_error] Attempting to reconnect RF generator (read_status returned None)...")
                                            reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                            if reconnect_success:
                                                logging.info(f"[handle_error] RF generator reconnected successfully (was None)")
                                            else:
                                                logging.warning(f"[handle_error] Failed to reconnect RF generator (was None): {reconnect_msg}")
                                        except Exception as reconnect_error:
                                            logging.error(f"[handle_error] Error reconnecting RF generator (was None): {reconnect_error}")
                                    
                                    if self._rf_operations_executor is None:
                                        self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                    self._rf_operations_executor.submit(reconnect_rf_async)
                                    # Нельзя считать плазму выключенной без ответа от генератора — продолжаем попытки
                            except Exception as e:
                                logging.error(f"HANDLE_ERROR STOP PLASMA: Error reading RF status (attempt {attempt + 1}): {e}")
                                # Пытаемся переподключиться к RF генератору асинхронно при ошибке
                                def reconnect_rf_async():
                                    try:
                                        logging.info(f"[handle_error] Attempting to reconnect RF generator (read_status error)...")
                                        reconnect_success, reconnect_msg = self.controller.reconnect_device('RF')
                                        if reconnect_success:
                                            logging.info(f"[handle_error] RF generator reconnected successfully (after error)")
                                        else:
                                            logging.warning(f"[handle_error] Failed to reconnect RF generator (after error): {reconnect_msg}")
                                    except Exception as reconnect_error:
                                        logging.error(f"[handle_error] Error reconnecting RF generator (after error): {reconnect_error}")
                                
                                if self._rf_operations_executor is None:
                                    self._rf_operations_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RFOps")
                                self._rf_operations_executor.submit(reconnect_rf_async)
                                # Нельзя считать плазму выключенной без ответа — продолжаем попытки
                        else:
                            logging.warning(f"HANDLE_ERROR STOP PLASMA: off_plasma command returned False on attempt {attempt + 1}")
                    except Exception as e:
                        logging.error(f"HANDLE_ERROR STOP PLASMA: Error turning off plasma (attempt {attempt + 1}): {e}", exc_info=True)
                    
                    if attempt < max_attempts - 1:
                        time.sleep(0.4)  # Задержка между попытками
                
                total_elapsed = time.time() - start_time
                if success:
                    logging.info(f"HANDLE_ERROR STOP PLASMA: Successfully stopped plasma in {total_elapsed:.3f}s")
                else:
                    logging.warning(f"HANDLE_ERROR STOP PLASMA: Failed to stop plasma after {total_elapsed:.3f}s")
                    
            except Exception as e:
                elapsed = time.time() - start_time
                logging.error(f"HANDLE_ERROR STOP PLASMA: EXCEPTION after {elapsed:.3f}s: {e}", exc_info=True)
        
        # Запускаем выключение плазмы в отдельном потоке (не блокирует UI)
        if self._stop_plasma_executor is None:
            self._stop_plasma_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop_plasma_error")
        self._stop_plasma_executor.submit(stop_plasma_task)

        msg = QMessageBox()
        
        msg.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        msg.setStyleSheet("""
                          QMessageBox {
                              background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                                  stop:0 rgb(255, 220, 200), stop:1 rgb(255, 180, 150));
                              border: 3px solid rgb(255, 100, 100);
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

        if need_reboot:
            msg.setText(self.translator.tr('fault') + " " + self.translator.tr('need_reboot'))
        else:
            msg.setText(self.translator.tr('fault'))

        msg.setInformativeText(error_message)
        # Убираем фокус с кнопки ОК для сенсорного экрана
        msg.setDefaultButton(None)
        # Находим кнопку ОК и убираем с неё фокус
        ok_button = msg.button(QMessageBox.Ok)
        if ok_button:
            ok_button.setFocusPolicy(Qt.NoFocus)
            ok_button.clearFocus()
            # Устанавливаем фокус на само окно вместо кнопки
            msg.setFocus()
        # Показываем окно и устанавливаем фокус на него
        msg.show()
        msg.activateWindow()
        msg.raise_()
        msg.exec()
        
    def _update_processing_time(self):
        """Обновление времени обработки (вызывается из таймера, только для обновления UI)"""
        update_start = time.time()
        process_logger.debug(f"[_update_processing_time] CALLED at {update_start:.3f}")
        
        # Простая проверка: если processing_start_time > 0, значит процесс идет
        if self.processing_start_time == 0:
            # Если процесс не начался, останавливаем таймер
            process_logger.warning("[_update_processing_time] processing_start_time is 0, stopping timer")
            self.processing_time_timer.stop()
            return
        
        # Если процесс завершен (не в состоянии processing), останавливаем таймер и устанавливаем финальное время
        if self.current_state != 'processing':
            process_logger.info(f"[_update_processing_time] Process finished (state={self.current_state}), stopping timer")
            self.processing_time_timer.stop()
            try:
                recipe_time = getattr(self, '_cached_recipe_time', 0)
                if recipe_time == 0:
                    time_str = self.recipe_params.get('time', '00:00')
                    if ':' in time_str:
                        recipe_minutes, recipe_seconds = time_str.split(':')
                        recipe_time = int(recipe_minutes) * 60 + int(recipe_seconds)
                if recipe_time > 0:
                    final_minutes, final_seconds = recipe_time // 60, recipe_time % 60
                    if hasattr(self.parent, 'TimeZnach') and hasattr(self.parent, 'TimeProgress'):
                        self.parent.TimeZnach.setText(f"{final_minutes:02d}:{final_seconds:02d}")
                        self.parent.TimeProgress.setMaximum(recipe_time)
                        self.parent.TimeProgress.setValue(recipe_time)
            except Exception as e:
                process_logger.error(f"[_update_processing_time] Error setting final time: {e}")
                logging.error(f"Error setting final processing time: {e}")
            return
        
        # Обновляем UI во время процесса (только UI, без блокирующих операций)
        try:
            recipe_time = getattr(self, '_cached_recipe_time', 0)
            if recipe_time == 0:
                process_logger.warning("[_update_processing_time] recipe_time is 0, skipping update")
                return
            
            elapsed_time = int(time.time() - self.processing_start_time)
            
            # Обновляем UI
            ui_update_start = time.time()
            elapsed_minutes, elapsed_seconds = elapsed_time // 60, elapsed_time % 60
            time_str = f"{elapsed_minutes:02d}:{elapsed_seconds:02d}"
            if hasattr(self.parent, 'TimeZnach') and hasattr(self.parent, 'TimeProgress'):
                self.parent.TimeZnach.setText(time_str)
                self.parent.TimeProgress.setMaximum(recipe_time)
                # Ограничиваем значение прогресса максимальным временем
                progress_value = min(elapsed_time, recipe_time)
                self.parent.TimeProgress.setValue(progress_value)
            ui_update_time = time.time() - ui_update_start
            total_time = time.time() - update_start
            
            process_logger.debug(f"[_update_processing_time] Updated UI: elapsed={elapsed_time}s, time_str={time_str}, progress={progress_value}/{recipe_time}, UI_update={ui_update_time:.3f}s, total={total_time:.3f}s")
            
            if total_time > 0.1:
                process_logger.warning(f"[_update_processing_time] SLOW: total time {total_time:.3f}s > 100ms")
        except Exception as e:
            process_logger.error(f"[_update_processing_time] Error: {e}", exc_info=True)
            logging.error(f"Error updating processing time: {e}", exc_info=True)
    
    def _check_processing_time_expired(self):
        """Проверка окончания времени обработки (вызывается из отдельного таймера, не блокируется)"""
        check_start = time.time()
        
        # Отслеживаем задержки между вызовами
        if not hasattr(self, '_last_check_time'):
            self._last_check_time = check_start
            self._last_check_log_time = 0
        
        time_since_last = check_start - self._last_check_time
        self._last_check_time = check_start
        
        # Логируем только каждую секунду, но всегда логируем задержки > 200ms
        should_log = (check_start - self._last_check_log_time) >= 1.0
        if should_log:
            process_logger.debug(f"[_check_processing_time_expired] CALLED at {check_start:.3f}, time_since_last={time_since_last:.3f}s")
            self._last_check_log_time = check_start
        elif time_since_last > 0.2:
            process_logger.warning(f"[_check_processing_time_expired] DELAY: {time_since_last:.3f}s since last call (expected ~0.1s)")
        
        # Быстрая проверка: только если процесс идет
        if self.current_state != 'processing' or self.current_step != 1:
            if should_log:
                process_logger.debug(f"[_check_processing_time_expired] Process not active (state={self.current_state}, step={self.current_step}), stopping timer")
            self.processing_time_check_timer.stop()
            return
        
        if self.processing_start_time == 0:
            if should_log:
                process_logger.warning("[_check_processing_time_expired] processing_start_time is 0")
            return
        
        try:
            # Используем кэшированное значение recipe_time
            recipe_time = getattr(self, '_cached_recipe_time', 0)
            if recipe_time == 0:
                # Если кэш не установлен, вычисляем один раз
                time_str = self.recipe_params.get('time', '00:00')
                if ':' in time_str:
                    recipe_minutes, recipe_seconds = time_str.split(':')
                    recipe_time = int(recipe_minutes) * 60 + int(recipe_seconds)
                    self._cached_recipe_time = recipe_time
                else:
                    return
            
            elapsed_time = time.time() - self.processing_start_time
            remaining_time = recipe_time - elapsed_time
            
            if should_log:
                process_logger.debug(f"[_check_processing_time_expired] elapsed={elapsed_time:.3f}s, recipe={recipe_time}s, remaining={remaining_time:.3f}s")
            
            # КРИТИЧНО: Проверяем окончание времени обработки точно по времени
            # Выключаем плазму точно через заданное время, независимо от таймера
            if elapsed_time >= recipe_time:
                # Время истекло - немедленно выключаем плазму
                actual_elapsed = elapsed_time - recipe_time
                process_logger.warning(f"[_check_processing_time_expired] TIME EXPIRED: elapsed={elapsed_time:.3f}s >= recipe={recipe_time}s, delay={actual_elapsed:.3f}s")
                logging.info(f"Processing time expired: {int(elapsed_time)} >= {recipe_time}, turning off plasma")
                self.processing_time_timer.stop()
                self.processing_time_check_timer.stop()
                # Устанавливаем финальное время (быстро, без блокировок)
                try:
                    final_minutes, final_seconds = recipe_time // 60, recipe_time % 60
                    if hasattr(self.parent, 'TimeZnach') and hasattr(self.parent, 'TimeProgress'):
                        self.parent.TimeZnach.setText(f"{final_minutes:02d}:{final_seconds:02d}")
                        self.parent.TimeProgress.setMaximum(recipe_time)
                        self.parent.TimeProgress.setValue(recipe_time)
                except Exception as e:
                    process_logger.error(f"[_check_processing_time_expired] Error setting final time: {e}")
                    pass  # Не блокируем из-за ошибок UI
                # Переходим к выключению плазмы
                self.parent.update_status(self.translator.tr('plasma_end'))
                self.attempt = 0
                self.current_step += 1
                QTimer.singleShot(100, self.process_processing)  # Быстрый переход
            else:
                check_time = time.time() - check_start
                if check_time > 0.01:
                    process_logger.warning(f"[_check_processing_time_expired] SLOW: check took {check_time:.3f}s > 10ms")
        except Exception as e:
            process_logger.error(f"[_check_processing_time_expired] Error: {e}", exc_info=True)
            # Минимальное логирование, чтобы не блокировать
            pass
    
    def stop_process(self):
        start_process_logger.info(f"[STOP_PROCESS] stop_process: ВХОД, current_state={self.current_state}, current_step={self.current_step}")
        self.check_stop_timer.stop()
        self.processing_time_timer.stop()
        self.processing_time_check_timer.stop()
        self.check_fault_timer.stop()
        start_process_logger.info(f"[STOP_PROCESS] stop_process: Все таймеры остановлены")
        
        # ВАЖНО: Останавливаем поток чтения RF ПЕРЕД обращением к генератору
        # Это предотвращает ошибки I/O при попытке выключить плазму
        if hasattr(self.parent, 'stop_rf_reading'):
            logging.info("[stop_process] Stopping RF reading thread before off_plasma...")
            self.parent.stop_rf_reading(wait=True)  # Ждем завершения, чтобы порт точно освободился
            time.sleep(1.0)  # Задержка для гарантии полного освобождения порта
            
            # Проверяем, что блокировка порта освобождена
            if self.controller.rf is not None and hasattr(self.controller.rf, '_lock'):
                if self.controller.rf._lock.acquire(blocking=False):
                    self.controller.rf._lock.release()
                    logging.info("[stop_process] RF port lock is available")
                else:
                    logging.warning("[stop_process] RF port lock is busy, waiting...")
                    if self.controller.rf._lock.acquire(blocking=True, timeout=2.0):
                        self.controller.rf._lock.release()
                        logging.info("[stop_process] RF port lock released after wait")
        
        # Останавливаем executor для проверки отраженной мощности
        if self._reflected_power_executor is not None:
            self._reflected_power_executor.shutdown(wait=False)
            self._reflected_power_executor = None
        self._reflected_power_checking = False
        
        # Останавливаем executor для проверки потоков
        if self._flow_check_executor is not None:
            self._flow_check_executor.shutdown(wait=False)
            self._flow_check_executor = None
        self._flow_checking = False
        
        # Останавливаем executor для чтения RRG
        if self._rrg_read_executor is not None:
            self._rrg_read_executor.shutdown(wait=False)
            self._rrg_read_executor = None
        
        # Останавливаем executor для операций с RF генератором
        if self._rf_operations_executor is not None:
            self._rf_operations_executor.shutdown(wait=False)
            self._rf_operations_executor = None

        self.parent.ButtonStart.setEnabled(False)
        # LED будет синхронизирован в main_window.check_permissions() или update_values()
        # на основе состояния кнопки (enabled и текст)
        self.parent.RecName.deselect()
        
        self.current_state = 'idle'
        self.pressure_history = []
        self.attempt = 0
        self.current_step = 0
        self.buzzer_activated = False  # Сбрасываем флаг при остановке процесса
        self.pumping_start_time = 0
        self.processing_start_time = 0
        self.venting_atm_start_time = 0
        # Сначала выключаем критичные устройства (плазма, нагрев)
        states = self.controller.handle_command('get_states')
        if not isinstance(states, dict):
            states = {}
        if states.get('plasma', 1):
            self.controller.handle_command('off_plasma')
            self.parent.StatusLine.setText(self.translator.tr('plasma_off'))
            time.sleep(0.3)
        
        # Затем выключаем остальные устройства
        for attempt in range(self.max_attempts):
            states = self.controller.handle_command('get_states')
            if not isinstance(states, dict):
                states = {}
            if not states:
                time.sleep(0.3)
                continue

            if states.get('plasma', 1):
                self.controller.handle_command('off_plasma')
                self.parent.StatusLine.setText(self.translator.tr('plasma_off'))

            valves_states = self.safe_get_valves_states()
            if valves_states.get('valve_ve1', 'open') == 'open':
                self.controller.handle_command('close_valve_ve1')
                self.parent.StatusLine.setText(self.translator.tr('ve1_close'))
            
            if valves_states.get('valve_ve2', 'open') == 'open':
                self.controller.handle_command('close_valve_ve2')
                self.parent.StatusLine.setText(self.translator.tr('ve2_close'))
            
            if valves_states.get('valve_ve01', 'open') == 'open':
                self.controller.handle_command('close_valve_ve01')
                self.parent.StatusLine.setText(self.translator.tr('ve01_close'))

            if states.get('pump', 1):
                self.controller.handle_command('off_pump')
                self.parent.StatusLine.setText(self.translator.tr('pump_off'))
                time.sleep(0.2)  # Задержка для выключения насоса

            states = self.controller.handle_command('get_states')
            if not isinstance(states, dict):
                states = {}
            valves_states = self.safe_get_valves_states()
            if states and valves_states and all([
                    states.get('plasma', 1) == 0,
                    valves_states.get('valve_ve1', 'close') == 'close',
                    valves_states.get('valve_ve2', 'close') == 'close',
                    valves_states.get('valve_ve01', 'close') == 'close',
                    states.get('pump', 1) == 0,
                    ]):
                logging.info("stop_process: All devices turned off successfully")
                break
            
            time.sleep(0.3)  # Задержка между попытками

        start_process_logger.info(f"[STOP_PROCESS] stop_process: Обновляем статус на 'stop_process'")
        self.parent.update_status(self.translator.tr('stop_process'))

        self.parent.ButtonStart.setEnabled(True)
        self.parent.NIButton.setEnabled(True)
        self.parent.VEButton.setEnabled(True)
        self.parent.HFButton.setEnabled(True)
        self.parent.VE0Button.setEnabled(True)

        self.parent.RecName.deselect()
            
        QTimer.singleShot(1000, lambda: self.parent.update_status(self.translator.tr('system_ready_tech')))

        self.parent.ButtonStart.setText(self.translator.tr('start'))
        self.parent.ButtonStart.setIcon(QtGui.QIcon('ui/Pictures13/Start.png'))
        # LED будет синхронизирован в main_window.check_permissions() или update_values()
        # на основе состояния кнопки (enabled и текст)
        