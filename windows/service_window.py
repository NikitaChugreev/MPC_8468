import os
import statistics
import time

import minimalmodbus

from PyQt5 import QtWidgets
from PyQt5.QtCore import QThread, pyqtSignal

import fun

from ui.ui_ser.servicewindow import Ui_ServiceWindow


from config.settings import settings, save_settings

class AddressScannerThread(QThread):
    log_message = pyqtSignal(str)
    address_found = pyqtSignal(int) 
    finished = pyqtSignal(bool)
    progress = pyqtSignal(int)

    def __init__(self, port, baudrate, num, parent=None):
        super().__init__(parent)
        self.port = port
        self.baudrate = baudrate
        self.num = num
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True
        self.log_message.emit("Сканирование прервано пользователем")

    def run(self):
        self.log_message.emit(f"Начинаю сканирование адресов для РРГ{self.num} на порту {self.port}, скорость {self.baudrate}")
        good_address = 0

        for i in range(1, 248):
            if self._is_cancelled:
                break

            instrument = None
            try:
                instrument = self.make_instrument(self.port, self.baudrate, i)
                instrument.read_long(registeraddress=0x0016, functioncode=3, signed=False)
                good_address = i
                self.log_message.emit(f"✅ Адрес {i} отвечает!")
                break
            except Exception as e:
                self.log_message.emit(f"❌ {i} – ошибка: {str(e)[:50]}")
            finally:
                if instrument:
                    instrument.serial.close()

            self.progress.emit(i)
            self.msleep(100)

        if good_address:
            self.address_found.emit(good_address)
            self.finished.emit(True)
        else:
            self.log_message.emit("Ни один адрес не ответил")
            self.finished.emit(False)

    def make_instrument(self, port, baudrate, address):
        instrument = minimalmodbus.Instrument(port=port, slaveaddress=address)
        instrument.serial.baudrate = baudrate
        instrument.serial.stopbits = 1
        instrument.serial.bytesize = 8
        instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
        instrument.serial.timeout = 0.5
        return instrument

class RfAddressScannerThread(QThread):
    log_message = pyqtSignal(str)
    address_found = pyqtSignal(int)
    finished = pyqtSignal(bool)

    def __init__(self, port, baudrate, parent=None):
        super().__init__(parent)
        self.port = port
        self.baudrate = baudrate
        self.probe_serial_timeout = 5.0
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def _make_instrument(self, addr):
        inst = minimalmodbus.Instrument(port=self.port, slaveaddress=addr)
        inst.serial.baudrate = self.baudrate
        inst.serial.stopbits = 1
        inst.serial.bytesize = 8
        inst.serial.parity = minimalmodbus.serial.PARITY_NONE
        inst.serial.timeout = self.probe_serial_timeout
        return inst

    def run(self):
        self.log_message.emit(f"Сканирование RF на порту {self.port}, скорость {self.baudrate}")
        for addr in range(1, 248):
            if self._is_cancelled:
                self.log_message.emit("Сканирование прервано")
                return

            instrument = None
            try:
                instrument = self._make_instrument(addr=addr)
                instrument.read_registers(0x0000, 1, functioncode=3)[0]
                self.address_found.emit(addr)
                self.log_message.emit(f"✅ Найден адрес: {addr}")
                self.finished.emit(True)
                return
            except Exception as e:
                self.log_message.emit(f"❌ {addr}: {str(e)[:50]}")
            finally:
                if instrument:
                    instrument.serial.close()

            self.msleep(50)

        self.log_message.emit("Адрес не найден")
        self.finished.emit(False)

