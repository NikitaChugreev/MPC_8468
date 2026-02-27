#!/usr/bin/env python3
"""
Диагностический скрипт для проверки датчика воды
Использование: python3 test_water_sensor.py
"""

import smbus2
import time
import sys

# Конфигурация датчика
I2C_BUS = 1
SENSOR_ADDR = 0x38
READ_PINS = [5, 1, 0, 2, 7, 6]
WEIGHTS = [8, 16, 32, 64, 4, 2]


def scan_i2c_bus(bus_id):
    """Сканирует I2C шину и возвращает список найденных устройств"""
    print(f"\n=== Сканирование I2C шины {bus_id} ===")
    found_devices = []
    try:
        bus = smbus2.SMBus(bus_id)
        for addr in range(0x08, 0x78):
            try:
                bus.read_byte(addr)
                found_devices.append(hex(addr))
                print(f"  ✓ Найдено устройство на адресе {hex(addr)}")
            except OSError:
                pass
            except Exception as e:
                pass
        bus.close()
    except Exception as e:
        print(f"  ✗ Ошибка при сканировании: {e}")
        return []
    
    if not found_devices:
        print("  ✗ Устройства не найдены")
    return found_devices


def test_device_access(bus_id, addr):
    """Проверяет доступность устройства на указанном адресе"""
    print(f"\n=== Проверка доступа к устройству {hex(addr)} ===")
    try:
        bus = smbus2.SMBus(bus_id)
        # Пробуем прочитать байт
        value = bus.read_byte(addr)
        bus.close()
        print(f"  ✓ Устройство доступно, прочитано значение: {value} (0b{value:08b})")
        return True
    except OSError as e:
        print(f"  ✗ Устройство недоступно: {e}")
        print(f"    Возможные причины:")
        print(f"    - Устройство не подключено")
        print(f"    - Неправильный адрес")
        print(f"    - Проблемы с I2C шиной")
        return False
    except Exception as e:
        print(f"  ✗ Ошибка: {e}")
        return False


def write_bit(bus, addr, pin, value):
    """Записывает бит в указанный пин"""
    try:
        raw = bus.read_byte(addr)
        if value:
            raw |= (1 << pin)
        else:
            raw &= ~(1 << pin)
        # КРИТИЧЕСКИ ВАЖНО: Убеждаемся, что INPUT пины данных всегда остаются HIGH (pull-up)
        # Это критично для правильного чтения INPUT пинов в PCF8574
        # При записи байта в PCF8574 устанавливаются ВСЕ 8 пинов одновременно
        # Если мы не сохраним INPUT пины в HIGH, они будут сброшены в LOW
        for read_pin in READ_PINS:
            raw |= (1 << read_pin)
        bus.write_byte(addr, raw)
        return True
    except Exception as e:
        print(f"  ✗ Ошибка записи бита {pin}: {e}")
        return False


def read_bit(bus, addr, pin):
    """Читает бит с указанного пина"""
    try:
        raw = bus.read_byte(addr)
        bit_value = 1 if (raw >> pin) & 1 else 0
        return bit_value, raw
    except Exception as e:
        print(f"  ✗ Ошибка чтения бита {pin}: {e}")
        return None, None


def setup_pins_as_inputs(bus_id, addr, input_pins):
    """Настраивает указанные пины как INPUT (с pull-up HIGH)"""
    try:
        bus = smbus2.SMBus(bus_id)
        raw = bus.read_byte(addr)
        
        # Устанавливаем INPUT пины в HIGH (pull-up)
        for pin in input_pins:
            raw |= (1 << pin)
        
        bus.write_byte(addr, raw)
        bus.close()
        return True
    except Exception as e:
        print(f"  ✗ Ошибка настройки пинов: {e}")
        return False


