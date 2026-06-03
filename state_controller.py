import time
import threading
import minimalmodbus
import board
import busio
from concurrent.futures import ThreadPoolExecutor

from gpiozero import Button, DigitalOutputDevice

import logging
from logging.handlers import RotatingFileHandler

i2c = busio.I2C(board.SCL, board.SDA)
from libs.ADC1115 import ADS1115
import libs.PCF8574 as PCF8574
from libs.ADC1115.AnalogIn import AnalogIn
import fun
from pins_GPIO import dict_pins
from config.settings import save_settings, settings


log_handler = RotatingFileHandler(filename="app_controller.log", maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.DEBUG, handlers=[log_handler])


class RRG_MFC_UT:
    """type_gas: 0=Air, 1=Ar, 2=O2, 3=N2, 4=Свой газ (коэффициент из настроек coef_rrg1/coef_rrg2)."""
    def __init__(self, port, device_id=1, rrg_num=None):
        self.port = port
        self.device_id = device_id
        self.rrg_num = rrg_num  # 1 или 2 — для "свой газ" берём coef_rrg1/coef_rrg2 из настроек
        self.max_attempts = 3  # Увеличено до 3 попыток для надежности
        self._lock = threading.Lock()  # Блокировка для thread-safe доступа
        self.operation_timeout = 0.3  # Увеличен таймаут операции до 4 секунд для медленных устройств
        self.serial_timeout = 0.9  # Увеличен таймаут для serial порта для медленных ответов

        self.coef_gas = {
            '0': 1.001, # Air
            '1': 1.6, # Ar
            '2': 1.025, # O2
            '3': 1.00,  # N2
            # '4' — свой газ: коэффициент берётся из settings в момент read/set
        }

        try:
            self.instrument = minimalmodbus.Instrument(port=port, slaveaddress=self.device_id)
            self.instrument.serial.baudrate = settings.get('BAUDRATE_RRG')
            self.instrument.serial.stopbits = 1
            self.instrument.serial.bytesize = 8
            self.instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
            self.instrument.serial.timeout = self.serial_timeout  # Увеличено для учета медленных ответов
            
            try:
                with self._lock:
                    self.instrument.write_register(0x0021, value=0, number_of_decimals=0, signed=False, functioncode=6)
            except Exception as e:
                logging.error(f"Error setting flow control mode RRG {self.device_id}: {e}")

            logging.info(f"RRG {self.device_id} flow control mode set successfully")

        except Exception as e:
            logging.error(f"Error init flow RRG: {str(e)}")

    def read_set_flow(self, type_gas):
        """Чтение установленного потока с защитой от зависаний"""
        start_time = time.time()
        with self._lock:  # Thread-safe доступ к serial порту
            for attempt in range(self.max_attempts):
                # Проверяем общий таймаут операции
                if time.time() - start_time > self.operation_timeout:
                    logging.warning(f"RRG {self.device_id} read_set_flow: operation timeout ({self.operation_timeout}s)")
                    return None
                
                try:
                    resp = self.instrument.read_registers(registeraddress=0x0022, number_of_registers=2)
                    high, low = resp
                    set_flow_raw = (high << 16) | low
                    # Проверяем, что type_gas не None и валиден
                    if type_gas is None:
                        logging.error(f"RRG {self.device_id} read_set_flow: type_gas is None")
                        return None
                    # Обрабатываем случай, когда type_gas < 0 (например, -1 означает "не выбран")
                    # В этом случае используем значение по умолчанию 0
                    if int(type_gas) < 0:
                        logging.warning(f"RRG {self.device_id} read_set_flow: type_gas={type_gas} is negative, using default type_gas=0")
                        type_gas = 0
                    # Преобразуем в строку для поиска в словаре (ключи - строки: '0', '1', '2', '3', '4'=свой газ)
                    type_gas_str = str(int(type_gas))  # Сначала int для обработки float, потом str
                    if type_gas_str == '4' and self.rrg_num is not None:
                        coef = float(settings.get(f'coef_rrg{self.rrg_num}', 1.0))
                    else:
                        coef = self.coef_gas.get(type_gas_str, 1.0)  # Используем 1.0 по умолчанию
                    set_flow = (set_flow_raw / 100.0) * coef * 0.06
                    # Округляем до 0.1 для согласованности с read_flow
                    set_flow_rounded = round(set_flow, 1)
                    return set_flow_rounded
                except Exception as e:
                    elapsed = time.time() - start_time
                    if elapsed > self.operation_timeout:
                        logging.warning(f"RRG {self.device_id} read_set_flow: timeout after {elapsed:.2f}s")
                        return None
                    if attempt < self.max_attempts - 1:
                        # Прогрессивная задержка: увеличиваем время между попытками, если устройство не отвечает
                        delay = 0.3 * (attempt + 1)  # 0.3s, 0.6s, 0.9s... (увеличено для более надежной работы)
                        logging.debug(f"RRG {self.device_id} read_set_flow: attempt {attempt + 1} failed, waiting {delay:.2f}s before retry")
                        time.sleep(delay)
                        continue
                    logging.error(f"Modbus error read_set_flow RRG_MFC_UT device {self.device_id}: {e}")

        logging.warning(f"RRG {self.device_id} read_set_flow: max attempts reached, no response")
        return None    
        
    def read_flow(self, type_gas):
        """Чтение текущего потока с защитой от зависаний"""
        start_time = time.time()
        
        with self._lock:  # Thread-safe доступ к serial порту
            for attempt in range(self.max_attempts):
                # Проверяем общий таймаут операции
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout:
                    return None
                
                try:
                    resp = self.instrument.read_long(registeraddress=0x0016, functioncode=3, signed=False)
                    # Проверяем, что type_gas не None и валиден
                    if type_gas is None:
                        logging.error(f"RRG {self.device_id} read_flow: type_gas is None")
                        return None
                    # Обрабатываем случай, когда type_gas < 0 (например, -1 означает "не выбран")
                    # В этом случае используем значение по умолчанию 0
                    if int(type_gas) < 0:
                        logging.warning(f"RRG {self.device_id} read_flow: type_gas={type_gas} is negative, using default type_gas=0")
                        type_gas = 0
                    # Преобразуем в строку для поиска в словаре (ключи - строки: '0', '1', '2', '3', '4'=свой газ)
                    type_gas_str = str(int(type_gas))  # Сначала int для обработки float, потом str
                    if type_gas_str == '4' and self.rrg_num is not None:
                        coef = float(settings.get(f'coef_rrg{self.rrg_num}', 1.0))
                    else:
                        coef = self.coef_gas.get(type_gas_str, 1.0)  # Используем 1.0 по умолчанию
                    flow = float((resp / 100.0) * coef * 0.06)
                    flow_rounded = round(flow, 1)
                    return flow_rounded
                except Exception as e:
                    elapsed = time.time() - start_time
                    if elapsed > self.operation_timeout:
                        return None
                    if attempt < self.max_attempts - 1:
                        # Прогрессивная задержка: увеличиваем время между попытками, если устройство не отвечает
                        delay = 0.3 * (attempt + 1)  # 0.3s, 0.6s, 0.9s... (увеличено для более надежной работы)
                        logging.debug(f"RRG {self.device_id} read_flow: attempt {attempt + 1} failed, waiting {delay:.2f}s before retry")
                        time.sleep(delay)
                        continue
                    logging.error(f"Modbus error read_flow RRG_MFC_UT device {self.device_id}: {e}")

        logging.warning(f"RRG {self.device_id} read_flow: max attempts reached, no response")
        return None

    def set_flow(self, flow_value, type_gas):
        """Установка потока с защитой от зависаний"""
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: RRG {self.device_id} set_flow STARTED in thread {thread_id}, flow_value={flow_value}, type_gas={type_gas}")
        start_time = time.time()
        
        if type_gas is None:
            logging.error(f"RRG {self.device_id} set_flow: type_gas is None")
            return False
        
        if int(type_gas) < 0:
            type_gas = 0
        
        type_gas_str = str(int(type_gas))
        if type_gas_str == '4' and self.rrg_num is not None:
            coef = float(settings.get(f'coef_rrg{self.rrg_num}', 1.0))
        else:
            coef = self.coef_gas.get(type_gas_str)
        if coef is None:
            coef = 1.0
        
        flow_value = flow_value * 1000 / 60 # перевод в sccm
        flow_raw = int(flow_value * 100 / coef)
        logging.info(f"DEBUG: RRG {self.device_id} set_flow: flow_raw={flow_raw}, coef={coef}")
        
        with self._lock:
            for attempt in range(self.max_attempts):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout:
                    return False
                try:
                    resp = self.instrument.write_long(registeraddress=0x0022, value=flow_raw, signed=False)
                    return True
                except Exception as e:
                    elapsed = time.time() - start_time
                    if elapsed > self.operation_timeout:
                        return False
                    if attempt < self.max_attempts - 1:
                        delay = 0.3 * (attempt + 1)
                        continue

        return False
        
    def close(self):
        try:
            with self._lock:
                self.instrument.serial.close()
            logging.info("MFC-UT Modbus connection close")
        except Exception as e:
            logging.error(f"Error closing MFC-UT connection: {str(e)}")

