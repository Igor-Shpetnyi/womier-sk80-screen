"""
Низькорівневий протокол екрана Womier SK80 (реверс-інжиніринг, див. PROTOCOL_REVERSE_ENGINEERING.md).

Формат: RGB565 little-endian, 160x90, row-major.
Кадр на дроті: заголовок (1218 байт, фіксований для 3-кадрової структури) +
3 x (28800 байт контенту + 1920 байт padding) = 94208 байт, порціями по 4096 байт.
"""
import base64
import struct
import time

import hid
from PIL import Image

REAL_W, REAL_H = 160, 90
FRAME_STRIDE = 30720
HEADER_LEN = 1218
CONTENT_BYTES = REAL_W * REAL_H * 2  # 28800
N_FRAMES = 3
CHUNK_SIZE = 4096

VID, PID = 0x05AC, 0x024F
IFACE_DATA = 2   # MI_02: endpoint 0x03 OUT / 0x84 IN — сирі пікселі
IFACE_CMD = 3    # MI_03: feature reports (wIndex=3) — start/params/commit

CHUNK_DELAY_S = 0.07  # безпечна затримка між порціями (перевірено стабільно)

# --- вбудовані байти заголовка й команд (взяті з capture_rgb.pcapng / test_red.gif) ---
_HEADER_B64 = (
    b'AzIyMv///////////////////////////////////////////////////////////////////////////////'
    b'/////////////////////////////////////////////////////////////////////////////////////'
    b'/////////////////////////////////////////////////////////////////////////////////////'
    b'/////////////////////////////////////////////////////////////////////////////////////'
    b'/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAA'
)
_CMD_START_B64 = b'BBgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=='
_CMD_PARAMS_B64 = b'BHIGAAAAAAAXAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=='
_CMD_COMMIT_B64 = b'BAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=='

# --- підтверджений реальним захопленням шаблон для 10-кадрової анімації (capture_10frames.pcapng) ---
_HEADER10_B64 = (
    b'Cvr6+vr6+vr6+vr/////////////////////////////////////////////////////////////////////'
    b'////////////////////////////////////////////////////////////////////////////////////'
    b'////////////////////////////////////////////////////////////////////////////////////'
    b'////////////////////////////////////////////////////////////////////////////////////'
    b'/////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    b'AAAAAAAAAAAAAAAAAAAAAAAAAAAA'
)
_CMD_PARAMS10_B64 = b'BHIGAAAAAABMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=='


def _load_header_from_file_fallback():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, 'ep3_rgb_stream.bin')
    if os.path.exists(path):
        return open(path, 'rb').read()[:HEADER_LEN]
    raise RuntimeError('no embedded header and no ep3_rgb_stream.bin found')


try:
    HEADER = base64.b64decode(_HEADER_B64)
    assert len(HEADER) == HEADER_LEN
except Exception:
    HEADER = _load_header_from_file_fallback()

CMD_START = base64.b64decode(_CMD_START_B64)
CMD_PARAMS = base64.b64decode(_CMD_PARAMS_B64)
CMD_COMMIT = base64.b64decode(_CMD_COMMIT_B64)

HEADER10 = base64.b64decode(_HEADER10_B64)
CMD_PARAMS10 = base64.b64decode(_CMD_PARAMS10_B64)
assert len(HEADER10) == HEADER_LEN

# зареєстровані ПІДТВЕРДЖЕНІ реальним трафіком шаблони (заголовок, params) для кожної
# кількості кадрів. Не додавати нові значення "навмання" — лише на основі реального
# захоплення (див. PROTOCOL_REVERSE_ENGINEERING.md, розділ про params-байт).
_TEMPLATES = {
    3: (HEADER, CMD_PARAMS),
    10: (HEADER10, CMD_PARAMS10),
}

# padding-хвіст кожного кадрового слоту (1920 байт, ідентичний для всіх спостережених кадрів)
_FRAME_PADDING = b'\x00' * (FRAME_STRIDE - CONTENT_BYTES)


def image_to_rgb565(img: Image.Image) -> bytes:
    """160x90 PIL.Image (RGB) -> сирі байти RGB565 little-endian, row-major."""
    import numpy as np
    if img.size != (REAL_W, REAL_H):
        img = img.resize((REAL_W, REAL_H), Image.LANCZOS)
    img = img.convert('RGB')
    arr = np.array(img).astype(np.uint32)
    r = arr[:, :, 0]; g = arr[:, :, 1]; b = arr[:, :, 2]
    val = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return val.astype('<u2').tobytes()


