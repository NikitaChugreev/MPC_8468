import json
import os

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), 'settings.json')
DEFAULT_SETTINGS = {
    "NUMBER_GASES": 2,
    "TYPE_RRG1": "MFC_UT",
    "TYPE_RRG2": "MFC_UT",
    "TYPE_RRG3": "",
    "TYPE_RRG4": "",
    "PORT_RRG": "/dev/serial/by-path/platform-3f980000.usb-usb-0:1.1.2:1.0-port0",
    "ADDRESS_RRG1": 19,
    "ADDRESS_RRG2": 16,
    "ADDRESS_RRG3": 0,
    "ADDRESS_RRG4": 0,
    "BAUDRATE_RRG": 19200,
    "MAX_FLOW_RRG": 30,
    "TYPE_RF": "APEL_M_1_5PDC",
    "PORT_RF": "/dev/serial/by-path/platform-3f980000.usb-usb-0:1.1.3:1.0-port0",
    "ADDRESS_RF": 10,
    "BAUDRATE_RF": 57600,
    "MIN_POWER_BP": 10,
    "MAX_POWER_BP": 1000,
    "ResPressure": 0.05,
    "PRESSURE_LED": 4.434,
    "use_pass_technologist": True,
    "time_venting": 60,
    "time_pump": 20,
    "time_pump_for_service": 0,
    "max_time_pump_for_service": 18000000,
    "time_work": 0,
    "pass_technologist": "1234",
    "pass_service": "5678",
    "coef_rrg1": 1.0,
    "coef_rrg2": 1.0,
    "LANG": 0,
    "enable_sound": True,
    "check_water_flow": True
}

def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            save_settings(DEFAULT_SETTINGS)
            return DEFAULT_SETTINGS.copy()
    except Exception as e:
        print(f"Ошибка загрузки настроек: {e}")
        return DEFAULT_SETTINGS.copy()

def save_settings(settings_dict):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings_dict, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Ошибка сохранения настроек: {e}")
        return False

settings = load_settings()