class MeasurementRFThread(QThread):
    update_text = pyqtSignal(str)
    finished_signal = pyqtSignal()
    progress_signal = pyqtSignal(int, int)
    
    def __init__(self, port=None, baudrate=None, device_id=None, n=40):
        super().__init__()
        self.port = port or settings.get('PORT_RF')
        self.baudrate = baudrate or settings.get('BAUDRATE_RF')
        self.device_id = device_id or settings.get('ADDRESS_RF')
        self.n = n
        self.probe_serial_timeout = 5.0
        
    def _make_instrument(self):
        inst = minimalmodbus.Instrument(port=self.port, slaveaddress=self.device_id)
        inst.serial.baudrate = self.baudrate
        inst.serial.stopbits = 1
        inst.serial.bytesize = 8
        inst.serial.parity = minimalmodbus.serial.PARITY_NONE
        inst.serial.timeout = self.probe_serial_timeout
        return inst
    
    def _measure(self, label, fn):
        times, errors = [], 0
        for i in range(self.n):
            try:
                t0 = time.perf_counter()
                fn()
                dt = time.perf_counter() - t0
                times.append(dt)
                self.progress_signal.emit(i + 1, self.n)
                self.update_text.emit(f"  [{i+1:>2}/{self.n}] {label}: {dt*1000:6.1f} ms  OK")
            except Exception as e:
                errors += 1
                self.update_text.emit(f"  [{i+1:>2}/{self.n}] {label}: ERROR — {e}")
            time.sleep(0.05)
        return times, errors
    
    def _print_stats(self, label, times, errors):
        if not times:
            self.update_text.emit(f"\n{label}: нет данных (все {errors} итераций упали)\n")
            return None, None
        
        s = sorted(times)
        p95 = s[max(0, int(len(s) * 0.95) - 1)]
        p99 = s[max(0, int(len(s) * 0.99) - 1)]
        
        self.update_text.emit(f"\n── {label} (N={len(times)}, ошибок: {errors}/{self.n}) ──")
        self.update_text.emit(f"   min  : {min(times)*1000:6.1f} ms")
        self.update_text.emit(f"   mean : {statistics.mean(times)*1000:6.1f} ms")
        if len(times) > 1:
            self.update_text.emit(f"   stdev: {statistics.stdev(times)*1000:6.1f} ms")
        self.update_text.emit(f"   p95  : {p95*1000:6.1f} ms")
        self.update_text.emit(f"   p99  : {p99*1000:6.1f} ms")
        self.update_text.emit(f"   max  : {max(times)*1000:6.1f} ms")
        return max(times), p95
    
    def _recommend(self, all_results):
        valid = [(m, p) for m, p in all_results if m is not None]
        if not valid:
            self.update_text.emit("\nНедостаточно данных для рекомендаций.")
            return
        
        worst_max = max(m for m, _ in valid)
        worst_p95 = max(p for _, p in valid)
        
        serial_rec = max(0.1, round(worst_p95 * 2.0, 2))
        op_rec = max(serial_rec * 3, round(worst_max * 3.0, 1))
        
        self.update_text.emit("\n" + "=" * 40)
        self.update_text.emit("РЕКОМЕНДАЦИИ")
        self.update_text.emit("=" * 40)
        self.update_text.emit(f"  Худшее max время ответа : {worst_max*1000:.1f} ms")
        self.update_text.emit(f"  Худшее p95 время ответа : {worst_p95*1000:.1f} ms")
        self.update_text.emit("")
        self.update_text.emit(f"  serial_timeout    = {serial_rec}   # p95 × 2.0")
        self.update_text.emit(f"  operation_timeout = {op_rec}   # max × 3.0")
        self.update_text.emit("=" * 40)
        self.update_text.emit("")
        self.update_text.emit("Текущие значения в state_controller.py:")
        self.update_text.emit("    serial_timeout    = 0.5")
        self.update_text.emit("    operation_timeout = 2.5")
        self.update_text.emit("=" * 40)
    
    def run(self):
        self.update_text.emit(f"\nПорт: {self.port}  |  Baudrate: {self.baudrate}  |  Device ID: {self.device_id}")
        self.update_text.emit(f"serial_timeout зонда: {self.probe_serial_timeout}s  |  Итераций: {self.n}\n")
        
        try:
            inst = self._make_instrument()
        except Exception as e:
            self.update_text.emit(f"Не удалось открыть порт: {e}")
            self.finished_signal.emit()
            return
        
        all_results = []
        
        self.update_text.emit("[read_status — read_registers 0x0000, count=3]")
        t, e = self._measure("read_status", lambda: inst.read_registers(0x0000, 3))
        all_results.append(self._print_stats("read_status", t, e))
        
        try:
            cur_power = inst.read_registers(0x0000, 1, functioncode=3)[0]
        except Exception:
            cur_power = 0
        self.update_text.emit(f"\n[set_power — write_register 0x0000, value={cur_power}]")
        t, e = self._measure("set_power", lambda: inst.write_register(0, cur_power, number_of_decimals=0, functioncode=6))
        all_results.append(self._print_stats("set_power", t, e))
        
        try:
            inst.serial.close()
        except Exception:
            pass
        
        self._recommend(all_results)
        self.finished_signal.emit()