def build_stream(pixel_data: bytes) -> bytes:
    """Складає повний стрім (заголовок + 3x однаковий кадр) з готових пікселів кадру."""
    assert len(pixel_data) == CONTENT_BYTES
    frame = pixel_data + _FRAME_PADDING
    return HEADER + frame * N_FRAMES


def build_stream_multi(frame_pixels, frame_delays_ms):
    """Як build_stream, але кожен кадр може мати свій вміст і свою затримку
    (мс, макс ~510мс через 8-бітне поле — див. PROTOCOL_REVERSE_ENGINEERING.md).
    Кількість кадрів має відповідати одному з ПІДТВЕРДЖЕНИХ шаблонів у _TEMPLATES
    (зараз 3 або 10) — інші кількості не гарантовано стабільні."""
    n = len(frame_pixels)
    assert n == len(frame_delays_ms)
    if n not in _TEMPLATES:
        raise ValueError(
            f'{n} кадрів не підтверджено реальним трафіком; є лише {sorted(_TEMPLATES)}'
        )
    header_template, _ = _TEMPLATES[n]
    header = bytearray(header_template)
    header[0] = n
    for i, ms in enumerate(frame_delays_ms):
        header[1 + i] = (int(ms) // 2) % 256
    body = b''.join(p + _FRAME_PADDING for p in frame_pixels)
    return bytes(header) + body


def find_path(iface):
    for d in hid.enumerate(VID, PID):
        if d['interface_number'] == iface:
            return d['path']
    return None


def driver_process_running():
    """Перевіряє, чи запущений фірмовий DeviceDriver.exe (блокує HID-доступ)."""
    import subprocess
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-Process -Name DeviceDriver -ErrorAction SilentlyContinue | Select-Object -First 1 Id"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


class DeviceUnavailable(Exception):
    pass


def send_pixel_data(pixel_data: bytes, chunk_delay=CHUNK_DELAY_S, stop_check=None):
    """Повний цикл: start -> params -> дані -> commit. Повертає час виконання (сек)."""
    return _send_stream(build_stream(pixel_data), CMD_PARAMS, chunk_delay=chunk_delay, stop_check=stop_check)


def send_frames(frame_pixels, frame_delays_ms, chunk_delay=CHUNK_DELAY_S, stop_check=None):
    """Як send_pixel_data, але кожен кадр може мати свій вміст і затримку —
    для простих анімацій (наприклад моргання маскота) в межах одного пуша."""
    n = len(frame_pixels)
    stream = build_stream_multi(frame_pixels, frame_delays_ms)
    _, params_template = _TEMPLATES[n]
    return _send_stream(stream, params_template, chunk_delay=chunk_delay, stop_check=stop_check)


def _send_stream(stream: bytes, params_cmd, chunk_delay=CHUNK_DELAY_S, stop_check=None):
    path2 = find_path(IFACE_DATA)
    path3 = find_path(IFACE_CMD)
    if path2 is None or path3 is None:
        raise DeviceUnavailable('Клавіатуру не знайдено (VID_05AC&PID_024F, інтерфейси 2/3)')

    chunks = [stream[i:i + CHUNK_SIZE] for i in range(0, len(stream), CHUNK_SIZE)]

    dev2 = hid.device(); dev2.open_path(path2)
    dev3 = hid.device(); dev3.open_path(path3)
    t0 = time.time()
    try:
        _send_cmd(dev3, CMD_START)
        _send_cmd(dev3, params_cmd)
        dev2.set_nonblocking(True)
        for chunk in chunks:
            if stop_check and stop_check():
                break
            buf = chunk if len(chunk) == CHUNK_SIZE else chunk + b'\x00' * (CHUNK_SIZE - len(chunk))
            dev2.write(b'\x00' + buf)
            time.sleep(chunk_delay)
            dev2.read(64, timeout_ms=50)
        _send_cmd(dev3, CMD_COMMIT)
    finally:
        dev2.close()
        dev3.close()
    return time.time() - t0


def _send_cmd(dev, data):
    dev.send_feature_report(b'\x00' + data)
    time.sleep(0.05)
    dev.get_feature_report(0, 65)
    time.sleep(0.05)
