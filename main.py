#!/usr/bin/env python3
import os
os.environ['DISPLAY'] = ':0'
os.environ['QT_QPA_PLATFORM'] = 'xcb'

from PyQt5.QtWidgets import QApplication

from windows.main_window import MainWindow

if __name__ == "__main__":
    app = QApplication([])
    
    # Устанавливаем стиль для нажатых кнопок (checkable buttons)
    # Используем бирюзово-голубой цвет, гармонирующий с дизайном (похож на rgb(120, 220, 220) из рецептов, но светлее)
    app.setStyleSheet("""
        QPushButton:checked {
            background-color: rgb(180, 210, 230);
            border: 2px solid rgb(140, 180, 200);
            border-radius: 4px;
        }
    """)
    
    main = MainWindow()
    main.show()
    app.processEvents()
    app.exec()