class RSG1000S:
    def __init__(self, port, device_id=4):
        self.port = port
        self.address = device_id
        
        self.max_attempts = 10
        self._lock = threading.Lock()  # Блокировка для thread-safe доступа к serial порту
        self.operation_timeout = 0.45  # Общий таймаут на операцию (секунды)
        self.serial_timeout = 0.15  # Таймаут для serial порта

        try:
            self.instrument = minimalmodbus.Instrument(port=self.port, slaveaddress=self.address)
            self.instrument.serial.baudrate = settings.get('BAUDRATE_RF')
            self.instrument.serial.stopbits = 1
            self.instrument.serial.bytesize = 8
            self.instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
            self.instrument.serial.timeout = self.serial_timeout

        except Exception as e:
            logging.error(f"Error init RF {str(e)}")

    def on_plasma(self):
        """Включение плазмы с защитой от зависаний"""
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: RF on_plasma STARTED in thread {thread_id}")
        start_time = time.time()
        
        with self._lock:  # Thread-safe доступ к serial порту
            for attempt in range(self.max_attempts):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout:
                    logging.warning(f"DEBUG: RF on_plasma: operation timeout ({elapsed:.3f}s > {self.operation_timeout}s)")
                    return False
                
                try:
                    logging.info(f"DEBUG: RF on_plasma: Attempt {attempt + 1}/{self.max_attempts}, calling write_bit...")
                    write_start = time.time()
                    self.instrument.write_bit(registeraddress=0x0000, value=1)
                    write_elapsed = time.time() - write_start
                    total_elapsed = time.time() - start_time
                    logging.info(f"DEBUG: RF on_plasma: write_bit completed in {write_elapsed:.3f}s, total={total_elapsed:.3f}s")
                    logging.info(f"RF turned ON successfully")
                    return True
                except Exception as e:
                    elapsed = time.time() - start_time
                    logging.error(f"DEBUG: RF on_plasma: Exception on attempt {attempt + 1}: {e}")
                    if elapsed > self.operation_timeout:
                        logging.warning(f"DEBUG: RF on_plasma: timeout after {elapsed:.3f}s")
                        return False
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)  # Небольшая задержка между попытками
                        continue
                    logging.error(f"Modbus error turning ON plasma: {e}")

        total_elapsed = time.time() - start_time
        logging.error(f"DEBUG: RF on_plasma: max attempts reached after {total_elapsed:.3f}s")
        return False
           
    def off_plasma(self):
        """Выключение плазмы с защитой от зависаний - КРИТИЧЕСКИ ВАЖНО для безопасности"""
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: RF off_plasma STARTED in thread {thread_id}")
        start_time = time.time()
        
        # Увеличиваем количество попыток для критически важной операции
        max_attempts_off = max(self.max_attempts, 5)  # Минимум 5 попыток для отключения
        
        with self._lock:  # Thread-safe доступ к serial порту
            for attempt in range(max_attempts_off):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout * 2:  # Увеличенный таймаут для критической операции
                    logging.warning(f"DEBUG: RF off_plasma: operation timeout ({elapsed:.3f}s > {self.operation_timeout * 2}s)")
                    return False
                
                try:
                    logging.info(f"DEBUG: RF off_plasma: Attempt {attempt + 1}/{max_attempts_off}, calling write_bit...")
                    write_start = time.time()
                    self.instrument.write_bit(registeraddress=0x0000, value=0)
                    write_elapsed = time.time() - write_start
                    total_elapsed = time.time() - start_time
                    logging.info(f"DEBUG: RF off_plasma: write_bit completed in {write_elapsed:.3f}s, total={total_elapsed:.3f}s")
                    logging.info(f"RF turned OFF successfully")
                    return True
                except Exception as e:
                    elapsed = time.time() - start_time
                    logging.error(f"DEBUG: RF off_plasma: Exception on attempt {attempt + 1}: {e}")
                    
                    # При ошибках связи увеличиваем задержку и продолжаем попытки
                    if attempt < max_attempts_off - 1:
                        delay = 0.1 * (attempt + 1)  # Прогрессивная задержка: 0.1s, 0.2s, 0.3s...
                        logging.warning(f"DEBUG: RF off_plasma: Waiting {delay:.2f}s before retry (attempt {attempt + 1}/{max_attempts_off})...")
                        time.sleep(delay)
                        
                        # Пытаемся очистить буферы порта перед следующей попыткой
                        try:
                            if hasattr(self.instrument, 'serial'):
                                if hasattr(self.instrument.serial, 'reset_input_buffer'):
                                    self.instrument.serial.reset_input_buffer()
                                if hasattr(self.instrument.serial, 'reset_output_buffer'):
                                    self.instrument.serial.reset_output_buffer()
                        except Exception as clear_error:
                            logging.debug(f"DEBUG: RF off_plasma: Could not clear buffers: {clear_error}")
                        
                        continue
                    
                    logging.error(f"Modbus error turning OFF plasma: {e}")

        total_elapsed = time.time() - start_time
        logging.error(f"DEBUG: RF off_plasma: CRITICAL - max attempts ({max_attempts_off}) reached after {total_elapsed:.3f}s")
        logging.error(f"DEBUG: RF off_plasma: CRITICAL SAFETY WARNING - Plasma may still be ON!")
        return False

    def read_status(self):
        """
        0x0000 - forward power,
        0x0001 - reflected power,
        0x0002 - status bits
        """
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: RF read_status STARTED in thread {thread_id}")
        start_time = time.time()
        
        with self._lock:  # Thread-safe доступ к serial порту
            for attempt in range(self.max_attempts):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout:
                    logging.warning(f"DEBUG: RF read_status: operation timeout ({elapsed:.3f}s > {self.operation_timeout}s)")
                    return None
                
                try:
                    logging.info(f"DEBUG: RF read_status: Attempt {attempt + 1}/{self.max_attempts}, calling read_registers...")
                    read_start = time.time()
                    resp = self.instrument.read_registers(registeraddress=0x0000, number_of_registers=3)
                    read_elapsed = time.time() - read_start
                    logging.info(f"DEBUG: RF read_status: read_registers completed in {read_elapsed:.3f}s")

                    forward = resp[0]
                    reflect = resp[1]
                    status = resp[2]

                    status_bits = {
                        "rf_on": bool(status & (1 << 7)),
                        "fault": bool(status & (1 << 6)),
                        "interlock_open": bool(status & (1 << 5)),
                        "over_voltage": bool(status & (1 << 4)),
                        "over_current": bool(status & (1 << 3)),
                        "over_heat": bool(status & (1 << 2)),
                        "over_reflect": bool(status & (1 << 1))
                    }

                    result = {
                        "forward_w": forward,
                        "reflect_w": reflect,
                        "status_raw": status,
                        **status_bits
                    }

                    total_elapsed = time.time() - start_time
                    logging.info(f"DEBUG: RF read_status COMPLETED in {total_elapsed:.3f}s, result: {result}")
                    return result
                except Exception as e:
                    elapsed = time.time() - start_time
                    logging.error(f"DEBUG: RF read_status: Exception on attempt {attempt + 1}: {e}")
                    if elapsed > self.operation_timeout:
                        logging.warning(f"DEBUG: RF read_status: timeout after {elapsed:.3f}s")
                        return None
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)  # Небольшая задержка между попытками
                        continue
                    logging.error(f"Error in reading status: {e}")

        total_elapsed = time.time() - start_time
        logging.error(f"DEBUG: RF read_status: max attempts reached after {total_elapsed:.3f}s")
        return None

    def set_power(self, power):
        """Установка мощности с защитой от зависаний"""
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: RF set_power STARTED in thread {thread_id}, power={power}, type={type(power)}")
        start_time = time.time()
        
        # Проверяем и преобразуем power
        if power is None:
            logging.error(f"DEBUG: RF set_power: power is None")
            return False
        
        try:
            power_int = int(float(power))  # Преобразуем через float для обработки строк
            if power_int < 0 or power_int > 1000:
                logging.error(f"DEBUG: RF set_power: power value {power_int} is out of range (0-1000)")
                return False
        except (ValueError, TypeError) as e:
            logging.error(f"DEBUG: RF set_power: Cannot convert power to int: {e}, power={power}")
            return False
        
        with self._lock:  # Thread-safe доступ к serial порту
            for attempt in range(self.max_attempts):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout:
                    logging.warning(f"DEBUG: RF set_power: operation timeout ({elapsed:.3f}s > {self.operation_timeout}s)")
                    return False
                
                try:
                    logging.info(f"DEBUG: RF set_power: Attempt {attempt + 1}/{self.max_attempts}, calling write_register with power={power_int}...")
                    write_start = time.time()
                    self.instrument.write_register(registeraddress=0, value=power_int, number_of_decimals=0, functioncode=6)
                    write_elapsed = time.time() - write_start
                    total_elapsed = time.time() - start_time
                    logging.info(f"DEBUG: RF set_power: write_register completed in {write_elapsed:.3f}s, total={total_elapsed:.3f}s")
                    logging.info(f"Set power to {power_int} W successfully") 
                    return True
                except Exception as e:
                    elapsed = time.time() - start_time
                    logging.error(f"DEBUG: RF set_power: Exception on attempt {attempt + 1}: {e}")
                    if elapsed > self.operation_timeout:
                        logging.warning(f"DEBUG: RF set_power: timeout after {elapsed:.3f}s")
                        return False
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)  # Небольшая задержка между попытками
                        continue
                    logging.error(f"Error in setting power to {power} W: {e}")

        total_elapsed = time.time() - start_time
        logging.error(f"DEBUG: RF set_power: max attempts reached after {total_elapsed:.3f}s")
        return False            

    def get_power(self):
        """Получение мощности с защитой от зависаний"""
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: RF get_power STARTED in thread {thread_id}")
        start_time = time.time()
        
        with self._lock:  # Thread-safe доступ к serial порту
            for attempt in range(self.max_attempts):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout:
                    logging.warning(f"DEBUG: RF get_power: operation timeout ({elapsed:.3f}s > {self.operation_timeout}s)")
                    return None
                
                try:
                    logging.info(f"DEBUG: RF get_power: Attempt {attempt + 1}/{self.max_attempts}, calling read_registers...")
                    read_start = time.time()
                    resp = self.instrument.read_registers(registeraddress=0x0000, functioncode=3)
                    read_elapsed = time.time() - read_start
                    total_elapsed = time.time() - start_time
                    logging.info(f"DEBUG: RF get_power: read_registers completed in {read_elapsed:.3f}s, total={total_elapsed:.3f}s")
                    logging.debug(f"Result of get power: {resp}")
                    return resp
                except Exception as e:
                    elapsed = time.time() - start_time
                    logging.error(f"DEBUG: RF get_power: Exception on attempt {attempt + 1}: {e}")
                    if elapsed > self.operation_timeout:
                        logging.warning(f"DEBUG: RF get_power: timeout after {elapsed:.3f}s")
                        return None
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)  # Небольшая задержка между попытками
                        continue
                    logging.error(f"Error in get power: {e}")

        total_elapsed = time.time() - start_time
        logging.error(f"DEBUG: RF get_power: max attempts reached after {total_elapsed:.3f}s")
        return None

    def get_reflected_power(self):
        for _ in range(self.max_attempts):
            status = self.read_status()
            if status:
                return status.get('reflect_w')
            
        logging.error("max attempts get reflected power")
        return None

    def get_forward_power(self):
        for _ in range(self.max_attempts):
            status = self.read_status()
            if status:
                return status.get('forward_w')
            
        logging.error("max attempts get forward power")
        return None

    def close(self):
        try:
            self.off_plasma()
            self.instrument.serial.close()
            logging.info('RSG1000S successfully closed.')
        except Exception as e:
            logging.error(f"Error in close RSG1000S: {e}")

