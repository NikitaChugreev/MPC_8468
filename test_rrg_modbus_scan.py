#!/usr/bin/env python3
"""
Тест: сканирование Modbus RS-485 для поиска подключённых RRG (масс-расходные контроллеры).
Показывает ID (адреса) устройств, которые отвечают на запрос чтения регистров.

Запуск:
  python3 test_rrg_modbus_scan.py                    # порт и скорость из config/settings.json
  python3 test_rrg_modbus_scan.py /dev/ttyUSB0      # указать порт вручную
  python3 test_rrg_modbus_scan.py COM3 19200        # порт и baudrate (Windows)
"""

import sys
import time

try:
    import minimalmodbus
except ImportError:
    print("Установите minimalmodbus: pip install minimalmodbus")
    sys.exit(1)

# Настройки из проекта (без импорта state_controller, чтобы не тянуть hardware)
def get_settings():
    import json
    import os
    path = os.path.join(os.path.dirname(__file__), 'config', 'settings.json')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'PORT_RRG': '/dev/ttyUSB0',
        'BAUDRATE_RRG': 19200,
    }


def scan_rrg(port, baudrate=19200, timeout=0.5, first=1, last=247):
    """
    Сканирует адреса first..last на порту port с заданной скоростью.
    Возвращает список адресов (id устройств), которые ответили на чтение регистра RRG.
    """
    found = []
    # Регистр, на который отвечает RRG MFC_UT (текущий поток / статус)
    RRG_REG = 0x0016
    RRG_NUM_REGISTERS = 2

    for slave in range(first, last + 1):
        try:
            instrument = minimalmodbus.Instrument(port=port, slaveaddress=slave)
            instrument.serial.baudrate = baudrate
            instrument.serial.bytesize = 8
            instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
            instrument.serial.stopbits = 1
            instrument.serial.timeout = timeout
        except Exception as e:
            print(f"  [Ошибка создания инструмента для адреса {slave}: {e}]")
            continue

        try:
            resp = instrument.read_registers(
                registeraddress=RRG_REG,
                number_of_registers=RRG_NUM_REGISTERS,
                functioncode=3
            )
            found.append(slave)
            print(f"  [OK] Адрес (ID) {slave} ответил: регистр 0x{RRG_REG:04X} = {resp}")
        except Exception as e:
            pass  # устройство не ответило — тихо пропускаем
        finally:
            try:
                instrument.serial.close()
            except Exception:
                pass

        time.sleep(0.05)

    return found


def main():
    settings = get_settings()
    port = settings.get('PORT_RRG', '/dev/ttyUSB0')
    baudrate = int(settings.get('BAUDRATE_RRG', 19200))

    if len(sys.argv) >= 2:
        port = sys.argv[1]
    if len(sys.argv) >= 3:
        baudrate = int(sys.argv[2])

    print("Сканирование RRG по Modbus RS-485")
    print(f"  Порт: {port}")
    print(f"  Скорость: {baudrate}")
    print(f"  Диапазон адресов: 1–247")
    print("---")

    try:
        found = scan_rrg(port, baudrate=baudrate)
    except Exception as e:
        print(f"Ошибка при сканировании: {e}")
        sys.exit(1)

    print("---")
    if found:
        print(f"Найдено устройств RRG: {len(found)}")
        print(f"ID устройств: {found}")
    else:
        print("Устройств не найдено. Проверьте порт, питание и адреса RRG.")


if __name__ == '__main__':
    main()