def test_pins_read_write(bus_id, addr):
    """Тестирует чтение и запись пинов"""
    print(f"\n=== Тест чтения/записи пинов ===")
    try:
        bus = smbus2.SMBus(bus_id)
        
        # Инициализация - устанавливаем все пины в HIGH (INPUT с pull-up)
        print(f"  Инициализация: запись 0xFF в устройство (все пины INPUT с pull-up)...")
        bus.write_byte(addr, 0xFF)
        time.sleep(0.1)
        
        # Читаем текущее состояние всех пинов
        raw_value = bus.read_byte(addr)
        print(f"  Прочитано начальное значение: {raw_value} (0b{raw_value:08b})")
        print(f"  Состояние пинов (0-7):")
        for pin in range(8):
            bit_val = 1 if (raw_value >> pin) & 1 else 0
            pin_type = "OUTPUT (управление)" if pin in [3, 4] else "INPUT (чтение)"
            print(f"    Пин {pin}: {bit_val} - {pin_type}")
        
        # Тестируем запись управляющих пинов
        print(f"\n  Тест управляющих пинов (MR и GATE):")
        for pin in [4, 3]:
            print(f"    Пин {pin} (MR)" if pin == 4 else f"    Пин {pin} (GATE):")
            
            # Устанавливаем в LOW
            write_bit(bus, addr, pin, 0)
            time.sleep(0.01)
            bit_val, raw = read_bit(bus, addr, pin)
            print(f"      LOW  -> прочитано: {bit_val}, raw: {raw} (0b{raw:08b})")
            
            # Устанавливаем в HIGH
            write_bit(bus, addr, pin, 1)
            time.sleep(0.01)
            bit_val, raw = read_bit(bus, addr, pin)
            print(f"      HIGH -> прочитано: {bit_val}, raw: {raw} (0b{raw:08b})")
        
        # Тестируем чтение пинов данных
        print(f"\n  Тест чтения пинов данных:")
        raw_value = bus.read_byte(addr)
        for pin in READ_PINS:
            bit_val, _ = read_bit(bus, addr, pin)
            if bit_val is not None:
                print(f"    Пин {pin}: {bit_val}")
        
        bus.close()
        return True
    except Exception as e:
        print(f"  ✗ Ошибка при тестировании пинов: {e}")
        return False