class APEL_M_1_5PDC:
    """
    Источник питания магнетронной распылительной системы APEL-M-1.5PDC-1000-1.
    Интерфейс: RS-485, протокол RTU ModBus.

    Регистры (FC4 — Input Registers, FC3/F6 — Holding Registers, FC1/F5 — Coils):
      Coil_ONOFF    addr=0  Вкл./Выкл. источника
      Coil_StTimer  addr=1  Вкл./Выкл. таймера
      Coil_RstTimer addr=2  Сброс таймера
      Coil_IgnOn    addr=3  Вкл./Выкл. генератора поджига

      IReg_State    0x00  Состояние (0=норма, 1=заблокирован, 2=ошибка настроек)
      IReg_Voltage  0x02  Выходное напряжение, В
      IReg_Current  0x03  Выходной ток, мА
      IReg_Power    0x04  Выходная мощность, Вт

      HReg_StabMode 0x10  Режим стабилизации (0=U, 1=I, 2=P)
      HReg_Voltage  0x11  Уставка напряжения, В  (100..1000)
      HReg_Current  0x12  Уставка тока, мА       (75..1500)
      HReg_Power    0x13  Уставка мощности, Вт   (100..1500)
      HReg_Mode     0x14  Режим работы (0=DC, 1=импульсный)
      HReg_Freq     0x15  Частота импульсов, кГц (1..100)
      HReg_Tau      0x16  Коэффициент заполнения, % (10..80)
      HReg_RemCtrl  0x1A  Блокировка ручного упр. (0=разрешено, 1=заблокировано)
      HReg_ArcCnt   0x1B  Счётчик дуг (запись 0 — сброс)
    """

    def __init__(self, port, device_id=1):
        self.port = port
        self.address = device_id

        self.max_attempts = 10
        self._lock = threading.Lock()
        self.operation_timeout = 0.3
        self.serial_timeout = 0.9

        try:
            self.instrument = minimalmodbus.Instrument(port=self.port, slaveaddress=self.address)
            self.instrument.serial.baudrate = settings.get('BAUDRATE_PDC')
            self.instrument.serial.stopbits = 1
            self.instrument.serial.bytesize = 8
            self.instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
            self.instrument.serial.timeout = self.serial_timeout
            logging.info("APEL_M_1_5PDC Modbus connection initialized successfully")

            self.set_stab_mode(2) # Режим стабилизации по мощности
            self.set_power(0)      # Установка нулевой мощности при инициализации для безопасности
            self.ignition_off()     # Выключение генератора поджига при инициализации для безопасности

        except Exception as e:
            logging.error(f"Error init APEL_M_1_5PDC: {e}")

    def read_status(self):
        raw = self._read_status()
        if raw is None:
            return None

        return {
            'forward_w': raw.get('power_w', 0),
            'reflect_w': 0,  # APEL не предоставляет отражённую мощность
            'rf_on': raw.get('state_code') == 0 and raw.get('power_w', 0) > 0,
        }

    # ── Вкл./Выкл. ──────────────────────────────────────────────────────────

    def on(self):
        """Включение источника питания (Coil_ONOFF = 1)."""
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: APEL on STARTED in thread {thread_id}")
        start_time = time.time()

        with self._lock:
            for attempt in range(self.max_attempts):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout:
                    logging.warning(f"DEBUG: APEL on: operation timeout ({elapsed:.3f}s)")
                    return False
                try:
                    write_start = time.time()
                    self.instrument.write_bit(registeraddress=0x0000, value=1)
                    logging.info(f"DEBUG: APEL on: write_bit in {time.time() - write_start:.3f}s")
                    logging.info("APEL turned ON successfully")
                    return True
                except Exception as e:
                    logging.error(f"DEBUG: APEL on: Exception on attempt {attempt + 1}: {e}")
                    if time.time() - start_time > self.operation_timeout:
                        return False
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)
                        continue
                    logging.error(f"Modbus error turning ON APEL: {e}")

        logging.error(f"DEBUG: APEL on: max attempts reached")
        return False

    def off(self):
        """Выключение источника питания (Coil_ONOFF = 0). КРИТИЧЕСКИ ВАЖНО для безопасности."""
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: APEL off STARTED in thread {thread_id}")
        start_time = time.time()

        max_attempts_off = max(self.max_attempts, 5)

        with self._lock:
            for attempt in range(max_attempts_off):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout * 2:
                    logging.warning(f"DEBUG: APEL off: operation timeout ({elapsed:.3f}s)")

                try:
                    write_start = time.time()
                    self.instrument.write_bit(registeraddress=0x0000, value=0)
                    logging.info(f"DEBUG: APEL off: write_bit in {time.time() - write_start:.3f}s")
                    logging.info("APEL turned OFF successfully")
                    return True
                except Exception as e:
                    logging.error(f"DEBUG: APEL off: Exception on attempt {attempt + 1}: {e}")
                    if attempt < max_attempts_off - 1:
                        delay = 0.1 * (attempt + 1)
                        logging.warning(f"DEBUG: APEL off: Waiting {delay:.2f}s before retry...")
                        time.sleep(delay)
                        try:
                            if hasattr(self.instrument, 'serial'):
                                if hasattr(self.instrument.serial, 'reset_input_buffer'):
                                    self.instrument.serial.reset_input_buffer()
                                if hasattr(self.instrument.serial, 'reset_output_buffer'):
                                    self.instrument.serial.reset_output_buffer()
                        except Exception as clear_error:
                            logging.debug(f"DEBUG: APEL off: Could not clear buffers: {clear_error}")
                        continue
                    logging.error(f"Modbus error turning OFF APEL: {e}")

        logging.error(f"DEBUG: APEL off: CRITICAL - max attempts reached. PS may still be ON!")
        return False

    # ── Чтение состояния ────────────────────────────────────────────────────

    def _read_status(self):
        """
        Читает Input Registers 0x00..0x04 (FC4):
          IReg_State, IReg_Res, IReg_Voltage(В), IReg_Current(мА), IReg_Power(Вт).
        Возвращает dict или None при ошибке.
        """
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: APEL read_status STARTED in thread {thread_id}")
        start_time = time.time()

        with self._lock:
            for attempt in range(self.max_attempts):
                elapsed = time.time() - start_time
                if elapsed > self.operation_timeout:
                    logging.warning(f"DEBUG: APEL read_status: timeout ({elapsed:.3f}s)")
                    return None
                try:
                    read_start = time.time()
                    resp = self.instrument.read_registers(registeraddress=0x0000, number_of_registers=5, functioncode=4)
                    logging.info(f"DEBUG: APEL read_status: completed in {time.time() - read_start:.3f}s")

                    state_code = resp[0]
                    result = {
                        "state_code": state_code,
                        "state_ok": state_code == 0,
                        "blocked": state_code == 1,
                        "settings_error": state_code == 2,
                        "voltage_v": resp[2],
                        "current_ma": resp[3],
                        "power_w": resp[4],
                    }
                    logging.info(f"DEBUG: APEL read_status COMPLETED: {result}")
                    return result
                except Exception as e:
                    logging.error(f"DEBUG: APEL read_status: Exception on attempt {attempt + 1}: {e}")
                    if time.time() - start_time > self.operation_timeout:
                        return None
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)
                        continue
                    logging.error(f"Error reading APEL status: {e}")

        logging.error("DEBUG: APEL read_status: max attempts reached")
        return None

    # ── Уставки ─────────────────────────────────────────────────────────────

    def set_stab_mode(self, mode):
        """Режим стабилизации: 0=напряжение, 1=ток, 2=мощность (HReg_StabMode 0x10)."""
        if mode not in (0, 1, 2):
            logging.error(f"APEL set_stab_mode: invalid mode={mode}")
            return False
        return self._write_holding_register(0x0010, int(mode), name="set_stab_mode")

    def set_power(self, power_w):
        """Уставка мощности, Вт (100..1500). HReg_Power 0x13."""
        val = int(float(power_w))
        if not (100 <= val <= 1500):
            logging.error(f"APEL set_power: value {val} out of range (100..1500)")
            return False
        return self._write_holding_register(0x0013, val, name="set_power")

    # ── Чтение выходных параметров ───────────────────────────────────────────

    def get_power(self):
        """Текущая выходная мощность, Вт (IReg_Power 0x04, FC4)."""
        return self._read_input_register(0x0004, name="get_power")

    # ── Генератор поджига ────────────────────────────────────────────────────

    def ignition_on(self):
        """Включить генератор поджигающих импульсов (Coil_IgnOn = 1)."""
        return self._write_coil(0x0003, 1, name="ignition_on")

    def ignition_off(self):
        """Выключить генератор поджигающих импульсов (Coil_IgnOn = 0)."""
        return self._write_coil(0x0003, 0, name="ignition_off")

    # ── Счётчик дуг ─────────────────────────────────────────────────────────

    def get_arc_count(self):
        """Чтение счётчика дуг (HReg_ArcCnt 0x1B, FC3). Возвращает 0..65535 или None."""
        return self._read_holding_register(0x001B, name="get_arc_count")

    def reset_arc_count(self):
        """Сброс счётчика дуг (HReg_ArcCnt 0x1B = 0, FC6)."""
        return self._write_holding_register(0x001B, 0, name="reset_arc_count")

    # ── Внутренние вспомогательные методы ───────────────────────────────────

    def _write_coil(self, addr, value, name="write_coil"):
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: APEL {name} STARTED in thread {thread_id}, addr=0x{addr:04X}, value={value}")
        start_time = time.time()

        with self._lock:
            for attempt in range(self.max_attempts):
                if time.time() - start_time > self.operation_timeout:
                    logging.warning(f"DEBUG: APEL {name}: timeout")
                    return False
                try:
                    self.instrument.write_bit(registeraddress=addr, value=value)
                    logging.info(f"DEBUG: APEL {name}: OK in {time.time() - start_time:.3f}s")
                    return True
                except Exception as e:
                    logging.error(f"DEBUG: APEL {name}: Exception on attempt {attempt + 1}: {e}")
                    if time.time() - start_time > self.operation_timeout:
                        return False
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)
                        continue
                    logging.error(f"Modbus error APEL {name}: {e}")

        logging.error(f"DEBUG: APEL {name}: max attempts reached")
        return False

    def _write_holding_register(self, addr, value, name="write_hreg"):
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: APEL {name} STARTED in thread {thread_id}, addr=0x{addr:04X}, value={value}")
        start_time = time.time()

        with self._lock:
            for attempt in range(self.max_attempts):
                if time.time() - start_time > self.operation_timeout:
                    logging.warning(f"DEBUG: APEL {name}: timeout")
                    return False
                try:
                    self.instrument.write_register(registeraddress=addr, value=value,
                                                   number_of_decimals=0, functioncode=6)
                    logging.info(f"DEBUG: APEL {name}: OK in {time.time() - start_time:.3f}s")
                    return True
                except Exception as e:
                    logging.error(f"DEBUG: APEL {name}: Exception on attempt {attempt + 1}: {e}")
                    if time.time() - start_time > self.operation_timeout:
                        return False
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)
                        continue
                    logging.error(f"Modbus error APEL {name}: {e}")

        logging.error(f"DEBUG: APEL {name}: max attempts reached")
        return False

    def _read_input_register(self, addr, name="read_ireg"):
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: APEL {name} STARTED in thread {thread_id}, addr=0x{addr:04X}")
        start_time = time.time()

        with self._lock:
            for attempt in range(self.max_attempts):
                if time.time() - start_time > self.operation_timeout:
                    logging.warning(f"DEBUG: APEL {name}: timeout")
                    return None
                try:
                    resp = self.instrument.read_registers(registeraddress=addr,
                                                         number_of_registers=1, functioncode=4)
                    logging.info(f"DEBUG: APEL {name}: OK in {time.time() - start_time:.3f}s, value={resp[0]}")
                    return resp[0] if resp else None
                except Exception as e:
                    logging.error(f"DEBUG: APEL {name}: Exception on attempt {attempt + 1}: {e}")
                    if time.time() - start_time > self.operation_timeout:
                        return None
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)
                        continue
                    logging.error(f"Modbus error APEL {name}: {e}")

        logging.error(f"DEBUG: APEL {name}: max attempts reached")
        return None

    def _read_holding_register(self, addr, name="read_hreg"):
        thread_id = threading.current_thread().ident
        logging.info(f"DEBUG: APEL {name} STARTED in thread {thread_id}, addr=0x{addr:04X}")
        start_time = time.time()

        with self._lock:
            for attempt in range(self.max_attempts):
                if time.time() - start_time > self.operation_timeout:
                    logging.warning(f"DEBUG: APEL {name}: timeout")
                    return None
                try:
                    resp = self.instrument.read_registers(registeraddress=addr,
                                                         number_of_registers=1, functioncode=3)
                    logging.info(f"DEBUG: APEL {name}: OK in {time.time() - start_time:.3f}s, value={resp[0]}")
                    return resp[0] if resp else None
                except Exception as e:
                    logging.error(f"DEBUG: APEL {name}: Exception on attempt {attempt + 1}: {e}")
                    if time.time() - start_time > self.operation_timeout:
                        return None
                    if attempt < self.max_attempts - 1:
                        time.sleep(0.05)
                        continue
                    logging.error(f"Modbus error APEL {name}: {e}")

        logging.error(f"DEBUG: APEL {name}: max attempts reached")
        return None

    # ── Закрытие ─────────────────────────────────────────────────────────────

    def close(self):
        try:
            self.off()
        except Exception as e:
            logging.error(f"Error turning off APEL during close: {e}")
        try:
            self.instrument.serial.close()
            logging.info("APEL_M_1_5PDC successfully closed.")
        except Exception as e:
            logging.error(f"Error closing APEL serial port: {e}")