class MeasurementRRGThread(QThread):
    update_text = pyqtSignal(str)
    finished_signal = pyqtSignal()
    progress_signal = pyqtSignal(int, int, int)  # device_index, current_iter, total_iter
    
    def __init__(self, port=None, baudrate=None, device_ids=None, n=40):
        super().__init__()
        self.port = port or settings.get('PORT_RRG')
        self.baudrate = baudrate or settings.get('BAUDRATE_RRG')
        self.device_ids = device_ids or [19, 16]
        self.n = n
        self.probe_serial_timeout = 5.0
        self.is_cancelled = False
        
    def cancel(self):
        self.is_cancelled = True
        self.update_text.emit("\nОтмена измерения...")
    
    def _make_instrument(self, device_id):
        inst = minimalmodbus.Instrument(port=self.port, slaveaddress=device_id)
        inst.serial.baudrate = self.baudrate
        inst.serial.stopbits = 1
        inst.serial.bytesize = 8
        inst.serial.parity = minimalmodbus.serial.PARITY_NONE
        inst.serial.timeout = self.probe_serial_timeout
        return inst
    
    def _measure(self, label, fn, device_id, operation_num, total_ops):
        times, errors = [], 0
        
        for i in range(self.n):
            if self.is_cancelled:
                return times, errors, True
            
            try:
                t0 = time.perf_counter()
                fn()
                dt = time.perf_counter() - t0
                times.append(dt)
                
                current_op = operation_num * self.n + (i + 1)
                total_ops_count = total_ops * self.n
                self.progress_signal.emit(device_id, current_op, total_ops_count)
                
                self.update_text.emit(
                    f"  [Устр.{device_id}:{i+1:>2}/{self.n}] {label}: {dt*1000:6.1f} ms  OK"
                )
            except Exception as e:
                errors += 1
                self.update_text.emit(
                    f"  [Устр.{device_id}:{i+1:>2}/{self.n}] {label}: ERROR — {e}"
                )
            
            time.sleep(0.05)
        
        return times, errors, False
    
    def _print_stats(self, label, times, errors, device_id):
        if not times:
            self.update_text.emit(
                f"\n{label} (устр.{device_id}): нет данных (все {errors} итераций упали)\n"
            )
            return None, None
        
        s = sorted(times)
        p95 = s[max(0, int(len(s) * 0.95) - 1)]
        p99 = s[max(0, int(len(s) * 0.99) - 1)]
        
        self.update_text.emit(
            f"\n── {label} (устр.{device_id}, N={len(times)}, ошибок: {errors}/{self.n}) ──"
        )
        self.update_text.emit(f"   min  : {min(times)*1000:6.1f} ms")
        self.update_text.emit(f"   mean : {statistics.mean(times)*1000:6.1f} ms")
        
        if len(times) > 1:
            self.update_text.emit(f"   stdev: {statistics.stdev(times)*1000:6.1f} ms")
        
        self.update_text.emit(f"   p95  : {p95*1000:6.1f} ms")
        self.update_text.emit(f"   p99  : {p99*1000:6.1f} ms")
        self.update_text.emit(f"   max  : {max(times)*1000:6.1f} ms")
        
        return max(times), p95
    
    def _recommend(self, all_results):
        """Формирование рекомендаций по таймаутам"""
        valid = [(m, p) for m, p in all_results if m is not None]
        
        if not valid:
            self.update_text.emit("\nНедостаточно данных для рекомендаций.")
            return
        
        worst_max = max(m for m, _ in valid)
        worst_p95 = max(p for _, p in valid)
        
        # serial_timeout: p95 * 2.0 (покрывает почти все нормальные ответы + запас)
        serial_rec = max(0.3, round(worst_p95 * 2.0, 2))
        # operation_timeout: max * 3 (хватит на 3 попытки даже в худшем случае)
        op_rec = max(serial_rec * 3, round(worst_max * 3.0, 1))
        
        self.update_text.emit("\n" + "=" * 40)
        self.update_text.emit("  РЕКОМЕНДАЦИИ ПО ТАЙМАУТАМ")
        self.update_text.emit("=" * 40)
        self.update_text.emit(f"  Худшее max время ответа : {worst_max*1000:.1f} ms")
        self.update_text.emit(f"  Худшее p95 время ответа : {worst_p95*1000:.1f} ms")
        self.update_text.emit("")
        self.update_text.emit(f"  serial_timeout    = {serial_rec}   # p95 × 2.0")
        self.update_text.emit(f"  operation_timeout = {op_rec}   # max × 3.0")
        self.update_text.emit("=" * 40)
        self.update_text.emit("")
        self.update_text.emit("  Текущие значения в коде:")
        self.update_text.emit("    serial_timeout    = 2.5")
        self.update_text.emit("    operation_timeout = 4.0")
        self.update_text.emit("=" * 40)
    
    def run(self):
        """Основной метод потока"""
        self.is_cancelled = False
        
        # Информация о тестировании
        self.update_text.emit(f"\n{'='*40}")
        self.update_text.emit(f"ИЗМЕРЕНИЕ timeout для РРГ")
        self.update_text.emit(f"{'='*40}")
        self.update_text.emit(f"Порт: {self.port}  |  Baudrate: {self.baudrate}")
        self.update_text.emit(f"Устройства: {self.device_ids}  |  Итераций: {self.n}")
        self.update_text.emit(f"serial_timeout зонда: {self.probe_serial_timeout}s")
        self.update_text.emit(f"{'='*40}\n")
        
        all_results = []
        total_operations = len(self.device_ids) * 3  # 3 операции на устройство
        
        for dev_idx, dev_id in enumerate(self.device_ids):
            # Проверка на отмену
            if self.is_cancelled:
                self.update_text.emit("\nИзмерение отменено пользователем")
                break
            
            self.update_text.emit(f"\n{'='*40}")
            self.update_text.emit(f"Устройство ID={dev_id} ({dev_idx+1}/{len(self.device_ids)})")
            self.update_text.emit(f"{'='*40}")
            
            try:
                inst = self._make_instrument(dev_id)
            except Exception as e:
                self.update_text.emit(f"Не удалось открыть порт: {e}")
                continue
            
            self.update_text.emit(f"\n[read_flow — read_long 0x0016]")
            times_rf, err_rf, cancelled = self._measure(
                "read_flow",
                lambda: inst.read_long(registeraddress=0x0016, functioncode=3, signed=False),
                dev_id, dev_idx * 3 + 0, total_operations
            )
            
            if cancelled:
                break
                
            r1 = self._print_stats("read_flow", times_rf, err_rf, dev_id)
            
            self.update_text.emit(f"\n[read_set_flow — read_registers 0x0022]")
            times_rsf, err_rsf, cancelled = self._measure(
                "read_set_flow",
                lambda: inst.read_registers(registeraddress=0x0022, number_of_registers=2),
                dev_id, dev_idx * 3 + 1, total_operations
            )
            
            if cancelled:
                break
                
            r2 = self._print_stats("read_set_flow", times_rsf, err_rsf, dev_id)
            
            try:
                cur_raw = inst.read_registers(registeraddress=0x0022, number_of_registers=2)
                flow_raw = (cur_raw[0] << 16) | cur_raw[1]
                self.update_text.emit(f"\n📝 Текущее значение уставки: {flow_raw}")
            except Exception as e:
                flow_raw = 0
                self.update_text.emit(f"\nНе удалось прочитать уставку: {e}, используем 0")
            
            self.update_text.emit(f"\n📊 [set_flow — write_long 0x0022, value={flow_raw}]")
            times_sf, err_sf, cancelled = self._measure(
                "set_flow",
                lambda: inst.write_long(registeraddress=0x0022, value=flow_raw, signed=False),
                dev_id, dev_idx * 3 + 2, total_operations
            )
            
            if cancelled:
                break
                
            r3 = self._print_stats("set_flow", times_sf, err_sf, dev_id)
            
            all_results.extend([r for r in [r1, r2, r3] if r is not None])
            
            try:
                inst.serial.close()
            except Exception:
                pass
            
            self.update_text.emit(f"\nУстройство {dev_id} обработано")
        
        # Формируем рекомендации
        if not self.is_cancelled:
            self._recommend(all_results)
        
        self.update_text.emit(f"\n{'='*40}")
        self.update_text.emit("ИЗМЕРЕНИЕ ЗАВЕРШЕНО")
        self.update_text.emit(f"{'='*40}")
        
        self.finished_signal.emit()

