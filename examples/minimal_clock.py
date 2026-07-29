"""Мінімальний приклад використання протоколу без GUI: живий годинник,
синхронізований з початком кожної хвилини.

Показує, що для власного пресету достатньо: намалювати PIL.Image 160x90 і
викликати дві функції з protocol.py. GUI (womier_gui.py) — лише зручна
надбудова над тим самим конвеєром.

Запуск (з кореня репозиторію, драйвер Womier-SK80 має бути закритий):
    python examples/minimal_clock.py
Зупинка: Ctrl+C.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import protocol
from presets import render_clock


def main():
    print('Синхронізується з початком кожної хвилини. Ctrl+C для зупинки.')
    est_duration = 3.6
    while True:
        next_minute = (int(time.time()) // 60 + 1) * 60
        wait = next_minute - est_duration - time.time()
        if wait > 0:
            time.sleep(wait)

        img = render_clock(next_minute)
        pixel_data = protocol.image_to_rgb565(img)
        try:
            est_duration = protocol.send_pixel_data(pixel_data)
            offset = time.time() - next_minute
            print(f'{time.strftime("%H:%M:%S")} — надіслано за {est_duration:.2f}с, '
                  f'відхилення {offset:+.2f}с від :00')
        except protocol.DeviceUnavailable as e:
            print(f'Пристрій не знайдено: {e}')
            time.sleep(5)


if __name__ == '__main__':
    main()