class SensorWater:
    def __init__(self, bus_id=1, addr=0x38, read_pins=None):
        """
        Датчик потока воды - использует алгоритм из _1main.py
        Событийная модель: iEvent (1-10) и iExt (1-2)
        """
        self.bus_id = bus_id
        self.addr = addr
        self.PCB2Config = 3  # Конфигурация платы
        
        # Инициализация PCF8574 (как в старом коде)
        self.ext = PCF8574.PCF(self.addr)
        self.ext.set_i2cBus(self.bus_id)
        
        # Настройка пинов (как в старом коде, строки 3772-3783)
        self._setup_pins()
        
        # Состояние (как в старом коде, строки 3699-3703)
        self.ext_value = 0
        self.iExt = 1
        self.iEvent = 0
        self.Counter_water = 0
        self.WaterList = [0.0, 0.0]
        self.current_flow = 0.0
        self._stop = False
        self._first_measurement_done = False  # Флаг первого измерения
        self._io_error_count = 0  # Счетчик ошибок I/O для автоматического переподключения
        self._max_io_errors = 20  # После 20 ошибок подряд пытаемся переподключиться
        
        # Запуск потока измерения
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()
    
    def _setup_pins(self):
        """Инициализация пинов (как в старом коде, строки 3772-3783)"""
        # Настройка пинов как в старом коде
        self.ext.pin_mode("p0", "INPUT")
        self.ext.pin_mode("p1", "INPUT")
        self.ext.pin_mode("p2", "INPUT")
        self.ext.pin_mode("p7", "INPUT")
        self.ext.pin_mode("p6", "INPUT")
        self.ext.pin_mode("p5", "INPUT")
        self.ext.pin_mode("p3", "OUTPUT")
        self.ext.pin_mode("p4", "OUTPUT")
        
        # Инициализация управляющих сигналов (строки 3781-3783)
        try:
            self.ext.write("p3", "HIGHT")
            time.sleep(0.01)
            self.ext.write("p4", "LOW")
            time.sleep(0.01)
            self.ext.write("p3", "LOW")
            time.sleep(0.1)
        except OSError as e:
            logging.error(f"Ошибка инициализации датчика воды: {e}")
    
    def _ext_write(self, x, y):
        """Запись значения в пин PCF8574 (как ext_write в старом коде, строки 4415-4422)"""
        try:
            self.ext.write(x, y)
            # Если операция успешна, сбрасываем счетчик ошибок
            if self._io_error_count > 0:
                self._io_error_count = 0
            return True
        except (OSError, IOError) as e:
            self._io_error_count += 1
            error_code = getattr(e, 'errno', None)
            # Логируем только если это не временная ошибка I/O (121 - Remote I/O error) или если ошибок много
            if error_code != 121 or self._io_error_count >= self._max_io_errors:
                logging.warning(f"Ошибка установки выхода расширителя (count={self._io_error_count}, errno={error_code}): {e}")
            return False
        except Exception as e:
            self._io_error_count += 1
            logging.error(f"Ошибка установки выхода расширителя (unexpected, count={self._io_error_count}): {e}")
            return False
    
    def _ext_read(self, x):
        """Чтение значения пина PCF8574 (как ext_read в старом коде, строки 4424-4432)"""
        try:
            self.ext_value = self.ext.read(x)
            # Если операция успешна, сбрасываем счетчик ошибок
            if self._io_error_count > 0:
                self._io_error_count = 0
            return True
        except (OSError, IOError) as e:
            self._io_error_count += 1
            error_code = getattr(e, 'errno', None)
            # Логируем только если это не временная ошибка I/O (121 - Remote I/O error) или если ошибок много
            if error_code != 121 or self._io_error_count >= self._max_io_errors:
                logging.warning(f"Ошибка измерения выхода расширителя (count={self._io_error_count}, errno={error_code}): {e}")
            return False
        except Exception as e:
            self._io_error_count += 1
            logging.error(f"Ошибка измерения выхода расширителя (unexpected, count={self._io_error_count}): {e}")
            return False
    
    def _timer_event(self):
        """Один вызов timerEvent (как в старом коде, строки 11256-11651)"""
        # Логика переключения iEvent и iExt (строки 11256-11262)
        if self.iEvent < 10:
            self.iEvent += 1
        else:
            self.iEvent = 1
            self.iExt += 1
            if self.iExt == 3:
                self.iExt = 1
        
        # Логика для PCB2Config == 3 (строки 11590-11651)
        if self.PCB2Config == 3:
            if self.iEvent == 1:
                if self.iExt == 1:
                    self._ext_write("p3", "HIGHT")
            if self.iEvent == 2:
                if self.iExt == 1:
                    self._ext_write("p3", "LOW")
            if self.iEvent == 3:
                if self.iExt == 1:
                    self._ext_write("p4", "HIGHT")
                if self.iExt == 2:
                    self._ext_write("p4", "LOW")
            if self.iEvent == 4:
                if self.iExt == 2:
                    self._ext_read("p5")
                    self.Counter_water = 8 * self.ext_value
            if self.iEvent == 5:
                if self.iExt == 2:
                    self._ext_read("p1")
                    self.Counter_water += 16 * self.ext_value
            if self.iEvent == 6:
                if self.iExt == 2:
                    self._ext_read("p0")
                    self.Counter_water += 32 * self.ext_value
            if self.iEvent == 7:
                if self.iExt == 2:
                    self._ext_read("p2")
                    self.Counter_water += 64 * self.ext_value
            if self.iEvent == 8:
                if self.iExt == 2:
                    self._ext_read("p7")
                    self.Counter_water += 4 * self.ext_value
            if self.iEvent == 9:
                if self.iExt == 2:
                    self._ext_read("p6")
                    self.Counter_water += 2 * self.ext_value
            
            # Обработка данных (событие 10, строки 11627-11651)
            if self.iEvent == 10:
                if self.iExt == 2:
                    if self.Counter_water > 126:
                        self.Counter_water = 126
                    self.WaterList[1] = self.WaterList[0]
                    
                    # Расчет потока (строки 11634-11637)
                    if self.Counter_water != 0:
                        self.WaterList[0] = round((56.653) * (1000 / self.Counter_water) ** (-0.876), 3)
                    else:
                        self.WaterList[0] = 0.0
                    
                    self.current_flow = self.WaterList[0]
                    
                    # Отмечаем, что первое измерение выполнено
                    was_first = not self._first_measurement_done
                    if not self._first_measurement_done:
                        self._first_measurement_done = True
                        logging.info(f"Water sensor: First measurement completed - Flow={self.current_flow:.3f} л/мин, Counter={self.Counter_water}")
                    
                    # Отладочное логирование
                    # Логируем предупреждение только после первого измерения (чтобы не показывать сразу при запуске)
                    if self.Counter_water == 0:
                        if self._first_measurement_done and not was_first:
                            logging.warning(f"Water sensor: Counter=0 - возможно нет потока или проблема с датчиком, Flow={self.current_flow:.3f}")
                    else:
                        # Логируем каждое обновление потока (но не слишком часто)
                        logging.debug(f"Water sensor: Counter={self.Counter_water}, Flow={self.current_flow:.3f} л/мин, iEvent={self.iEvent}, iExt={self.iExt}")
    
    def _worker(self):
        """Основной цикл измерения (как timerEvent вызывается каждые 100мс в старом коде)"""
        while not self._stop:
            try:
                self._timer_event()
            except Exception as e:
                logging.error(f"Water sensor _worker: Error in _timer_event: {e}", exc_info=True)
                self._io_error_count += 1
            
            # Если ошибок слишком много, пытаемся переподключиться (но только если это не остановка)
            if not self._stop and self._io_error_count >= self._max_io_errors:
                logging.warning(f"Water sensor _worker: Too many I/O errors ({self._io_error_count}), attempting reconnect...")
                # Переподключаемся (но не блокируем поток надолго)
                try:
                    self.reconnect()
                except Exception as reconnect_error:
                    logging.error(f"Water sensor _worker: Reconnect failed: {reconnect_error}")
                # Если переподключение не удалось, продолжаем работу (может быть временная проблема)
            
            # Задержка 100мс между вызовами timerEvent (как в старом коде)
            time.sleep(0.1)

    def reconnect(self):
        """Переподключение датчика воды при ошибках I/O"""
        try:
            logging.warning("SensorWater.reconnect: Attempting to reconnect water sensor...")
            
            # Останавливаем старый поток
            old_stop = self._stop
            self._stop = True
            if hasattr(self, 'thread') and self.thread.is_alive():
                self.thread.join(timeout=1.0)
            
            # Пересоздаем PCF8574 объект
            try:
                if hasattr(self, 'ext'):
                    del self.ext
            except:
                pass
            
            time.sleep(0.5)  # Задержка перед переподключением
            
            # Пересоздаем объекты
            self.ext = PCF8574.PCF(self.addr)
            self.ext.set_i2cBus(self.bus_id)
            
            # Сбрасываем состояние
            self.ext_value = 0
            self.iExt = 1
            self.iEvent = 0
            self.Counter_water = 0
            self.WaterList = [0.0, 0.0]
            self.current_flow = 0.0
            self._first_measurement_done = False
            self._io_error_count = 0  # Сбрасываем счетчик ошибок
            self._stop = False
            
            # Настраиваем пины заново
            self._setup_pins()
            
            # Запускаем поток заново
            self.thread = threading.Thread(target=self._worker, daemon=True)
            self.thread.start()
            
            logging.info("SensorWater.reconnect: Water sensor reconnected successfully")
            return True
        except Exception as e:
            logging.error(f"SensorWater.reconnect: Failed to reconnect: {e}", exc_info=True)
            self._stop = old_stop  # Восстанавливаем старое состояние при ошибке
            return False
    
    def get_flow(self):
        """
        Возвращает текущий поток воды.
        Если первое измерение еще не выполнено, возвращает None,
        чтобы отличать "еще не измерено" от "поток действительно равен нулю".
        """
        if not self._first_measurement_done:
            return None  # Измерение еще не выполнено
        
        return self.current_flow

    def stop(self):
        self._stop = True
        time.sleep(0.05)
        