def simulate_sensor_cycle_new(bus_id, addr):
    """Симулирует цикл работы датчика (текущая реализация)"""
    print(f"\n=== Симуляция цикла работы датчика (ТЕКУЩАЯ РЕАЛИЗАЦИЯ) ===")
    WINDOW_MS = 300
    WINDOW_S = WINDOW_MS / 1000.0
    
    try:
        bus = smbus2.SMBus(bus_id)
        
        # Инициализация - все пины в HIGH (INPUT с pull-up для пинов данных)
        print(f"  Инициализация: установка всех пинов в HIGH (INPUT с pull-up)...")
        bus.write_byte(addr, 0xFF)
        time.sleep(0.1)
        
        # Убеждаемся что пины данных (0,1,2,5,6,7) установлены как HIGH (pull-up)
        # Это важно для правильного чтения INPUT пинов в PCF8574
        raw = bus.read_byte(addr)
        for pin in READ_PINS:
            raw |= (1 << pin)  # Устанавливаем INPUT пины в HIGH
        bus.write_byte(addr, raw)
        time.sleep(0.05)
        
        print(f"  Начало цикла измерения (окно: {WINDOW_MS} мс)...")
        
        # MR = HIGH
        write_bit(bus, addr, 4, 1)
        print(f"    MR (pin 4) установлен в HIGH")
        time.sleep(0.01)
        
        # GATE = HIGH (начало измерения)
        write_bit(bus, addr, 3, 1)
        print(f"    GATE (pin 3) установлен в HIGH - начало измерения")
        
        # Ждем окно измерения
        time_start = time.time()
        print(f"    Ожидание {WINDOW_MS} мс...")
        while time.time() - time_start < WINDOW_S:
            time.sleep(0.05)
            elapsed = int((time.time() - time_start) * 1000)
            if elapsed % 100 < 10:  # Показываем каждые 100мс
                raw = bus.read_byte(addr)
                data_pins = [(raw >> pin) & 1 for pin in READ_PINS]
                print(f"      [{elapsed} мс] Состояние пинов данных: {data_pins}")
        
        # Читаем данные ВО ВРЕМЯ измерения (GATE HIGH), в конце окна
        # Это критически важно - датчик выдает данные во время GATE HIGH
        print(f"    Чтение данных ВО ВРЕМЯ измерения (GATE HIGH):")
        raw = bus.read_byte(addr)
        print(f"      Raw значение: {raw} (0b{raw:08b})")
        
        count = 0
        for pin, w in zip(READ_PINS, WEIGHTS):
            bit_raw = 1 if (raw >> pin) & 1 else 0
            # ИНВЕРТИРОВАННАЯ ЛОГИКА: LOW (0) = есть сигнал (1), HIGH (1) = нет сигнала (0)
            bit = 0 if bit_raw else 1
            if bit is not None:
                contribution = bit * w
                count += contribution
                print(f"      Пин {pin} (вес {w:2d}): raw={bit_raw} -> инвертировано={bit} -> вклад: {contribution}")
        
        # Теперь можно опустить GATE
        write_bit(bus, addr, 3, 0)
        print(f"    GATE установлен в LOW - конец измерения")
        
        print(f"\n    Итоговый count: {count}")
        
        if count > 126:
            count = 126
            print(f"    count ограничен до 126")
        
        if count != 0:
            flow = 56.653 * (1000 / count) ** (-0.876)
            print(f"    Поток воды: {flow:.3f} л/мин")
        else:
            flow = 0.0
            print(f"    ⚠️  count = 0 -> поток = 0.0 л/мин")
            print(f"    Возможные причины:")
            print(f"      - Датчик не подключен или неисправен")
            print(f"      - Нет потока воды")
            print(f"      - Все пины данных читаются как 0")
        
        # Сброс MR
        write_bit(bus, addr, 4, 0)
        time.sleep(0.002)
        write_bit(bus, addr, 4, 1)
        print(f"    MR сброшен (LOW -> HIGH)")
        
        bus.close()
        return count, flow
    except Exception as e:
        print(f"  ✗ Ошибка при симуляции: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def simulate_sensor_cycle_old(bus_id, addr):
    """Симулирует цикл работы датчика (СТАРАЯ РЕАЛИЗАЦИЯ - событийная)"""
    print(f"\n=== Симуляция цикла работы датчика (СТАРАЯ РЕАЛИЗАЦИЯ) ===")
    print(f"  Старая реализация использует событийный подход (10 событий по 100мс)")
    
    try:
        bus = smbus2.SMBus(bus_id)
        
        # Инициализация (как в старом коде, строки 3773-3783)
        # Сначала настраиваем все пины как нужно
        print(f"  Инициализация (как в старом коде):")
        print(f"    Настройка пинов данных как INPUT (pull-up HIGH)...")
        
        # Устанавливаем все INPUT пины в HIGH (pull-up)
        raw = 0xFF  # Все пины HIGH
        bus.write_byte(addr, raw)
        time.sleep(0.05)
        
        # Затем установка управляющих сигналов (как в старом коде, строки 3781-3783)
        write_bit(bus, addr, 3, 1)  # p3 (GATE) HIGH
        print(f"    p3 (GATE) = HIGH")
        time.sleep(0.01)
        write_bit(bus, addr, 4, 0)  # p4 (MR) LOW
        print(f"    p4 (MR) = LOW")
        time.sleep(0.01)
        write_bit(bus, addr, 3, 0)  # p3 (GATE) LOW
        print(f"    p3 (GATE) = LOW")
        time.sleep(0.1)
        
        Counter_water = 0
        result_count = 0
        result_flow = 0.0
        
        # Цикл из 10 событий (каждое по 100мс, как в старом коде)
        for iEvent in range(1, 11):
            time.sleep(0.1)  # 100мс между событиями
            
            if iEvent == 1:
                # GATE = HIGH
                write_bit(bus, addr, 3, 1)
                print(f"  [Событие {iEvent}] GATE (p3) = HIGH")
                
            elif iEvent == 2:
                # GATE = LOW
                write_bit(bus, addr, 3, 0)
                print(f"  [Событие {iEvent}] GATE (p3) = LOW")
                
            elif iEvent == 3:
                # MR = HIGH, затем MR = LOW
                write_bit(bus, addr, 4, 1)
                print(f"  [Событие {iEvent}] MR (p4) = HIGH")
                time.sleep(0.01)
                write_bit(bus, addr, 4, 0)
                print(f"              MR (p4) = LOW")
                
            elif iEvent >= 4 and iEvent <= 9:
                # Чтение пинов: 4->p5, 5->p1, 6->p0, 7->p2, 8->p7, 9->p6
                pin_map = {4: 5, 5: 1, 6: 0, 7: 2, 8: 7, 9: 6}
                weight_map = {4: 8, 5: 16, 6: 32, 7: 64, 8: 4, 9: 2}
                
                pin = pin_map[iEvent]
                weight = weight_map[iEvent]
                bit, raw = read_bit(bus, addr, pin)
                
                if bit is not None:
                    if iEvent == 4:
                        Counter_water = weight * bit
                    else:
                        Counter_water += weight * bit
                    print(f"  [Событие {iEvent}] Чтение p{pin} (вес {weight}): {bit} -> count={Counter_water}")
                else:
                    print(f"  [Событие {iEvent}] Ошибка чтения p{pin}")
                    
            elif iEvent == 10:
                # Обработка данных
                print(f"  [Событие {iEvent}] Обработка данных")
                
                final_count = Counter_water
                if final_count > 126:
                    final_count = 126
                    print(f"              count ограничен до 126")
                
                if final_count != 0:
                    flow = round((56.653) * (1000 / final_count) ** (-0.876), 3)
                    print(f"              count={final_count}, поток={flow:.3f} л/мин")
                else:
                    flow = 0.0
                    print(f"              ⚠️  count=0 -> поток=0.0 л/мин")
                
                # Сброс для следующего цикла (но сохраняем результат)
                result_count = final_count
                result_flow = flow
                Counter_water = 0
        
        bus.close()
        return result_count, result_flow
        
    except Exception as e:
        print(f"  ✗ Ошибка при симуляции: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def simulate_sensor_cycle(bus_id, addr):
    """Симулирует цикл работы датчика (по умолчанию новая реализация)"""
    return simulate_sensor_cycle_new(bus_id, addr)


def continuous_monitoring(bus_id, addr, duration=30):
    """Непрерывный мониторинг датчика в течение указанного времени"""
    print(f"\n=== Непрерывный мониторинг ({duration} секунд) ===")
    print(f"  Нажмите Ctrl+C для остановки")
    print(f"  ⚠️  Убедитесь что есть поток воды через датчик!")
    
    WINDOW_MS = 300
    WINDOW_S = WINDOW_MS / 1000.0
    
    try:
        bus = smbus2.SMBus(bus_id)
        # Инициализация: все INPUT пины в HIGH (pull-up)
        bus.write_byte(addr, 0xFF)
        time.sleep(0.1)
        
        # Убеждаемся что INPUT пины данных установлены в HIGH
        raw = bus.read_byte(addr)
        for pin in READ_PINS:
            raw |= (1 << pin)
        bus.write_byte(addr, raw)
        time.sleep(0.05)
        
        measurements = []
        start_time = time.time()
        
        try:
            while time.time() - start_time < duration:
                # Перед каждым циклом убеждаемся что INPUT пины установлены в HIGH
                raw = bus.read_byte(addr)
                for pin in READ_PINS:
                    raw |= (1 << pin)
                bus.write_byte(addr, raw)
                time.sleep(0.01)
                
                # Цикл измерения
                write_bit(bus, addr, 4, 1)  # MR = HIGH
                write_bit(bus, addr, 3, 1)  # GATE = HIGH
                
                time_start = time.time()
                # Ждем почти до конца окна, затем читаем ВО ВРЕМЯ GATE HIGH
                while time.time() - time_start < (WINDOW_S - 0.05):
                    time.sleep(0.005)
                
                # Читаем данные ВО ВРЕМЯ измерения (GATE HIGH), в конце окна
                raw = bus.read_byte(addr)
                count = 0
                
                for pin, w in zip(READ_PINS, WEIGHTS):
                    bit_raw = 1 if (raw >> pin) & 1 else 0
                    # ИНВЕРТИРОВАННАЯ ЛОГИКА: LOW (0) = есть сигнал (1), HIGH (1) = нет сигнала (0)
                    bit = 0 if bit_raw else 1
                    count += bit * w
                
                # Теперь опускаем GATE
                write_bit(bus, addr, 3, 0)  # GATE = LOW
                
                if count > 126:
                    count = 126
                
                if count != 0:
                    flow = 56.653 * (1000 / count) ** (-0.876)
                else:
                    flow = 0.0
                
                measurements.append((count, flow))
                elapsed = time.time() - start_time
                
                print(f"  [{elapsed:6.1f}с] count={count:3d}, flow={flow:7.3f} л/мин, raw=0b{raw:08b}")
                
                write_bit(bus, addr, 4, 0)
                time.sleep(0.002)
                write_bit(bus, addr, 4, 1)
                
                time.sleep(0.1)
        
        except KeyboardInterrupt:
            print(f"\n  Остановлено пользователем")
        
        bus.close()
        
        # Статистика
        if measurements:
            counts = [m[0] for m in measurements]
            flows = [m[1] for m in measurements]
            non_zero = [f for f in flows if f > 0]
            
            print(f"\n  Статистика:")
            print(f"    Всего измерений: {len(measurements)}")
            print(f"    Измерений с потоком > 0: {len(non_zero)}")
            if non_zero:
                print(f"    Мин. поток: {min(non_zero):.3f} л/мин")
                print(f"    Макс. поток: {max(non_zero):.3f} л/мин")
                print(f"    Средний поток: {sum(non_zero)/len(non_zero):.3f} л/мин")
            else:
                print(f"    ⚠️  Все измерения показывают поток = 0")
                print(f"    Средний count: {sum(counts)/len(counts):.1f}")
        
    except Exception as e:
        print(f"  ✗ Ошибка при мониторинге: {e}")
        import traceback
        traceback.print_exc()


def test_inverted_logic(bus_id, addr):
    """Тест с инвертированной логикой - возможно пины LOW означают наличие сигнала"""
    print(f"\n=== ТЕСТ ИНВЕРТИРОВАННОЙ ЛОГИКИ ===")
    print(f"  Проверка: может быть LOW = есть сигнал?")
    print(f"  ⚠️  Если датчик замыкает пины на землю при наличии сигнала, то 0 = есть поток!")
    
    WINDOW_MS = 300
    WINDOW_S = WINDOW_MS / 1000.0
    
    try:
        bus = smbus2.SMBus(bus_id)
        
        # Инициализация
        bus.write_byte(addr, 0xFF)
        time.sleep(0.1)
        raw = bus.read_byte(addr)
        print(f"  После записи 0xFF прочитано: {raw} (0b{raw:08b})")
        # Важно: если датчик замыкает на землю, мы НЕ должны принудительно устанавливать INPUT пины
        # Пробуем оставить их как есть
        bus.write_byte(addr, raw)  # Записываем то что прочитали
        time.sleep(0.05)
        
        print(f"  Начало цикла измерения...")
        write_bit(bus, addr, 4, 1)  # MR = HIGH
        write_bit(bus, addr, 3, 1)  # GATE = HIGH
        
        # Читаем ВО ВРЕМЯ измерения (GATE HIGH)
        print(f"  Чтение ВО ВРЕМЯ измерения (GATE HIGH):")
        readings_during = []
        time_start = time.time()
        while time.time() - time_start < WINDOW_S:
            time.sleep(0.05)
            raw = bus.read_byte(addr)
            readings_during.append(raw)
            if len(readings_during) <= 3:
                elapsed = int((time.time() - time_start) * 1000)
                data_pins = [(raw >> pin) & 1 for pin in READ_PINS]
                print(f"    [{elapsed} мс] Raw=0b{raw:08b}, пины данных: {data_pins}")
        
        write_bit(bus, addr, 3, 0)  # GATE = LOW
        time.sleep(0.01)
        
        # Читаем ПОСЛЕ измерения
        print(f"  Чтение ПОСЛЕ измерения (GATE LOW):")
        raw_after = bus.read_byte(addr)
        data_pins_after = [(raw_after >> pin) & 1 for pin in READ_PINS]
        print(f"    Raw=0b{raw_after:08b}, пины данных: {data_pins_after}")
        
        # Анализ: находим уникальные значения во время измерения
        unique_during = set(readings_during)
        print(f"\n  Анализ:")
        print(f"    Уникальных значений во время измерения: {len(unique_during)}")
        
        best_count_normal = 0
        best_count_inverted = 0
        best_raw = None
        
        # Проверяем все уникальные значения
        for raw_val in unique_during:
            # Обычное чтение
            count_n = 0
            for pin, w in zip(READ_PINS, WEIGHTS):
                bit = 1 if (raw_val >> pin) & 1 else 0
                count_n += bit * w
            
            # Инвертированное чтение (LOW = 1)
            count_inv = 0
            for pin, w in zip(READ_PINS, WEIGHTS):
                bit = 1 if (raw_val >> pin) & 1 else 0
                bit_inv = 0 if bit else 1  # Инверсия: LOW = есть сигнал
                count_inv += bit_inv * w
            
            if count_n > best_count_normal:
                best_count_normal = count_n
                best_raw = raw_val
            if count_inv > best_count_inverted:
                best_count_inverted = count_inv
                best_raw = raw_val
            
            if count_n > 0 or count_inv > 0:
                print(f"    Raw=0b{raw_val:08b}: count_норм={count_n}, count_инв={count_inv}")
        
        # Проверяем значение после GATE LOW
        count_after_n = 0
        count_after_inv = 0
        for pin, w in zip(READ_PINS, WEIGHTS):
            bit = 1 if (raw_after >> pin) & 1 else 0
            count_after_n += bit * w
            count_after_inv += (0 if bit else 1) * w
        
        print(f"\n  Результаты:")
        print(f"    Лучший count (нормальный) во время: {best_count_normal}")
        print(f"    Лучший count (инвертированный) во время: {best_count_inverted}")
        print(f"    Count (нормальный) после GATE LOW: {count_after_n}")
        print(f"    Count (инвертированный) после GATE LOW: {count_after_inv}")
        
        # Выбираем лучший вариант
        all_counts = [
            (best_count_normal, "норм_во_время"),
            (best_count_inverted, "инв_во_время"),
            (count_after_n, "норм_после"),
            (count_after_inv, "инв_после")
        ]
        
        best = max(all_counts, key=lambda x: x[0])
        
        if best[0] > 0 and best[0] <= 126:
            flow = 56.653 * (1000 / best[0]) ** (-0.876)
            print(f"\n  ✓ НАЙДЕН ПОТОК! ({best[1]}): count={best[0]}, flow={flow:.3f} л/мин")
            if "инв" in best[1]:
                print(f"  ⚠️  ВНИМАНИЕ: Используется инвертированная логика!")
                print(f"     Датчик использует активный LOW сигнал (LOW = есть поток)")
                print(f"     Нужно инвертировать чтение в коде!")
            if "во_время" in best[1]:
                print(f"  ⚠️  ВНИМАНИЕ: Нужно читать ВО ВРЕМЯ GATE HIGH, а не после!")
        else:
            print(f"\n  ✗ Поток не обнаружен во всех вариантах")
            print(f"    Возможные причины:")
            print(f"    - Нет потока воды через датчик")
            print(f"    - Датчик не подключен к PCF8574")
            print(f"    - Неправильная последовательность управления")
        
        write_bit(bus, addr, 4, 0)
        time.sleep(0.002)
        write_bit(bus, addr, 4, 1)
        
        bus.close()
        
    except Exception as e:
        print(f"  ✗ Ошибка: {e}")
        import traceback
        traceback.print_exc()


def test_alternative_sequences(bus_id, addr):
    """Тестирует альтернативные последовательности управления"""
    print(f"\n=== ТЕСТ АЛЬТЕРНАТИВНЫХ ПОСЛЕДОВАТЕЛЬНОСТЕЙ ===")
    print(f"  Проверка различных способов управления датчиком")
    
    sequences = [
        {
            "name": "Последовательность 1: MR->GATE->чтение",
            "steps": [
                ("MR HIGH", lambda b: write_bit(b, addr, 4, 1)),
                ("GATE HIGH", lambda b: write_bit(b, addr, 3, 1)),
                ("wait 300ms", lambda b: time.sleep(0.3)),
                ("GATE LOW", lambda b: write_bit(b, addr, 3, 0)),
                ("read", None),
            ]
        },
        {
            "name": "Последовательность 2: GATE->MR->чтение",
            "steps": [
                ("GATE HIGH", lambda b: write_bit(b, addr, 3, 1)),
                ("MR HIGH", lambda b: write_bit(b, addr, 4, 1)),
                ("wait 300ms", lambda b: time.sleep(0.3)),
                ("GATE LOW", lambda b: write_bit(b, addr, 3, 0)),
                ("read", None),
            ]
        },
        {
            "name": "Последовательность 3: Сброс MR перед чтением",
            "steps": [
                ("MR HIGH", lambda b: write_bit(b, addr, 4, 1)),
                ("GATE HIGH", lambda b: write_bit(b, addr, 3, 1)),
                ("wait 300ms", lambda b: time.sleep(0.3)),
                ("GATE LOW", lambda b: write_bit(b, addr, 3, 0)),
                ("MR LOW", lambda b: write_bit(b, addr, 4, 0)),
                ("wait 2ms", lambda b: time.sleep(0.002)),
                ("MR HIGH", lambda b: write_bit(b, addr, 4, 1)),
                ("read", None),
            ]
        },
    ]
    
    try:
        for seq_idx, seq in enumerate(sequences, 1):
            print(f"\n  Тест {seq_idx}: {seq['name']}")
            bus = smbus2.SMBus(bus_id)
            
            # Инициализация
            bus.write_byte(addr, 0xFF)
            time.sleep(0.05)
            raw = bus.read_byte(addr)
            for pin in READ_PINS:
                raw |= (1 << pin)
            bus.write_byte(addr, raw)
            time.sleep(0.05)
            
            # Выполняем последовательность
            for step_name, step_func in seq['steps']:
                if step_func:
                    if "wait" in step_name.lower():
                        step_func(bus)
                    else:
                        step_func(bus)
                        time.sleep(0.01)
                else:
                    # Чтение
                    raw = bus.read_byte(addr)
                    count = 0
                    pin_values = []
                    
                    for pin, w in zip(READ_PINS, WEIGHTS):
                        bit = 1 if (raw >> pin) & 1 else 0
                        pin_values.append(f"p{pin}={bit}")
                        count += bit * w
                    
                    if count > 126:
                        count = 126
                    
                    if count != 0:
                        flow = 56.653 * (1000 / count) ** (-0.876)
                        print(f"    ✓ Raw=0b{raw:08b}, pins={', '.join(pin_values)}, count={count}, flow={flow:.3f} л/мин")
                    else:
                        print(f"    ✗ Raw=0b{raw:08b}, pins={', '.join(pin_values)}, count=0")
            
            bus.close()
            time.sleep(0.2)
            
    except Exception as e:
        print(f"  ✗ Ошибка: {e}")
        import traceback
        traceback.print_exc()


def main():
    print("=" * 60)
    print("ДИАГНОСТИКА ДАТЧИКА ВОДЫ")
    print("=" * 60)
    print(f"Конфигурация:")
    print(f"  I2C шина: {I2C_BUS}")
    print(f"  Адрес устройства: {hex(SENSOR_ADDR)}")
    print(f"  Пины для чтения: {READ_PINS}")
    print(f"  Веса: {WEIGHTS}")
    
    # 1. Сканирование I2C шины
    found_devices = scan_i2c_bus(I2C_BUS)
    
    # 2. Проверка доступности устройства
    if hex(SENSOR_ADDR) not in found_devices:
        print(f"\n⚠️  ВНИМАНИЕ: Устройство {hex(SENSOR_ADDR)} не найдено при сканировании!")
        print(f"  Продолжаем тестирование в любом случае...")
    
    device_accessible = test_device_access(I2C_BUS, SENSOR_ADDR)
    
    if not device_accessible:
        print(f"\n❌ Устройство недоступно. Проверьте:")
        print(f"  1. Подключен ли PCF8574 к I2C шине")
        print(f"  2. Правильный ли адрес (должен быть {hex(SENSOR_ADDR)})")
        print(f"  3. Включен ли I2C в системе (sudo raspi-config)")
        print(f"  4. Правильно ли подключены провода (SDA, SCL, VCC, GND)")
        return
    
    # 3. Тест чтения/записи пинов
    pins_ok = test_pins_read_write(I2C_BUS, SENSOR_ADDR)
    
    # 4. Тест инвертированной логики
    print(f"\n" + "="*60)
    response = input("\nПротестировать инвертированную логику? (y/n, по умолчанию y): ").strip().lower()
    if response != 'n':
        test_inverted_logic(I2C_BUS, SENSOR_ADDR)
    
    # 5. Тест альтернативных последовательностей
    print(f"\n" + "="*60)
    response = input("\nПротестировать альтернативные последовательности управления? (y/n, по умолчанию y): ").strip().lower()
    if response != 'n':
        test_alternative_sequences(I2C_BUS, SENSOR_ADDR)
    
    # 6. Симуляция одного цикла измерения (ТЕКУЩАЯ реализация)
    count, flow = simulate_sensor_cycle_new(I2C_BUS, SENSOR_ADDR)
    
    # 7. Симуляция СТАРОЙ реализации для сравнения
    print(f"\n" + "="*60)
    response = input("\nПротестировать СТАРУЮ реализацию для сравнения? (y/n, по умолчанию y): ").strip().lower()
    if response != 'n':
        count_old, flow_old = simulate_sensor_cycle_old(I2C_BUS, SENSOR_ADDR)
        print(f"\n" + "="*60)
        print(f"СРАВНЕНИЕ РЕЗУЛЬТАТОВ:")
        print(f"  Текущая реализация: count={count}, flow={flow:.3f} л/мин")
        print(f"  Старая реализация:  count={count_old}, flow={flow_old:.3f} л/мин")
        if count == 0 and count_old != 0:
            print(f"\n  ⚠️  Различие! Старая реализация работает, текущая - нет!")
        elif count != 0 and count_old == 0:
            print(f"\n  ⚠️  Различие! Текущая реализация работает, старая - нет!")
        elif count == 0 and count_old == 0:
            print(f"\n  ⚠️  Обе реализации показывают 0 - проблема может быть в датчике/подключении")
        else:
            print(f"\n  ✓ Обе реализации работают")
    
    # 8. Предложение непрерывного мониторинга
    if count == 0:
        print(f"\n⚠️  Датчик показывает count=0 (поток=0)")
        response = input("\nЗапустить непрерывный мониторинг? (y/n, по умолчанию y): ").strip().lower()
        if response != 'n':
            duration = input("Длительность мониторинга в секундах (по умолчанию 30): ").strip()
            try:
                duration = int(duration) if duration else 30
            except:
                duration = 30
            continuous_monitoring(I2C_BUS, SENSOR_ADDR, duration)
    
    print("\n" + "=" * 60)
    print("Диагностика завершена")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