class ServiceWindow(QtWidgets.QMainWindow, Ui_ServiceWindow):
    def __init__(self):
        super().__init__()
        self.setupUi(self)

        self.connect_buttons()

        self.BAUD_INDEX_MAP = {
            '9600': 0, '14440': 1, '19200': 2,
            '38400': 3, '56000': 4, '57600': 5, '115200': 6
        }
        
        self.BAUD_CODE_MAP = {
            '9600': 1, '14440': 2, '19200': 3,
            '38400': 4, '56000': 5, '57600': 6, '115200': 7
        }

        self.TYPE_RF_MAP = {
            'RSG1000S': 0,
            'APEL': 1
        }
        
        self.NUM_GASES_MAP = {
            '2': 0, 
            '3': 1, 
            '4': 2
        }
        self.ANS_BOOL_MAP = {
            'true': 0, 
            'false': 1
        }

        self.ANS_BOOL_REVERSE = {
            'Да': 'true', 
            'Нет': 'false'
        }

        self.read_config()
        self.check_ports()


        self.buttons_service = [
            self.DoorButtonS, self.StartButtonS, self.StopButtonS, self.DoorLightS, self.StartLightS, self.StopLightS, 
            self.VE1ButtonS, self.VE2ButtonS,
            self.VE01ButtonS, self.NIButtonS, self.BuzzButtonS, self.ButtonClose
        ]
    
    def handle_commands(self, sender):
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

            self.parent().controller.handle_command(command=command)

        except Exception as e:
            print(e)
      

    def update_values(self):
        values_adc = self.parent().values_adc
        water_raw = self.parent().controller.handle_command('get_sensor_water')
        if values_adc.get('P') is not None:
            self.PressLableSZnachU.setText(str(fun.bit_u(float(values_adc['P']))) + ' В')
            self.WLabelS.setText(water_raw + str('л/мин'))

    def connect_buttons(self):
        self.btn_checkPorts.clicked.connect(self.check_ports)
        self.ButtonCancel.clicked.connect(self.close)
        self.btn_read_config.clicked.connect(self.read_config)
        self.btn_save_config.clicked.connect(self.save_config)
        self.btn_checkCurrentAddresses.clicked.connect(self.read_current_addresses)
        self.btn_checkPortRRG.clicked.connect(self.check_port_rrg)
        self.btn_checkPortRF.clicked.connect(self.check_port_rf)

        for i in range(1, 5):
            getattr(self, f'btn_applyNewAddressRRG{i}').clicked.connect(lambda: self.apply_new_address_rrg(i))
            getattr(self, f'btn_applySetAddressRRG{i}').clicked.connect(lambda: self.update_address(i))
            getattr(self, f'btn_applyNewBaudrateRRG{i}').clicked.connect(lambda: self.update_baudrate(i))
            getattr(self, f'btn_checkRRG{i}').clicked.connect(lambda: self.check_port_rrg_full(i))
            getattr(self, f'btn_findRRG{i}').clicked.connect(lambda: self.find_address_rrg(i))

        self.btn_checkRF.clicked.connect(self.check_port_rf_full)
        self.btn_measurement_RF.clicked.connect(self.measurement_rf)
        self.btn_measurement_RRG.clicked.connect(self.measurement_rrg)

        self.btn_findRF.clicked.connect(self.find_address_rf)
        self.btn_check_config.clicked.connect(self.check_config)

        for btn in self.buttons_service:
            btn.clicked.connect(lambda checked, btn=btn: self.handle_commands(btn.objectName()))

    def check_ports(self):
        self.textEdit.setText('')
        self.comboBox_2.clear()
        self.edit_checkRRG1_port.clear()
        self.edit_checkRRG2_port.clear()
        self.edit_checkRRG3_port.clear()
        self.edit_checkRRG4_port.clear()
        self.edit_checkRF_port.clear()

        ports = os.listdir('/dev/serial/by-path')
        
        for port in ports:
            port = '/dev/serial/by-path/' + port
            self.comboBox_2.addItem(port)
            self.edit_checkRRG1_port.addItem(port)
            self.edit_checkRRG2_port.addItem(port)
            self.edit_checkRRG3_port.addItem(port)
            self.edit_checkRRG4_port.addItem(port)
            self.edit_checkRF_port.addItem(port)
            self.textEdit.setText(port + '\n' + self.textEdit.toPlainText())

    def read_config(self):
        self.config_number_gases.setCurrentIndex(self.NUM_GASES_MAP.get(str(settings.get('NUMBER_GASES')), 0))

        self.config_port_RRG.setText(settings.get('PORT_RRG'))
        self.config_baudrate_RRG.setValue(settings.get('BAUDRATE_RRG'))

        for i in range(1, 5):
            getattr(self, f'config_address_rrg{i}').setValue(settings.get(f'ADDRESS_RRG{i}'))
            
        self.config_min_flow.setValue(settings.get('MIN_FLOW_RRG'))
        self.config_max_flow.setValue(settings.get('MAX_FLOW_RRG'))

        self.config_port_RF.setText(settings.get('PORT_RF'))
        self.config_baudrate_RF.setValue(settings.get('BAUDRATE_RF'))
        self.config_address_rf.setValue(settings.get('ADDRESS_RF'))

        self.config_min_power.setValue(settings.get('MIN_POWER_BP'))
        self.config_max_power.setValue(settings.get('MAX_POWER_BP'))
        self.config_type_RF.setCurrentIndex(self.TYPE_RF_MAP.get(settings.get('TYPE_RF')))

        self.config_water.setCurrentIndex(self.ANS_BOOL_MAP.get(settings.get('sensor_water', 'false')))
        self.config_hall.setCurrentIndex(self.ANS_BOOL_MAP.get(settings.get('sensor_door', 'false')))
        self.config_purge.setCurrentIndex(self.ANS_BOOL_MAP.get(settings.get('enable_purge', 'false')))
        self.config_res_pressure.setValue(settings.get('ResPressure'))

                            
    def save_config(self):
        config = {
            'NUMBER_GASES': int(self.config_number_gases.currentText()),

            'PORT_RRG': self.config_port_RRG.text(),
            'BAUDRATE_RRG': int(self.config_baudrate_RRG.value()),

            'ADDRESS_RRG1': self.config_address_rrg1.value(),
            'ADDRESS_RRG2': self.config_address_rrg2.value(),
            'ADDRESS_RRG3': self.config_address_rrg3.value(),
            'ADDRESS_RRG3': self.config_address_rrg4.value(),

            'MIN_FLOW_RRG': self.config_min_flow.value(),
            'MAX_FLOW_RRG': self.config_max_flow.value(),

            'PORT_RF': self.config_port_RF.text(),
            'BAUDRATE_RF': int(self.config_baudrate_RF.value()),
            'ADDRESS_RF': self.config_address_rf.value(),

            'MIN_POWER_BP': self.config_min_power.value(),
            'MAX_POWER_BP': self.config_max_power.value(),
            'TYPE_RF': self.config_type_RF.currentText(),

            'sensor_water': self.ANS_BOOL_REVERSE.get(self.config_water.currentText()),
            'sensor_door': self.ANS_BOOL_REVERSE.get(self.config_hall.currentText()),
            'enable_purge': self.ANS_BOOL_REVERSE.get(self.config_purge.currentText()),
            'ResPressure': self.config_res_pressure.value()
        }

        try:
            settings.update(config)
            save_settings(settings_dict=settings)
            self.textEdit.setText(f'Успешно сохранено!')
        except Exception as e:
            self.textEdit.setText(f'Ошибка при сохраненении: {str(e)}')
            
    def read_current_addresses(self):
        for i in range(1, 5):
            getattr(self, f'label_currentAddressRRG{i}').setText(str(settings.get(f'ADDRESS_RRG{i}', 0)))


    def make_instrument(self, port, baudrate, address, timeout=0.5):
        instrument = minimalmodbus.Instrument(port=port, slaveaddress=address)
        instrument.serial.baudrate = baudrate
        instrument.serial.stopbits = 1
        instrument.serial.bytesize = 8
        instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
        instrument.serial.timeout = timeout
        return instrument

    def check_port_rrg(self):
        self.textEdit.clear()
        success_count = 0
        results = []

        port = settings.get('PORT_RRG')
        baudrate = settings.get('BAUDRATE_RRG')

        for i in range(1, settings.get('NUMBER_GASES') + 1):
            address = settings.get(f'ADDRESS_RRG{i}')
            instrument = None

            try:
                instrument = self.make_instrument(port=port, baudrate=baudrate, address=address)
                value_flow = instrument.read_long(registeraddress=0x0016, functioncode=3, signed=False)
                results.append(f"РРГ{i} (адрес {address}): поток = {value_flow}")
                success_count += 1
            except Exception as e:
                results.append(f"РРГ{i} (адрес {address}): ОШИБКА — {e}")
            finally:
                if instrument:
                    instrument.serial.close()

        for msg in results:
            self.textEdit.append(msg)

        if success_count == settings.get('NUMBER_GASES', 2):
            self.label_checkPortRRG.setText("Все РРГ отвечают")
        elif success_count > 0:
            self.label_checkPortRRG.setText(f"Отвечают {success_count} из {settings.get('NUMBER_GASES', 2)}")
        else:
            self.label_checkPortRRG.setText("Ни один РРГ не отвечает")

    def check_port_rrg_full(self, num):
        instrument = None

        port = getattr(self, f'edit_checkRRG{num}_port').currentText()
        baudrate = int(getattr(self, f'edit_checkRRG{num}_baudrate').currentText())
        address = getattr(self, f'edit_checkRRG{num}_address').value()

        try:
            instrument = self.make_instrument(port=port, baudrate=baudrate, address=address)
            value_flow = instrument.read_long(registeraddress=0x0016, functioncode=3, signed=False)
            self.textEdit.setText(f'Текущий поток: {value_flow}')
        except Exception as e:
            self.textEdit.setText(str(e))
        finally:
            if instrument:
                instrument.serial.close()

    def check_port_rf(self):
        instrument = None
        port = settings.get('PORT_RF', '')
        baudrate = settings.get('BAUDRATE_RF', 19200)
        address = settings.get('ADDRESS_RF', 1)

        if not port:
            self.textEdit.append("Ошибка: порт RF не задан в настройках")
            self.label_checkPortRF.setText("Порт не задан")
            return

        try:
            instrument = self.make_instrument(port=port, baudrate=baudrate, address=address, timeout=5.0)
            status = instrument.read_registers(0x0000, 3)
            
            msg = (f'port: {port}\nbaudrate: {baudrate}\n'
                f'address: {address}\nТекущий статус: {status}')
            self.textEdit.append(msg)  # или self.textEdit.clear(); self.textEdit.setText(msg)
            self.label_checkPortRF.setText(f"Статус: {status}")
            
        except minimalmodbus.NoResponseError as e:
            self.textEdit.append(f"Нет ответа от RF (адрес {address}): {e}")
            self.label_checkPortRF.setText("Нет ответа")
        except Exception as e:
            self.textEdit.append(f"Ошибка при проверке RF: {e}")
            self.label_checkPortRF.setText("Ошибка")
        finally:
            if instrument:
                instrument.serial.close()

    def check_port_rf_full(self):
        instrument = None

        port = getattr(self, f'edit_checkRF_port').currentText()
        baudrate = int(getattr(self, f'edit_checkRF_baudrate').currentText())
        address=getattr(self, f'edit_checkRF_address').value()

        try:
            instrument = self.make_instrument(port=port, baudrate=baudrate, address=address)
            value_power = instrument.read_registers(0x0000, 1, functioncode=3)[0]
            self.textEdit.setText(f'port: {port}\nbaudrate: {baudrate}\naddress: {address}\nТекущая мощность: {value_power}')
        except Exception as e:
            self.textEdit.setText(str(e))
        finally:
            if instrument:
                instrument.serial.close()
            
    def change_reg_rrg(self, port, baudrate, current_address, new_address):
        instrument = None
        try:
            instrument = self.make_instrument(port=port, baudrate=baudrate, address=current_address)
            instrument.write_register(0, new_address, functioncode=6)
            time.sleep(0.5)
            instrument.address = new_address
            instrument.read_register(0, functioncode=3)
            return True, 'Успешно'
        except Exception as e:
            return False, str(e)
        finally:
            if instrument:
                instrument.serial.close()

    def apply_new_address_rrg(self, num):
        port = settings.get('PORT_RRG')
        baudrate = settings.get('BAUDRATE_RRG'),
        current_address = 0xFE
        new_address = getattr(self, f'edit_newAddressRRG{num}').value()

        res, msg = self.change_reg_rrg(port=port, baudrate=baudrate, current_address=current_address, new_address=new_address)

        if res:
            settings[f'ADDRESS_RRG{num}'] = new_address
            save_settings(settings_dict=settings)

            getattr(self, f'label_currentAddressRRG{num}').setText(str(settings.get(f'ADDRESS_RRG{num}', 0)))
            getattr(self, f'edit_checkRRG{num}_address').setValue(settings.get(f'ADDRESS_RRG{num}', 0))
            
        self.textEdit.setText(f'Смена адреса для РРГ {num}: {current_address} -> {new_address}\n{msg}')

    def update_address(self, num):
        port = settings.get('PORT_RRG')
        baudrate = settings.get('BAUDRATE_RRG')

        try:
            current_address = int(getattr(self, f'label_currentAddressRRG{num}').text())
        except Exception as e:
            current_address = settings.get(f'ADDRESS_RRG{num}')

        new_address = getattr(self, f'edit_setAddressRRG{num}').value()

        res, msg = self.change_reg_rrg(port=port, baudrate=baudrate, current_address=current_address, new_address=new_address)
        
        if res:
            settings[f'ADDRESS_RRG{num}'] = getattr(self, f'edit_setAddressRRG{num}').value()
            save_settings(settings_dict=settings)

            self.read_config()
            self.read_current_addresses()
            
        self.textEdit.setText(f'Смена адреса для РРГ {num}: {current_address} -> {new_address}\n{msg}')

    def _change_baudrate(self, num, new_baudrate):
        instrument = None

        port = settings.get('PORT_RRG')
        baudrate = settings.get('BAUDRATE_RRG')
        address = settings.get(f'ADDRESS_RRG{num}')

        try:
            instrument = self.make_instrument(port=port, baudrate=baudrate, address=address)
            instrument.write_register(1, new_baudrate, functioncode=6)
            time.sleep(0.3)
            
            new_baudrate_text = getattr(self, f'edit_newBaudrateRRG{num}').currentText()
            settings['BAUDRATE_RRG'] = int(new_baudrate_text)
            save_settings(settings_dict=settings)
            return True, 'Успешно'
        except Exception as e:
            return False, str(e)
        finally:
            if instrument:
                instrument.serial.close()
        
    def update_baudrate(self, num):
        current_baudrate = settings.get('BAUDRATE_RRG')
        selected_baudrate = getattr(self, f'edit_newBaudrateRRG{num}').currentText()
        modbus_code = self.BAUD_CODE_MAP.get(selected_baudrate)
        
        if modbus_code is None:
            self.textEdit.setText(f'Неизвестная скорость: {selected_baudrate}')
            return
        
        res, msg = self._change_baudrate(num=num, new_baudrate=modbus_code)

        if res:
            self.read_config()
            self.read_current_addresses()

        self.textEdit.setText(f'Смена baudrate для РРГ {num}: {current_baudrate} -> {selected_baudrate}\n{msg}')

    def find_address_rrg(self, num):
        if hasattr(self, 'scanner_thread') and self.scanner_thread and self.scanner_thread.isRunning():
            self.textEdit.append("Сканирование уже выполняется, дождитесь окончания")
            return

        port_combo = getattr(self, f'edit_checkRRG{num}_port')
        port = port_combo.currentText()           # исправлено: берём текст
        baudrate = int(getattr(self, f'edit_checkRRG{num}_baudrate').currentText())

        if not port:
            self.textEdit.append("Ошибка: не выбран порт")
            return


        self.scanner_thread = AddressScannerThread(port, baudrate, num)
        self.scanner_thread.log_message.connect(self.textEdit.append)
        self.scanner_thread.address_found.connect(
            lambda addr: self.on_address_found(num, addr)
        )
        self.scanner_thread.finished.connect(
            lambda success: self.on_scan_finished(num, success)
        )
        self.scanner_thread.start()

    def on_address_found(self, num, address):
        getattr(self, f'find_address_RRG{num}').setText(f'{address} - OK')
        getattr(self, f'edit_checkRRG{num}_address').setValue(address)
        self.textEdit.append(f"✅ Адрес {address} установлен для РРГ{num}")

    def on_scan_finished(self, num, success):
        if not success:
            getattr(self, f'find_address_RRG{num}').setText('x')
            self.textEdit.append(f"❌ Не удалось найти адрес для РРГ{num}")
        self.scanner_thread = None

    def find_address_rf(self):
        if hasattr(self, 'rf_scanner') and self.rf_scanner and self.rf_scanner.isRunning():
            self.textEdit.append("Сканирование уже выполняется")
            return

        port = self.edit_checkRF_port.currentText()
        if not port:
            self.textEdit.append("Ошибка: не выбран порт")
            return

        baudrate = int(self.edit_checkRF_baudrate.currentText())

        self.rf_scanner = RfAddressScannerThread(port, baudrate)
        self.rf_scanner.log_message.connect(self.textEdit.append)
        self.rf_scanner.address_found.connect(self.on_rf_address_found)
        self.rf_scanner.finished.connect(self.on_rf_scan_finished)
        self.rf_scanner.start()

    def on_rf_address_found(self, address):
        self.find_address_RF.setText(f"{address} - OK")
        self.edit_checkRF_address.setValue(address)

    def on_rf_scan_finished(self, success):
        if not success:
            self.find_address_RF.setText("x")
        self.rf_scanner = None

    def check_config(self):
        flag = True
        errors = []
        
        # ---- Проверка РРГ ----
        port_rrg = self.config_port_RRG.text().strip()
        if not port_rrg:
            errors.append("Порт для РРГ не задан")
            flag = False
        else:
            baudrate_rrg = int(self.config_baudrate_RRG.currentText())
            # Проверяем только реальное количество газов
            num_gases = settings.get('NUMBER_GASES', 2)
            addresses_rrg = []
            for i in range(1, num_gases + 1):
                addr = getattr(self, f'config_address_rrg{i}').value()
                addresses_rrg.append(addr)
            
            for address in addresses_rrg:
                instrument = None
                try:
                    instrument = self.make_instrument(port_rrg, baudrate_rrg, address)
                    instrument.read_long(0x0016, 3, signed=False)
                except Exception as e:
                    flag = False
                    errors.append(f'РРГ с адресом {address}: {e}')
                finally:
                    if instrument:
                        instrument.serial.close()
                time.sleep(0.05)  # небольшая пауза между попытками
        
        # ---- Проверка RF ----
        port_rf = self.config_port_RF.text().strip()
        if not port_rf:
            errors.append("Порт для RF не задан")
            flag = False
        else:
            baudrate_rf = int(self.config_baudrate_RF.currentText())
            address_rf = self.config_address_rf.value()
            instrument = None
            try:
                instrument = self.make_instrument(port_rf, baudrate_rf, address_rf)
                instrument.read_registers(0x0000, 1, 3)[0]
            except Exception as e:
                flag = False
                errors.append(f'RF с адресом {address_rf}: {e}')
            finally:
                if instrument:
                    instrument.serial.close()
        
        # ---- Вывод результата ----
        if flag:
            self.textEdit.append("Конфигурация успешная.")
        else:
            self.textEdit.append("Ошибка при проверке конфигурации:")
            for er in errors:
                self.textEdit.append(f"  • {er}")

    def measurement_rrg(self):
        self.textEdit.setText('')
        self.thread = MeasurementRRGThread()
        self.thread.update_text.connect(self.textEdit.append)
        self.thread.finished_signal.connect(lambda: self.textEdit.append("Измерение завершено"))
        self.thread.start()

    def measurement_rf(self):
        self.textEdit.setText('')
        self.thread = MeasurementRFThread()
        self.thread.update_text.connect(self.textEdit.append)
        self.thread.finished_signal.connect(lambda: self.textEdit.append("Измерение завершено"))
        self.thread.start()