class Controller:
    def __init__(self):
        self.init_is_successfully = True
        self.ok_device_init = []
        self.fault_device_init = []

        # GPIO
        self.pin_button_start = dict_pins['button_start']
        self.pin_button_stop = dict_pins['button_stop']

        self.pin_led_start = dict_pins['led_start']
        self.pin_led_stop = dict_pins['led_stop']
        self.pin_led_vacuum = dict_pins['led_vacuum']

        self.pin_pump = dict_pins['pump']
        self.pin_valve_ve1 = dict_pins['ve1']
        self.pin_valve_ve2 = dict_pins['ve2']
        self.pin_valve_ve3 = dict_pins['ve3']
        self.pin_valve_ve4 = dict_pins['ve4']
        self.pin_valve_ve01 = dict_pins['ve01']
    
        self.pin_buzz = dict_pins['buzz']
        self.sensor_door = dict_pins['sensor_door']

        self.pin_bp = dict_pins['bp']

        # GPIO
        self.button_start = None
        self.button_stop = None

        self.led_start = None
        self.led_stop = None
        self.led_vacuum = None

        self.pump = None
        self.valve_ve1 = None
        self.valve_ve2 = None
        self.valve_ve3 = None
        self.valve_ve4 = None
        self.valve_ve01 = None
        
        self.buzz = None
        self.sensor_door = None
        self.sensor_pressure = None

        self.rrg_1 = None
        self.rrg_2 = None
        self.rrg_3 = None
        self.rrg_4 = None

        self.rf = None
        self.bp = None

        self.sensor_water = None
        self._cached_plasma_status = False  # Кэш статуса плазмы для быстрого доступа без блокировки UI

        self.time_start_pump = 0
        self.time_stop_pump = 0

        # ThreadPoolExecutor для асинхронного переподключения RRG
        self._rrg_reconnect_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="RRGReconnect")
        # Счетчики неудач для каждого RRG (чтобы не переподключаться слишком часто)
        self._rrg_failure_counters = {1: 0, 2: 0}
        self._max_rrg_failures_before_reconnect = 3  # Переподключаемся после 3 неудач подряд
        
        # Счетчики ошибок I/O для датчиков (для автоматического переподключения)
        self._adc_error_count = 0
        self._water_sensor_error_count = 0
        self._max_sensor_errors_before_reconnect = 3  # Уменьшено до 3 для более быстрого переподключения
        self._last_adc_reconnect_time = 0
        self._last_water_reconnect_time = 0
        self._sensor_reconnect_cooldown = 5.0  # Уменьшено до 5 секунд для более частого переподключения
        self._adc_reconnect_attempts = 0  # Счетчик попыток переподключения
        
        # Кэш последнего успешного значения давления (для работы во время плазмы)
        self._last_successful_pressure = None
        self._last_successful_pressure_time = 0
        
        # ThreadPoolExecutor для асинхронного переподключения датчиков
        self._sensor_reconnect_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="SensorReconnect")
        self._adc_reconnecting = False  # Флаг для предотвращения множественных одновременных переподключений

        ADS = ADS1115
        # Создаем I2C шину локально для возможности пересоздания
        self._i2c_bus = busio.I2C(board.SCL, board.SDA)
        self._ads1 = ADS.ADS1115(self._i2c_bus, address=0x48)
        self.ACP = AnalogIn(self._ads1, ADS.P0)
        # self.ACT = AnalogIn(self._ads1, ADS.P3)

        self.init_devices()

    def reconnect_device(self, device_name):
        try: 
            if device_name.startswith('rrg'):
                rrg_num = device_name.split('_')[-1]
                rrg = getattr(self, f"rrg_{rrg_num}")
                address_key = f'ADDRESS_RRG{rrg_num}'

                if rrg is not None:
                    try:
                        rrg.close()
                    except Exception as e:
                        pass
                
                if settings.get(f'TYPE_RRG{rrg_num}') == 'MFC_UT':
                    setattr(self, f'rrg_{rrg_num}', RRG_MFC_UT(settings.get('PORT_RRG'), device_id=settings.get(address_key), rrg_num=int(rrg_num)))
                    logging.info(f"{device_name} reconnected")
                    return True, "OK"
                else:
                    return False, "Неизвестный тип РРГ"

            elif device_name == 'RF':
                if self.rf is not None:
                    try:
                        self.rf.close()
                    except:
                        pass
                
                type_rf = settings.get('TYPE_RF')
                
                if type_rf == "APEL_M_1_5PDC":
                    self.rf = APEL_M_1_5PDC(settings.get('PORT_RF'), device_id=settings.get('ADDRESS_RF'))
                    logging.info("RF reconnected")
                    return True, "OK"
                elif type_rf == "RSG1000S":
                    self.rf = RSG1000S(settings.get('PORT_RF'), device_id=settings.get('ADDRESS_RF'))
                    logging.info("RF reconnected")
                    return True, "OK"
                else:
                    return False, "Неизвестный тип RF"
            
            elif device_name == 'ADC' or device_name == 'sensor_pressure':
                # Переподключение ADC датчиков
                success = self._reconnect_adc_sensors()
                return success, "OK" if success else "Failed to reconnect ADC sensors"
            
            elif device_name == 'sensor_water' or device_name == 'water':
                # Переподключение датчика воды
                success = self._reconnect_water_sensor()
                return success, "OK" if success else "Failed to reconnect water sensor"
                
            else:
                return False
            
        except Exception as e:
            logging.error(f"Ошибка при переподключении {device_name}: {e}")
            return False, f"Исключение: {str(e)}"
    
    def _reconnect_adc_sensors(self, force_i2c_reinit=False, ignore_cooldown=False):
        """Переподключение к ADC датчикам при ошибках I/O
        
        Args:
            force_i2c_reinit: Если True, пересоздает I2C шину (для критических ошибок)
            ignore_cooldown: Если True, игнорирует cooldown (например, после отключения плазмы)
        """
        # Защита от множественных одновременных переподключений
        if self._adc_reconnecting:
            logging.debug(f"_reconnect_adc_sensors: Reconnection already in progress, skipping...")
            return False
        
        # Проверяем, действительно ли нужно переподключение (объекты уже могут быть инициализированы)
        if hasattr(self, 'ACP') and self.ACP is not None:
            try:
                # Пробуем прочитать значение, чтобы проверить, работает ли датчик
                test_value = self.ACP.value
                logging.debug(f"_reconnect_adc_sensors: ACP is already initialized and working (test_value={test_value}), skipping reconnect")
                return True
            except:
                # Если чтение не удалось, продолжаем переподключение
                pass
        
        current_time = time.time()
        # Игнорируем cooldown если плазма выключена и есть ошибки, или если явно запрошено
        plasma_off = not getattr(self, '_cached_plasma_status', False)
        if not ignore_cooldown and not (plasma_off and self._adc_error_count > 0):
            if current_time - self._last_adc_reconnect_time < self._sensor_reconnect_cooldown:
                logging.debug(f"_reconnect_adc_sensors: Cooldown active, skipping reconnect")
                return False
        
        self._adc_reconnecting = True  # Устанавливаем флаг
        try:
            self._adc_reconnect_attempts += 1
            logging.warning(f"_reconnect_adc_sensors: Attempting to reconnect ADC sensors (attempt {self._adc_reconnect_attempts})...")
            self._last_adc_reconnect_time = current_time
            
            ADS = ADS1115
            
            # Сохраняем ссылки на старые объекты перед удалением (на случай неудачи)
            old_acp = None
            old_ads1 = None
            old_i2c_bus = None
            
            # Если много неудачных попыток или принудительная переинициализация - пересоздаем I2C шину
            if force_i2c_reinit or self._adc_reconnect_attempts >= 3:
                logging.warning("_reconnect_adc_sensors: Force I2C bus reinitialization...")
                try:
                    # Сохраняем ссылки перед удалением
                    if hasattr(self, 'ACP'):
                        old_acp = self.ACP
                    if hasattr(self, '_ads1'):
                        old_ads1 = self._ads1
                    if hasattr(self, '_i2c_bus'):
                        old_i2c_bus = self._i2c_bus
                    
                    # Освобождаем старые объекты
                    if hasattr(self, 'ACP'):
                        try:
                            del self.ACP
                        except:
                            pass
                    if hasattr(self, '_ads1'):
                        try:
                            del self._ads1
                        except:
                            pass
                    if hasattr(self, '_i2c_bus'):
                        try:
                            # Пытаемся освободить I2C шину
                            if hasattr(self._i2c_bus, 'deinit'):
                                try:
                                    self._i2c_bus.deinit()
                                except:
                                    pass
                            del self._i2c_bus
                        except:
                            pass
                except Exception as cleanup_error:
                    logging.warning(f"_reconnect_adc_sensors: Error during cleanup: {cleanup_error}")
                
                # Задержка перед пересозданием
                time.sleep(1.0)
                
                # Пересоздаем I2C шину
                try:
                    self._i2c_bus = busio.I2C(board.SCL, board.SDA)
                    logging.info("_reconnect_adc_sensors: I2C bus reinitialized")
                except Exception as i2c_error:
                    logging.error(f"_reconnect_adc_sensors: Failed to reinitialize I2C bus: {i2c_error}")
                    # Восстанавливаем старые объекты при неудаче только если старый I2C валиден
                    try:
                        i2c_valid = False
                        if old_i2c_bus is not None:
                            try:
                                if hasattr(old_i2c_bus, '_i2c'):
                                    _ = old_i2c_bus._i2c
                                    i2c_valid = True
                            except (AttributeError, OSError, IOError):
                                i2c_valid = False
                        
                        if i2c_valid:
                            if old_i2c_bus is not None:
                                self._i2c_bus = old_i2c_bus
                            if old_ads1 is not None:
                                self._ads1 = old_ads1
                            if old_acp is not None:
                                self.ACP = old_acp
                            logging.debug("_reconnect_adc_sensors: Restored old objects (I2C was valid)")
                        else:
                            # Если I2C невалиден, не восстанавливаем объекты - они будут None
                            logging.warning("_reconnect_adc_sensors: Old I2C bus is invalid, not restoring objects")
                            if hasattr(self, 'ACP'):
                                try:
                                    del self.ACP
                                except:
                                    pass
                            if hasattr(self, '_ads1'):
                                try:
                                    del self._ads1
                                except:
                                    pass
                    except Exception as restore_error:
                        logging.warning(f"_reconnect_adc_sensors: Error during restore check: {restore_error}")
                        # В случае ошибки проверки, не восстанавливаем объекты
                        if hasattr(self, 'ACP'):
                            try:
                                del self.ACP
                            except:
                                pass
                        if hasattr(self, '_ads1'):
                            try:
                                del self._ads1
                            except:
                                pass
                    self._adc_reconnecting = False  # Сбрасываем флаг при неудаче
                    return False
                
                self._adc_reconnect_attempts = 0  # Сбрасываем счетчик после успешной переинициализации I2C
            else:
                # Мягкое переподключение - только объекты ADC
                try:
                    # Сохраняем ссылки перед удалением
                    if hasattr(self, 'ACP'):
                        old_acp = self.ACP
                    if hasattr(self, '_ads1'):
                        old_ads1 = self._ads1
                    
                    if hasattr(self, 'ACP'):
                        del self.ACP
                    if hasattr(self, '_ads1'):
                        del self._ads1
                except:
                    pass
                time.sleep(0.5)
            
            # Пересоздаем объекты ADC
            try:
                # Проверяем, что I2C шина валидна перед созданием объектов
                if not hasattr(self, '_i2c_bus') or self._i2c_bus is None:
                    raise ValueError("I2C bus is not initialized")
                
                # Проверяем, что I2C объект не был удален (проверяем наличие внутреннего атрибута)
                try:
                    # Пытаемся проверить состояние I2C объекта
                    if hasattr(self._i2c_bus, '_i2c'):
                        _ = self._i2c_bus._i2c  # Проверяем доступность
                except AttributeError:
                    # Если I2C объект в некорректном состоянии, пересоздаем его
                    logging.warning("_reconnect_adc_sensors: I2C bus object is invalid, recreating...")
                    try:
                        if hasattr(self._i2c_bus, 'deinit'):
                            try:
                                self._i2c_bus.deinit()
                            except:
                                pass
                    except:
                        pass
                    self._i2c_bus = busio.I2C(board.SCL, board.SDA)
                    logging.info("_reconnect_adc_sensors: I2C bus recreated")
                
                self._ads1 = ADS.ADS1115(self._i2c_bus, address=0x48)
                self.ACP = AnalogIn(self._ads1, ADS.P0)
                
                # Проверяем, что объекты работают - пытаемся прочитать значение
                test_value = self.ACP.value
                logging.debug(f"_reconnect_adc_sensors: Test read successful, value={test_value}")
                
                # Сбрасываем счетчик ошибок только при успешном переподключении
                self._adc_error_count = 0
                self._adc_reconnect_attempts = 0  # Сбрасываем счетчик попыток
                logging.info("_reconnect_adc_sensors: ADC sensors reconnected successfully")
                self._adc_reconnecting = False  # Сбрасываем флаг после успешного переподключения
                return True
            except AttributeError as attr_error:
                # Специальная обработка ошибки "'I2C' object has no attribute '_i2c'"
                error_msg = str(attr_error)
                if '_i2c' in error_msg:
                    logging.error(f"_reconnect_adc_sensors: I2C bus object is invalid (AttributeError: {error_msg}), forcing I2C reinit...")
                    # Принудительно переинициализируем I2C
                    try:
                        if hasattr(self, '_i2c_bus') and self._i2c_bus is not None:
                            try:
                                if hasattr(self._i2c_bus, 'deinit'):
                                    self._i2c_bus.deinit()
                            except:
                                pass
                    except:
                        pass
                    try:
                        self._i2c_bus = busio.I2C(board.SCL, board.SDA)
                        time.sleep(0.2)  # Небольшая задержка после пересоздания
                        # Пытаемся создать объекты ADC еще раз
                        self._ads1 = ADS.ADS1115(self._i2c_bus, address=0x48)
                        self.ACP = AnalogIn(self._ads1, ADS.P0)
                        test_value = self.ACP.value
                        logging.info(f"_reconnect_adc_sensors: Successfully recreated after I2C error, test_value={test_value}")
                        self._adc_error_count = 0
                        self._adc_reconnect_attempts = 0
                        self._adc_reconnecting = False  # Сбрасываем флаг после успешного переподключения
                        return True
                    except Exception as retry_error:
                        logging.error(f"_reconnect_adc_sensors: Failed to recreate after I2C error: {retry_error}")
                        # Не восстанавливаем старые объекты, так как они ссылаются на невалидный I2C
                        self._adc_reconnecting = False  # Сбрасываем флаг при неудаче
                        return False
                else:
                    # Другие AttributeError - обрабатываем как обычную ошибку
                    raise
            except Exception as adc_error:
                logging.error(f"_reconnect_adc_sensors: Failed to create ADC objects: {adc_error}")
                # ВАЖНО: Восстанавливаем старые объекты при неудаче только если I2C шина валидна
                try:
                    # Проверяем, что I2C шина валидна перед восстановлением
                    i2c_valid = False
                    if hasattr(self, '_i2c_bus') and self._i2c_bus is not None:
                        try:
                            if hasattr(self._i2c_bus, '_i2c'):
                                _ = self._i2c_bus._i2c
                                i2c_valid = True
                        except AttributeError:
                            i2c_valid = False
                    
                    if i2c_valid and old_ads1 is not None:
                        self._ads1 = old_ads1
                        logging.debug("_reconnect_adc_sensors: Restored old _ads1")
                    if i2c_valid and old_acp is not None:
                        self.ACP = old_acp
                        logging.debug("_reconnect_adc_sensors: Restored old ACP")
                    if not i2c_valid:
                        # Если I2C невалиден, убеждаемся, что объекты удалены
                        logging.warning("_reconnect_adc_sensors: I2C bus is invalid, not restoring objects")
                        if hasattr(self, 'ACP'):
                            try:
                                del self.ACP
                            except:
                                pass
                        if hasattr(self, '_ads1'):
                            try:
                                del self._ads1
                            except:
                                pass
                except Exception as restore_error:
                    logging.warning(f"_reconnect_adc_sensors: Failed to restore old objects: {restore_error}")
                    # В случае ошибки, не восстанавливаем объекты
                    if hasattr(self, 'ACP'):
                        try:
                            del self.ACP
                        except:
                            pass
                    if hasattr(self, '_ads1'):
                        try:
                            del self._ads1
                        except:
                            pass
                
                # Если не удалось создать объекты, пробуем переинициализировать I2C при следующей попытке
                if not force_i2c_reinit:
                    logging.warning("_reconnect_adc_sensors: Will try I2C reinit on next attempt")
                # Увеличиваем счетчик попыток для принудительной переинициализации I2C
                self._adc_reconnect_attempts += 1
                self._adc_reconnecting = False  # Сбрасываем флаг при неудаче
                return False
                
        except Exception as e:
            logging.error(f"_reconnect_adc_sensors: Failed to reconnect ADC sensors: {e}", exc_info=True)
            self._adc_reconnecting = False  # Сбрасываем флаг при ошибке
            return False
    
    def _reconnect_water_sensor(self):
        """Переподключение к датчику воды при ошибках I/O"""
        current_time = time.time()
        if current_time - self._last_water_reconnect_time < self._sensor_reconnect_cooldown:
            logging.debug(f"_reconnect_water_sensor: Cooldown active, skipping reconnect")
            return False
        
        try:
            logging.warning("_reconnect_water_sensor: Attempting to reconnect water sensor...")
            self._last_water_reconnect_time = current_time
            
            if self.sensor_water is None:
                logging.warning("_reconnect_water_sensor: sensor_water is None, creating new instance")
                self.sensor_water = SensorWater()
                self._water_sensor_error_count = 0
                logging.info("_reconnect_water_sensor: Water sensor created successfully")
                return True
            
            # Вызываем метод reconnect у датчика воды
            success = self.sensor_water.reconnect()
            if success:
                self._water_sensor_error_count = 0
                logging.info("_reconnect_water_sensor: Water sensor reconnected successfully")
            return success
        except Exception as e:
            logging.error(f"_reconnect_water_sensor: Failed to reconnect water sensor: {e}", exc_info=True)
            return False
    
    def get_values_adc(self):
        """
        Чтение значений ADC с множественными попытками при ошибках I/O.
        Датчик давления (0x48) может пропадать с I2C шины во время работы плазмы,
        поэтому делаем несколько попыток с короткими задержками, чтобы "поймать" момент доступности.
        Переподключения выполняются асинхронно, чтобы не блокировать UI.
        """
        max_retries = 3  # Уменьшено количество попыток для уменьшения блокировок UI
        retry_delay = 0.003  # Уменьшена задержка между попытками (3мс) для более быстрого чтения
        
        # Проверяем, инициализирован ли ACP - если нет, запускаем асинхронное переподключение и возвращаем None
        if not hasattr(self, 'ACP') or self.ACP is None:
            # Проверяем, не идет ли уже переподключение
            if not self._adc_reconnecting:
                logging.warning(f"get_values_adc: ACP not initialized, scheduling async reconnect...")
                # Запускаем асинхронное переподключение, чтобы не блокировать UI
                def reconnect_task():
                    try:
                        self._reconnect_adc_sensors(force_i2c_reinit=True, ignore_cooldown=True)
                    except Exception as reconnect_error:
                        logging.error(f"get_values_adc: Error during async reconnect: {reconnect_error}")
                self._sensor_reconnect_executor.submit(reconnect_task)
            return {'P': None, 'U': None, 'I': None, 'T': None}
        
        last_error = None
        for attempt in range(max_retries):
            try:
                # Проверяем, что ACP все еще инициализирован (может быть удален в другом потоке)
                if not hasattr(self, 'ACP') or self.ACP is None:
                    return {'P': None, 'U': None, 'I': None, 'T': None}
                
                # Читаем значение давления
                p_value = self.ACP.value
                values = {
                    'P': p_value,
                    'U': self.ACU.value if hasattr(self, 'ACU') and self.ACU is not None else 0,
                    'I': self.ACI.value if hasattr(self, 'ACI') and self.ACI is not None else 0,
                    'T': self.ACT.value if hasattr(self, 'ACT') and self.ACT is not None else 0
                }
                
                # Если значение 0 и были ошибки I/O, это может быть невалидное чтение
                # (датчик может возвращать 0 без исключения, когда он недоступен)
                if p_value == 0.0 and self._adc_error_count > 0:
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    # Если все попытки дали 0 и были ошибки, возвращаем None
                    logging.warning(f"get_values_adc: All {max_retries} attempts returned P=0 with I/O errors, returning None")
                    return {'P': None, 'U': None, 'I': None, 'T': None}
                
                # Если чтение успешно (не 0 или 0 без ошибок), сбрасываем счетчик ошибок и сохраняем успешное значение давления
                if self._adc_error_count > 0:
                    self._adc_error_count = 0
                    self._adc_reconnect_attempts = 0  # Также сбрасываем счетчик попыток переподключения
                # Сохраняем успешное значение давления для использования при ошибках (даже если оно 0, но только если нет ошибок I/O)
                if values.get('P') is not None:
                    self._last_successful_pressure = values.get('P')
                    self._last_successful_pressure_time = time.time()
                return values
            except (OSError, IOError) as e:
                last_error = e
                # Если это не последняя попытка, делаем короткую задержку и пробуем снова
                # Это позволяет "поймать" момент, когда датчик появляется на шине
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
            except AttributeError as attr_error:
                # Специальная обработка ошибки "'I2C' object has no attribute '_i2c'"
                error_msg = str(attr_error)
                if '_i2c' in error_msg:
                    logging.error(f"get_values_adc: I2C object is invalid (attempt {attempt + 1}): {error_msg}")
                    # Помечаем объекты как невалидные
                    try:
                        if hasattr(self, 'ACP'):
                            del self.ACP
                    except:
                        pass
                    try:
                        if hasattr(self, '_ads1'):
                            del self._ads1
                    except:
                        pass
                    # Запускаем асинхронное переподключение, чтобы не блокировать UI
                    if not self._adc_reconnecting:
                        def reconnect_task():
                            try:
                                reconnect_success = self._reconnect_adc_sensors(force_i2c_reinit=True, ignore_cooldown=True)
                                if reconnect_success:
                                    logging.info(f"get_values_adc: Successfully reconnected after I2C error (async)")
                            except Exception as reconnect_error:
                                logging.error(f"get_values_adc: Error during async I2C reconnection: {reconnect_error}")
                        self._sensor_reconnect_executor.submit(reconnect_task)
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    last_error = attr_error
                else:
                    # Другие AttributeError - обрабатываем как обычную ошибку
                    logging.error(f"get_values_adc: Unexpected AttributeError on attempt {attempt + 1}: {attr_error}", exc_info=True)
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    last_error = attr_error
            except Exception as e:
                # Неожиданная ошибка - логируем и пробуем снова
                logging.error(f"get_values_adc: Unexpected error on attempt {attempt + 1}: {e}", exc_info=True)
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                last_error = e
        
        # Если все попытки исчерпаны, обрабатываем как ошибку
        # Ошибки I/O (например, [Errno 121] Remote I/O error)
        if last_error is not None:
            self._adc_error_count += 1
            error_msg = str(last_error)
            logging.warning(f"get_values_adc: I/O error after {max_retries} attempts (count={self._adc_error_count}): {error_msg}")
            
            # Запускаем асинхронное переподключение, чтобы не блокировать UI
            if self._adc_error_count >= self._max_sensor_errors_before_reconnect and not self._adc_reconnecting:
                # Определяем, нужно ли принудительно переинициализировать I2C
                force_i2c_reinit = (self._adc_error_count >= 10) or getattr(self, '_cached_plasma_status', False)
                # Игнорируем cooldown если плазма выключена и накопилось много ошибок
                ignore_cooldown = not getattr(self, '_cached_plasma_status', False) and (self._adc_error_count >= 5)
                
                def reconnect_task():
                    try:
                        reconnect_success = self._reconnect_adc_sensors(force_i2c_reinit=force_i2c_reinit, ignore_cooldown=ignore_cooldown)
                        if reconnect_success:
                            logging.info(f"get_values_adc: Successfully reconnected after I/O errors (async)")
                    except Exception as reconnect_error:
                        logging.error(f"get_values_adc: Error during async reconnect: {reconnect_error}")
                self._sensor_reconnect_executor.submit(reconnect_task)
            # Возвращаем None, чтобы не блокировать UI
            return {'P': None, 'U': None, 'I': None, 'T': None}
        
        # Если по какой-то причине не было ошибок, но и чтения не произошло
        logging.error(f"get_values_adc: Unexpected state - no successful read and no error")
        return {'P': None, 'U': None, 'I': None, 'T': None}

    def check_devices(self):
        return True
#         devices = []
#         bus = smbus2.SMBus(1)
#         for dev in range(128):
#             try:
#                 bus.read_byte(dev)
#                 devices.append(hex(dev))
#             except Exception as e:
#                 return None
#         return devices

    def scan_modbus_rrg(self, number):
        for slave in range(1, 248):
            try:
                instrument = minimalmodbus.Instrument(port=settings.get('PORT_RRG'), slaveaddress=slave)
                instrument.serial.baudrate = settings.get("BAUDRATE_RRG")
                instrument.serial.bytesize = 8
                instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
                instrument.serial.stopbits = 1
                instrument.serial.timeout = 0.5  # Уменьшено с 1 до 0.5 секунды
            except Exception as e:
                logging.error(f"Slave {slave} error.")
                continue

            try:
                resp = instrument.read_registers(registeraddress=0x0016, number_of_registers=2)
                logging.info(f"[OK] Адрес {slave} ответил {resp}.")
                return slave
            except Exception as e:
                logging.error(f"{slave} не ответил.")
            time.sleep(0.05)

        instrument.serial.close()

    def scan_modbus_rf(self):
        for slave in range(1, 248):
            try:
                instrument = minimalmodbus.Instrument(port=settings.get('PORT_RF'), slaveaddress=slave)
                instrument.serial.baudrate = settings.get("BAUDRATE_RRG")
                instrument.serial.bytesize = 8
                instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
                instrument.serial.stopbits = 1
                instrument.serial.timeout = 0.5  # Уменьшено с 1 до 0.5 секунды
            except Exception as e:
                logging.error(f"Slave {slave} error.")
                continue

            try:
                resp = instrument.read_registers(registeraddress=0x0016, number_of_registers=2)
                logging.info(f"[OK] Адрес {slave} ответил {resp}.")
                return slave
            except Exception as e:
                logging.error(f"{slave} не ответил.")
            time.sleep(0.05)

        instrument.serial.close()

    def init_devices(self):
        try:
            self.button_start = Button(self.pin_button_start, pull_up=True)
            self.button_stop = Button(self.pin_button_stop, pull_up=False)
            self.led_start = DigitalOutputDevice(self.pin_led_start)
            self.led_stop = DigitalOutputDevice(self.pin_led_stop)
            self.led_vacuum = DigitalOutputDevice(self.pin_led_vacuum)
            self.pump = DigitalOutputDevice(self.pin_pump)
            self.valve_ve1 = DigitalOutputDevice(self.pin_valve_ve1)
            self.valve_ve2 = DigitalOutputDevice(self.pin_valve_ve2)
            self.valve_ve3 = DigitalOutputDevice(self.pin_valve_ve3)
            self.valve_ve4 = DigitalOutputDevice(self.pin_valve_ve4)
            self.valve_ve01 = DigitalOutputDevice(self.pin_valve_ve01)
            self.buzz = DigitalOutputDevice(self.pin_buzz)
            self.bp = DigitalOutputDevice(self.pin_bp)
            self.ok_device_init.append('GPIO')

        except Exception as e:
            logging.error(f"Ошибка при инициализации устройств GPIO: {e}")
            self.init_is_successfully = False
            self.fault_device_init.append('GPIO')
            
        try:
            if settings.get('TYPE_RRG1') == 'MFC_UT':
                self.rrg_1 = RRG_MFC_UT(settings.get('PORT_RRG'), device_id=settings.get('ADDRESS_RRG1'), rrg_num=1)

            if settings.get('TYPE_RRG2') == 'MFC_UT':
                self.rrg_2 = RRG_MFC_UT(settings.get('PORT_RRG'), device_id=settings.get('ADDRESS_RRG2'), rrg_num=2)

            if settings.get('TYPE_RRG3') == 'MFC_UT':
                self.rrg_3 = RRG_MFC_UT(settings.get('PORT_RRG'), device_id=settings.get('ADDRESS_RRG3'), rrg_num=3)

            if settings.get('TYPE_RRG4') == 'MFC_UT':
                self.rrg_4 = RRG_MFC_UT(settings.get('PORT_RRG'), device_id=settings.get('ADDRESS_RRG4'), rrg_num=4)

            self.ok_device_init.append('RRG')

        except Exception as e:
            logging.error(f"Ошибка при инициализации РРГ: {e}")
            self.init_is_successfully = False
            self.fault_device_init.append('RRG')
        
        try:
            type_rf = settings.get('TYPE_RF')
            if type_rf == "APEL_M_1_5PDC":
                self.rf = APEL_M_1_5PDC(settings.get('PORT_RF'), device_id=settings.get('ADDRESS_RF'))
                self.ok_device_init.append('APEL_M_1_5PDC')
            elif type_rf == "RSG1000S":
                self.rf = RSG1000S(settings.get('PORT_RF'), device_id=settings.get('ADDRESS_RF'))
                self.ok_device_init.append('RSG1000S')
            
        except Exception as e:
            logging.error(f"Ошибка при инициализации Генератора: {e}")
            self.init_is_successfully = False
            self.fault_device_init.append('RF')

        try:
            if settings.get('check_water_flow'):
                self.sensor_water = SensorWater()
                self.ok_device_init.append('sensor_water')
        except Exception as e:
            logging.error(f"Ошибка при инициализации sensor_water: {e}")
            self.init_is_successfully = False
            self.fault_device_init.append('sensor_water')

        if self.init_is_successfully == True:
            logging.info('Инициализация успешно завершена.')
            self.init_is_successfully = True
        else:
            logging.error(f"Инициализация не удалась: fault: {self.fault_device_init}, ok: {self.ok_device_init}")
            self.init_is_successfully = False

    def handle_command(self, command, num_rrg=None, flow_lh=None, power=None, type_gas=None):
        logs_text = {
            # Светодиоды
            'on_led_start': 'Включить СВЕТ СТАРТ: ',
            'off_led_start': 'Отключить СВЕТ СТАРТ: ',
            'on_led_stop': 'Включить СВЕТ СТОП: ',
            'off_led_stop': 'Отключить СВЕТ СТОП: ',
            'on_led_vacuum': 'Включить СВЕТ ВАКУУМ: ',
            'off_led_vacuum': 'Отключить СВЕТ ВАКУУМ: ',

            # Бузер
            'on_buzz': 'Включение Бузер: ',
            'off_buzz': 'Отключение Бузер: ',

            # Клапаны
            "open_valve_ve1": 'Открыть VE1: ',
            "open_valve_ve2": 'Открыть VE2: ',
            "open_valve_ve3": 'Открыть VE3: ',
            "open_valve_ve4": 'Открыть VE4: ',
            "close_valve_ve1": 'Закрыть VE1: ',
            "close_valve_ve2": 'Закрыть VE2: ',
            "close_valve_ve3": 'Закрыть VE3: ',
            "close_valve_ve4": 'Закрыть VE4: ',
            "open_valve_ve01": 'Открыть VE01: ',
            "close_valve_ve01": 'Закрыть VE01: ',
            
            # Насос
            'on_pump': 'Включить НАСОС: ',
            'off_pump': 'Отключить НАСОС: ',

            # Генератор
            'on_plasma': 'Включить ПЛАЗМА: ',
            'off_plasma': 'Отключить ПЛАЗМА: ',
            'set_power': 'Установка мощности генератора: ',
            'get_power': 'Чтение установенной мощности генератора: ',
            'get_forward_power': 'Чтение падающей мощности генератора: ',
            'get_reflected_power': 'Чтение отраженной мощности генератора: ',

            # РРГ
            'set_flow': 'Установить поток РРГ: ',
            'read_flow': 'Чтение потока РРГ: ',

            'get_sensor_water': 'Чтение потока воды:'
        }

        try:
            # Светодиоды
            if command == "on_led_start":
                self.led_start.on()
                res = (self.led_start.value == True)
                logging.info(logs_text[command] + str(res))
                return res
            
            elif command == "off_led_start":
                self.led_start.off()
                res = (self.led_start.value == False)
                logging.info(logs_text[command] + str(res))
                return res
            
            elif command == "on_led_stop":
                self.led_stop.on()
                res = (self.led_stop.value == True)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "off_led_stop":
                self.led_stop.off()
                res = (self.led_stop.value == False)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "on_led_vacuum":
                self.led_vacuum.on()
                res = (self.led_vacuum.value == True)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "off_led_vacuum":
                self.led_vacuum.off()
                res = (self.led_vacuum.value == False)
                logging.info(logs_text[command] + str(res))
                return res


            # Кнопки
            elif command == "get_state_button_start":
                return self.button_start.is_pressed

            elif command == "get_state_button_stop":
                return self.button_stop.is_pressed  

            
            # Бузер
            elif command == "on_buzz":
                self.buzz.on()
                res = (self.buzz.value == True)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "off_buzz":
                self.buzz.off()
                res = (self.buzz.value == False)
                logging.info(logs_text[command] + str(res))
                return res


            # Клапаны
            elif command == "open_valve_ve1":
                self.valve_ve1.on()
                res = (self.valve_ve1.value == True)
                logging.info(logs_text[command] + str(res))
                return res 

            elif command == "close_valve_ve1":
                self.valve_ve1.off()
                res = (self.valve_ve1.value == False)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "open_valve_ve2":
                self.valve_ve2.on()
                res = (self.valve_ve2.value == True)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "close_valve_ve2":
                self.valve_ve2.off()
                res = (self.valve_ve2.value == False)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "open_valve_ve3":
                self.valve_ve3.on()
                res = (self.valve_ve3.value == True)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "close_valve_ve3":
                self.valve_ve3.off()
                res = (self.valve_ve3.value == False)
                logging.info(logs_text[command] + str(res))
                return res
            
            elif command == "open_valve_ve4":
                self.valve_ve4.on()
                res = (self.valve_ve4.value == True)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "close_valve_ve4":
                self.valve_ve4.off()
                res = (self.valve_ve4.value == False)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "open_valve_ve01":
                self.valve_ve01.on()
                res = (self.valve_ve01.value == True)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "close_valve_ve01":
                self.valve_ve01.off()
                res = (self.valve_ve01.value == False)
                logging.info(logs_text[command] + str(res))
                return res


            # Насос
            elif command == "on_pump":
                self.pump.on()
                res = (self.pump.value == True)
                logging.info(logs_text[command] + str(res))
                if res:
                    self.time_start_pump = time.time()
                return res

            elif command == "off_pump":
                self.pump.off()
                res = (self.pump.value == False)
                logging.info(logs_text[command] + str(res))
                if res:
                    self.time_stop_pump = time.time()
                    delta = int(self.time_stop_pump - self.time_start_pump)
                    last_time = settings.get('time_pump_for_service')
                    current_time = last_time + delta
                    settings.update({'time_pump_for_service': current_time})
                    save_settings(settings)
                return res
            
            
            # Датчики
            elif command == "get_sensor_door":
                res = self.sensor_door.value
                return res
            
            elif command == "get_sensor_pressure":
                try:
                    adc_values = self.get_values_adc()
                    # Проверяем, что adc_values не None и что значение 'P' не None
                    if adc_values is None:
                        logging.warning("get_sensor_pressure: get_values_adc returned None")
                        # Используем кэшированное значение, если оно не старше 30 секунд
                        if self._last_successful_pressure is not None and (time.time() - self._last_successful_pressure_time) < 30:
                            acp = float(self._last_successful_pressure)
                            volt = fun.bit_u(acp)
                            mb = fun.p_uinp(volt)
                            result = int(mb) if mb > 1 else round(mb, 2)
                            return result
                        return 0.0
                    p_value = adc_values.get('P')
                    if p_value is None:
                        logging.warning("get_sensor_pressure: ADC value 'P' is None")
                        # Используем кэшированное значение, если оно не старше 30 секунд
                        if self._last_successful_pressure is not None and (time.time() - self._last_successful_pressure_time) < 30:
                            acp = float(self._last_successful_pressure)
                            volt = fun.bit_u(acp)
                            mb = fun.p_uinp(volt)
                            result = int(mb) if mb > 1 else round(mb, 2)
                            return result
                        return 0.0
                    
                    # Если значение 0 и были недавние ошибки I/O, используем кэшированное значение
                    # (датчик может возвращать 0 без исключения, когда он недоступен)
                    if p_value == 0.0 and self._adc_error_count > 0:
                        if self._last_successful_pressure is not None and (time.time() - self._last_successful_pressure_time) < 30:
                            acp = float(self._last_successful_pressure)
                            volt = fun.bit_u(acp)
                            mb = fun.p_uinp(volt)
                            result = int(mb) if mb > 1 else round(mb, 2)
                            return result
                    
                    acp = float(p_value)
                    volt = fun.bit_u(acp)
                    mb = fun.p_uinp(volt)
                    result = int(mb) if mb > 1 else round(mb, 2)
                    return result
                except Exception as e:
                    logging.error(f"get_sensor_pressure: Error reading pressure: {e}", exc_info=True)
                    # Используем кэшированное значение при исключении, если оно не старше 30 секунд
                    if self._last_successful_pressure is not None and (time.time() - self._last_successful_pressure_time) < 30:
                        try:
                            acp = float(self._last_successful_pressure)
                            volt = fun.bit_u(acp)
                            mb = fun.p_uinp(volt)
                            result = int(mb) if mb > 1 else round(mb, 2)
                            return result
                        except Exception as cache_error:
                            logging.error(f"get_sensor_pressure: Error using cached value: {cache_error}")
                    return 0.0
                
            elif command == "get_sensor_water":
                if self.sensor_water is None:
                    logging.warning("get_sensor_water: sensor_water is None")
                    return None
                
                try:
                    flow = self.sensor_water.get_flow()
                    # Если датчик еще не выполнил первое измерение, возвращаем 0.0 для совместимости
                    # но в state_machine.py проверяется None отдельно
                    if flow is None:
                        logging.debug("get_sensor_water: Flow is None (sensor not ready)")
                        return None
                    
                    # Если чтение успешно, сбрасываем счетчик ошибок
                    if self._water_sensor_error_count > 0:
                        self._water_sensor_error_count = 0
                    
                    logging.debug(f"get_sensor_water: Flow={flow}")
                    return flow
                except (OSError, IOError) as e:
                    # Ошибки I/O (например, [Errno 121] Remote I/O error)
                    self._water_sensor_error_count += 1
                    error_code = getattr(e, 'errno', None)
                    logging.warning(f"get_sensor_water: I/O error (count={self._water_sensor_error_count}, errno={error_code}): {e}")
                    
                    # Если ошибок много подряд, пытаемся переподключиться
                    if self._water_sensor_error_count >= self._max_sensor_errors_before_reconnect:
                        logging.warning(f"get_sensor_water: Too many I/O errors ({self._water_sensor_error_count}), attempting reconnect...")
                        # Переподключаемся асинхронно, чтобы не блокировать
                        def reconnect_task():
                            self._reconnect_water_sensor()
                        self._sensor_reconnect_executor.submit(reconnect_task)
                    
                    return None
                except Exception as e:
                    logging.error(f"get_sensor_water: Unexpected error reading water flow: {e}", exc_info=True)
                    return None

            # РРГ
            elif command == "read_set_flow":
                try:
                    rrg = getattr(self, f"rrg_{num_rrg}", None)
                    if rrg is None:
                        logging.error(f'RRG {num_rrg} not initialized')
                        return None
                    result = rrg.read_set_flow(type_gas=type_gas)
                    # Если устройство не ответило, увеличиваем счетчик неудач
                    if result is None:
                        self._rrg_failure_counters[num_rrg] = self._rrg_failure_counters.get(num_rrg, 0) + 1
                        logging.warning(f'RRG {num_rrg} не ответил (неудач подряд: {self._rrg_failure_counters[num_rrg]})')
                        
                        # Переподключаемся асинхронно только если накопилось достаточно неудач
                        if self._rrg_failure_counters[num_rrg] >= self._max_rrg_failures_before_reconnect:
                            logging.info(f'RRG {num_rrg} достиг лимита неудач, запускаем асинхронное переподключение...')
                            def reconnect_rrg_async(rrg_num):
                                try:
                                    logging.info(f'RRG {rrg_num}: начинаем переподключение...')
                                    reconnect_success, reconnect_msg = self.reconnect_device(f'rrg_{rrg_num}')
                                    if reconnect_success:
                                        logging.info(f'RRG {rrg_num} успешно переподключен')
                                        self._rrg_failure_counters[rrg_num] = 0  # Сбрасываем счетчик при успешном переподключении
                                    else:
                                        logging.error(f'Не удалось переподключить RRG {rrg_num}: {reconnect_msg}')
                                except Exception as reconnect_error:
                                    logging.error(f'Ошибка при переподключении RRG {rrg_num}: {reconnect_error}')
                            
                            # Запускаем переподключение асинхронно, не блокируя процесс
                            self._rrg_reconnect_executor.submit(reconnect_rrg_async, num_rrg)
                    else:
                        # При успешном чтении сбрасываем счетчик неудач
                        if self._rrg_failure_counters.get(num_rrg, 0) > 0:
                            self._rrg_failure_counters[num_rrg] = 0
                            logging.debug(f'RRG {num_rrg} успешно ответил, счетчик неудач сброшен')
                    return result
                except Exception as e:
                    self._rrg_failure_counters[num_rrg] = self._rrg_failure_counters.get(num_rrg, 0) + 1
                    logging.error(f'read_set_flow error: {str(e)}')
                    # Переподключаемся асинхронно при исключении, если накопилось достаточно неудач
                    if self._rrg_failure_counters.get(num_rrg, 0) >= self._max_rrg_failures_before_reconnect:
                        def reconnect_rrg_async(rrg_num):
                            try:
                                logging.info(f'RRG {rrg_num}: переподключение после исключения...')
                                reconnect_success, reconnect_msg = self.reconnect_device(f'rrg_{rrg_num}')
                                if reconnect_success:
                                    logging.info(f'RRG {rrg_num} успешно переподключен после исключения')
                                    self._rrg_failure_counters[rrg_num] = 0
                                else:
                                    logging.error(f'Не удалось переподключить RRG {rrg_num}: {reconnect_msg}')
                            except Exception as reconnect_error:
                                logging.error(f'Ошибка при переподключении RRG {rrg_num}: {reconnect_error}')
                        self._rrg_reconnect_executor.submit(reconnect_rrg_async, num_rrg)
                    return None

            elif command == "read_flow":
                try:
                    rrg = getattr(self, f"rrg_{num_rrg}", None)
                    if rrg is None:
                        logging.error(f'RRG {num_rrg} not initialized')
                        return None
                    flow = rrg.read_flow(type_gas=type_gas)
                    # Если устройство не ответило, увеличиваем счетчик неудач
                    if flow is None:
                        self._rrg_failure_counters[num_rrg] = self._rrg_failure_counters.get(num_rrg, 0) + 1
                        logging.warning(f'RRG {num_rrg} не ответил (неудач подряд: {self._rrg_failure_counters[num_rrg]})')
                        
                        # Переподключаемся асинхронно только если накопилось достаточно неудач
                        if self._rrg_failure_counters[num_rrg] >= self._max_rrg_failures_before_reconnect:
                            logging.info(f'RRG {num_rrg} достиг лимита неудач, запускаем асинхронное переподключение...')
                            def reconnect_rrg_async(rrg_num):
                                try:
                                    logging.info(f'RRG {rrg_num}: начинаем переподключение...')
                                    reconnect_success, reconnect_msg = self.reconnect_device(f'rrg_{rrg_num}')
                                    if reconnect_success:
                                        logging.info(f'RRG {rrg_num} успешно переподключен')
                                        self._rrg_failure_counters[rrg_num] = 0  # Сбрасываем счетчик при успешном переподключении
                                    else:
                                        logging.error(f'Не удалось переподключить RRG {rrg_num}: {reconnect_msg}')
                                except Exception as reconnect_error:
                                    logging.error(f'Ошибка при переподключении RRG {rrg_num}: {reconnect_error}')
                            
                            # Запускаем переподключение асинхронно, не блокируя процесс
                            self._rrg_reconnect_executor.submit(reconnect_rrg_async, num_rrg)
                    else:
                        # При успешном чтении сбрасываем счетчик неудач
                        if self._rrg_failure_counters.get(num_rrg, 0) > 0:
                            self._rrg_failure_counters[num_rrg] = 0
                            logging.debug(f'RRG {num_rrg} успешно ответил, счетчик неудач сброшен')
                    return flow
                except Exception as e:
                    self._rrg_failure_counters[num_rrg] = self._rrg_failure_counters.get(num_rrg, 0) + 1
                    logging.error(f'read_flow error: {str(e)}')
                    # Переподключаемся асинхронно при исключении, если накопилось достаточно неудач
                    if self._rrg_failure_counters.get(num_rrg, 0) >= self._max_rrg_failures_before_reconnect:
                        def reconnect_rrg_async(rrg_num):
                            try:
                                logging.info(f'RRG {rrg_num}: переподключение после исключения...')
                                reconnect_success, reconnect_msg = self.reconnect_device(f'rrg_{rrg_num}')
                                if reconnect_success:
                                    logging.info(f'RRG {rrg_num} успешно переподключен после исключения')
                                    self._rrg_failure_counters[rrg_num] = 0
                                else:
                                    logging.error(f'Не удалось переподключить RRG {rrg_num}: {reconnect_msg}')
                            except Exception as reconnect_error:
                                logging.error(f'Ошибка при переподключении RRG {rrg_num}: {reconnect_error}')
                        self._rrg_reconnect_executor.submit(reconnect_rrg_async, num_rrg)
                    return None
                    
            elif command == "set_flow":
                try:
                    rrg = getattr(self, f"rrg_{num_rrg}", None)
                    if rrg is None:
                        logging.error(f'RRG {num_rrg} not initialized')
                        return False
                    logging.info(logs_text[command] + " " + str(num_rrg) + " " + str(flow_lh))
                    result = rrg.set_flow(flow_value=flow_lh, type_gas=type_gas)
                    # Если операция не удалась, увеличиваем счетчик неудач
                    if not result:
                        self._rrg_failure_counters[num_rrg] = self._rrg_failure_counters.get(num_rrg, 0) + 1
                        logging.warning(f'RRG {num_rrg} не ответил на set_flow (неудач подряд: {self._rrg_failure_counters[num_rrg]})')
                        
                        # Переподключаемся асинхронно только если накопилось достаточно неудач
                        if self._rrg_failure_counters[num_rrg] >= self._max_rrg_failures_before_reconnect:
                            logging.info(f'RRG {num_rrg} достиг лимита неудач, запускаем асинхронное переподключение...')
                            def reconnect_rrg_async(rrg_num):
                                try:
                                    logging.info(f'RRG {rrg_num}: начинаем переподключение...')
                                    reconnect_success, reconnect_msg = self.reconnect_device(f'rrg_{rrg_num}')
                                    if reconnect_success:
                                        logging.info(f'RRG {rrg_num} успешно переподключен')
                                        self._rrg_failure_counters[rrg_num] = 0  # Сбрасываем счетчик при успешном переподключении
                                    else:
                                        logging.error(f'Не удалось переподключить RRG {rrg_num}: {reconnect_msg}')
                                except Exception as reconnect_error:
                                    logging.error(f'Ошибка при переподключении RRG {rrg_num}: {reconnect_error}')
                            
                            # Запускаем переподключение асинхронно, не блокируя процесс
                            self._rrg_reconnect_executor.submit(reconnect_rrg_async, num_rrg)
                    else:
                        # При успешной установке сбрасываем счетчик неудач
                        if self._rrg_failure_counters.get(num_rrg, 0) > 0:
                            self._rrg_failure_counters[num_rrg] = 0
                            logging.debug(f'RRG {num_rrg} успешно установил поток, счетчик неудач сброшен')
                    return result
                except Exception as e:
                    self._rrg_failure_counters[num_rrg] = self._rrg_failure_counters.get(num_rrg, 0) + 1
                    logging.error(f'set_flow error: {str(e)}')
                    # Переподключаемся асинхронно при исключении, если накопилось достаточно неудач
                    if self._rrg_failure_counters.get(num_rrg, 0) >= self._max_rrg_failures_before_reconnect:
                        def reconnect_rrg_async(rrg_num):
                            try:
                                logging.info(f'RRG {rrg_num}: переподключение после исключения...')
                                reconnect_success, reconnect_msg = self.reconnect_device(f'rrg_{rrg_num}')
                                if reconnect_success:
                                    logging.info(f'RRG {rrg_num} успешно переподключен после исключения')
                                    self._rrg_failure_counters[rrg_num] = 0
                                else:
                                    logging.error(f'Не удалось переподключить RRG {rrg_num}: {reconnect_msg}')
                            except Exception as reconnect_error:
                                logging.error(f'Ошибка при переподключении RRG {rrg_num}: {reconnect_error}')
                        self._rrg_reconnect_executor.submit(reconnect_rrg_async, num_rrg)
                    return False

            # Генератор
            elif command == "on_plasma":
                if self.rf is None:
                    logging.error("on_plasma: RF generator not initialized")
                    return False
                res = self.rf.on()
                logging.info(logs_text[command] + str(res))
                # Обновляем кэш статуса при включении
                if res:
                    self._cached_plasma_status = True
                return res

            elif command == "off_plasma":
                if self.rf is None:
                    logging.error("off_plasma: RF generator not initialized")
                    return False
                res = self.rf.off()
                logging.info(logs_text[command] + str(res))
                # Обновляем кэш статуса при выключении
                if res:
                    self._cached_plasma_status = False
                    # После отключения плазмы пытаемся переподключиться к ADC датчикам, если были ошибки
                    if self._adc_error_count > 0 and not self._adc_reconnecting:
                        logging.info("off_plasma: Plasma turned off, attempting to reconnect ADC sensors...")
                        def reconnect_after_plasma_off():
                            # Игнорируем cooldown после отключения плазмы
                            self._reconnect_adc_sensors(force_i2c_reinit=True, ignore_cooldown=True)
                        self._sensor_reconnect_executor.submit(reconnect_after_plasma_off)
                    elif self._adc_reconnecting:
                        logging.debug("off_plasma: Plasma turned off, but ADC reconnection already in progress, skipping...")
                return res

            elif command == "set_power":
                if self.rf is None:
                    logging.error("set_power: RF generator not initialized")
                    return False
                res = self.rf.set_power(power_w=power)
                logging.info(logs_text[command] + str(res))
                return res
            
            elif command == "get_reflected_power":
                return 0
                if self.rf is None:
                    logging.error("get_reflected_power: RF generator not initialized")
                    return None
                res = self.rf.get_reflected_power()
                return res
            
            elif command == "get_forward_power":
                if self.rf is None:
                    logging.error("get_forward_power: RF generator not initialized")
                    return None
                res = self.rf.get_power()
                return res
            
            elif command == "on_bp":
                self.bp.on()
                res = (self.bp.value == True)
                logging.info(logs_text[command] + str(res))
                return res

            elif command == "off_bp":
                self.bp.off()
                res = (self.bp.value == False)
                logging.info(logs_text[command] + str(res))
                return res

            # Общее
            elif command == 'get_states':
                states = {
                    'pump': bool(self.pump.value),
                    'valve_ve1': 'open' if self.valve_ve1.value else 'close',
                    'valve_ve2': 'open' if self.valve_ve2.value else 'close',
                    'valve_ve3': 'open' if self.valve_ve3.value else 'close',
                    'valve_ve4': 'open' if self.valve_ve4.value else 'close',
                    'valve_ve01': 'open' if self.valve_ve01.value else 'close',
                    'led_vacuum': bool(self.led_vacuum.value),
                    'led_start': bool(self.led_start.value),
                    'led_stop': bool(self.led_stop.value),
                    'buzz': bool(self.buzz.value),
                    
                    'plasma': getattr(self, '_cached_plasma_status', False),  # Используем кэшированное значение
                }
                return states
            
            # Получение состояний только клапанов (оптимизированная версия get_states)
            elif command == 'get_valves_states':
                try:
                    # Возвращаем только состояния клапанов без дополнительных проверок устройств
                    valves_states = {
                        'valve_ve1': 'open' if self.valve_ve1.value else 'close',
                        'valve_ve2': 'open' if self.valve_ve2.value else 'close',
                        'valve_ve3': 'open' if self.valve_ve3.value else 'close',
                        'valve_ve4': 'open' if self.valve_ve4.value else 'close',
                        'valve_ve01': 'open' if self.valve_ve01.value else 'close'
                    }
                    return valves_states
                except Exception as e:
                    logging.error(f"Ошибка при получении состояний клапанов: {e}")
                    return None
            
            else:
                logging.error(f"Неизвестная команда {command}")
                return f"Неизвестная команда {command}"

        except Exception as e:
                return f"Ошибка при выполнении команды {command}: {str(e)}"

controller = Controller()