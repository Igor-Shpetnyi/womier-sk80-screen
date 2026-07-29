"""
Реєстр пресетів для GUI. Кожен пресет — це функція render(epoch_time) -> PIL.Image (160x90 RGB).

Щоб додати новий пресет: написати функцію render(...) і додати запис у PRESETS.
"""
import datetime
import math
import time
from PIL import Image, ImageDraw, ImageFont

import claude_limits

W, H = 160, 90

_FONT_PATH = 'C:/Windows/Fonts/consola.ttf'
_FONT_PATH_BOLD = 'C:/Windows/Fonts/consolab.ttf'
_font_cache = {}


def _font(path, base_size, scale):
    size = max(1, round(base_size * scale))
    key = (path, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


def _centered_text_image(lines, scale=1):
    """lines: список (текст, font_path, базовий_розмір, колір).
    Рендерить напряму у роздільній здатності W*scale x H*scale (без подальшого
    розмиваючого масштабування) — використовується і для кадру на пристрій (scale=1),
    і для якісного прев'ю в GUI (scale>1)."""
    w, h = round(W * scale), round(H * scale)
    img = Image.new('RGB', (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    boxes = []
    for text, font_path, base_size, color in lines:
        font = _font(font_path, base_size, scale)
        bbox = draw.textbbox((0, 0), text, font=font)
        boxes.append((text, font, color, bbox, bbox[2] - bbox[0], bbox[3] - bbox[1]))

    gap = round(4 * scale)
    total_h = sum(b[5] for b in boxes) + gap * (len(boxes) - 1)
    y = (h - total_h) // 2
    for text, font, color, bbox, tw, th in boxes:
        x = (w - tw) // 2 - bbox[0]
        draw.text((x, y - bbox[1]), text, font=font, fill=color)
        y += th + gap
    return img


# поточні кольори годинника (2 параметри: колір часу, колір дати) — змінюються з GUI
CLOCK_STATE = {
    'time_color': (0, 255, 0),
    'date_color': (0, 200, 255),
}

# готові кольорові варіанти для швидкого вибору (назва, колір часу, колір дати)
CLOCK_COLOR_PRESETS = [
    ('Смарагдовий', (0, 255, 0), (0, 200, 255)),
    ('Білий', (255, 255, 255), (170, 170, 170)),
    ('Бурштиновий', (255, 170, 0), (255, 210, 120)),
    ('Небесний', (60, 180, 255), (255, 255, 255)),
    ('Рожевий', (255, 60, 180), (255, 190, 220)),
]


def render_clock(epoch_time, scale=1):
    t = time.localtime(epoch_time)
    time_str = time.strftime('%H:%M', t)
    date_str = time.strftime('%d.%m.%Y', t)
    return _centered_text_image([
        (time_str, _FONT_PATH, 34, CLOCK_STATE['time_color']),
        (date_str, _FONT_PATH, 16, CLOCK_STATE['date_color']),
    ], scale=scale)


def render_placeholder(epoch_time, scale=1):
    return _centered_text_image([
        ('Незабаром', _FONT_PATH_BOLD, 14, (120, 120, 120)),
    ], scale=scale)


def _severity_color(pct):
    if pct is None:
        return (110, 115, 120)
    if pct < 70:
        return (16, 200, 120)
    if pct < 90:
        return (255, 170, 0)
    return (230, 60, 60)


def _draw_bar(draw, x, y, w, h, pct, color):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=(38, 42, 48))
    if pct:
        fill_w = max(h, round(w * min(pct, 100) / 100))
        draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=h // 2, fill=color)


def _shade(color, delta):
    return tuple(max(0, min(255, c + delta)) for c in color)


_WEEKDAYS_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _parse_resets_at(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return None


def _format_session_reset(resets_at_iso, now_dt):
    """"Resets in 17 min" — короткий підпис під баром сесійного (5h) ліміту,
    в стилі офіційного дашборду usage limits на Claude.ai."""
    dt = _parse_resets_at(resets_at_iso)
    if dt is None:
        return ''
    remaining_min = int((dt - now_dt).total_seconds() // 60)
    if remaining_min <= 0:
        return 'Resets soon'
    if remaining_min < 60:
        return f'Resets in {remaining_min} min'
    h, m = divmod(remaining_min, 60)
    return f'Resets in {h}h {m}m'


def _format_weekly_reset(resets_at_iso, now_dt):
    """"Resets Thu 19:00" — тижневий (7d) ліміт скидається майже завжди в інший
    день, тому день тижня показуємо явно (день + локальний час скидання)."""
    dt = _parse_resets_at(resets_at_iso)
    if dt is None:
        return ''
    local_dt = dt.astimezone() if dt.tzinfo else dt
    return f'Resets {_WEEKDAYS_SHORT[local_dt.weekday()]} {local_dt.strftime("%H:%M")}'


def _draw_mascot(draw, x0, y0, size, eyes_open=True, color=(208, 106, 75),
                  y_shift=0, leg_lifts=(0, 0, 0, 0), arm_shifts=(0, 0)):
    """Маскот Clawd: тіло, 2 руки, 4 ноги, очі. Малює у 16x16-одиничній сітці.
    Пропорції й колір виміряні з реального запису Clawd (clawd.gif): тіло (208,106,75),
    руки на рівні очей, 4 ноги з проміжками, тонка тінь по правому/нижньому краю кожного
    блоку і світла лінія зверху голови — так само, як в оригіналі.
    leg_lifts: наскільки кожна з 4 ніг "піднята" від землі (0 = стоїть на землі).
    arm_shifts: вертикальний зсув лівої/правої руки (для розмахування на бігу)."""
    u = size / 16.0
    y_body = y0 + y_shift
    dark = _shade(color, -60)
    light = (170, 170, 170)
    lw = max(1, round(u * 0.15))

    def block(x1, y1, x2, y2):
        draw.rectangle([x1, y1, x2, y2], fill=color)
        draw.line([x2, y1, x2, y2], fill=dark, width=lw)
        draw.line([x1, y2, x2, y2], fill=dark, width=lw)

    # тіло
    block(x0 + 3 * u, y_body + 3 * u, x0 + 13 * u, y_body + 11 * u)
    draw.line([x0 + 3 * u, y_body + 3 * u, x0 + 13 * u, y_body + 3 * u], fill=light, width=lw)
    # руки — на рівні очей + власний змах (для бігу)
    block(x0 + 0 * u, y_body + 6 * u + arm_shifts[0], x0 + 3 * u, y_body + 8.3 * u + arm_shifts[0])
    block(x0 + 13 * u, y_body + 6 * u + arm_shifts[1], x0 + 16 * u, y_body + 8.3 * u + arm_shifts[1])

    # ноги (4, з проміжками) — верх приєднаний до тіла, низ тягнеться до "землі"
    # (фіксованої лінії y0+14.5u), мінус індивідуальний підйом ноги
    leg_w, gap = 1.4 * u, 0.9 * u
    lx = x0 + 3 * u
    leg_top = y_body + 11 * u
    ground = y0 + 14.5 * u
    for i in range(4):
        bottom = max(leg_top + 1.5 * u, ground - leg_lifts[i])
        block(lx, leg_top, lx + leg_w, bottom)
        lx += leg_w + gap

    eye_color = (15, 15, 15)
    if eyes_open:
        draw.rectangle([x0 + 5 * u, y_body + 4.3 * u, x0 + 6.5 * u, y_body + 6.3 * u], fill=eye_color)
        draw.rectangle([x0 + 9.5 * u, y_body + 4.3 * u, x0 + 11 * u, y_body + 6.3 * u], fill=eye_color)
    else:
        draw.rectangle([x0 + 5 * u, y_body + 5.6 * u, x0 + 6.5 * u, y_body + 6.3 * u], fill=eye_color)
        draw.rectangle([x0 + 9.5 * u, y_body + 5.6 * u, x0 + 11 * u, y_body + 6.3 * u], fill=eye_color)


def render_claude_limits(epoch_time=None, scale=1):
    """Статичний варіант (перший кадр анімації) — для сумісності зі звичайним render()."""
    return render_claude_limits_frames(epoch_time, scale)[0][0]


def render_claude_limits_frames(epoch_time=None, scale=1):
    """Анімований варіант: Clawd біжить (ноги по черзі відриваються від землі, руки
    розгойдуються протифазно, тіло підстрибує в такт кроків) — намальовано векторно,
    тому без спотворень масштабування. Колір фіксований (помаранчевий), бари праворуч
    кольоруються за важливістю. Повертає [(image, delay_ms), ...]."""
    w, h = round(W * scale), round(H * scale)
    state = claude_limits.STATE
    five = state.get('five_hour_pct')
    seven = state.get('seven_day_pct')
    now_dt = datetime.datetime.fromtimestamp(epoch_time or time.time(), tz=datetime.timezone.utc)
    five_caption = _format_session_reset(state.get('five_hour_resets_at'), now_dt)
    seven_caption = _format_weekly_reset(state.get('seven_day_resets_at'), now_dt)

    if five is None and seven is None:
        img = Image.new('RGB', (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        msg = 'Немає даних' if state.get('error') else 'Завантаження...'
        font = _font(_FONT_PATH_BOLD, 13, scale)
        bbox = draw.textbbox((0, 0), msg, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((w - tw) // 2 - bbox[0], (h - th) // 2 - bbox[1]), msg,
                   font=font, fill=(140, 140, 140))
        return [(img, 480)] * 10

    mascot_color = (208, 106, 75)  # точний колір, виміряний з реального Clawd (clawd.gif)

    icon_size = round(36 * scale)
    icon_x = round(6 * scale)
    icon_y = (h - icon_size) // 2

    bar_x = icon_x + icon_size + round(8 * scale)
    label_w = round(16 * scale)
    bar_w = round(56 * scale)
    bar_h = round(12 * scale)
    bar_row_h = round(24 * scale)     # рядок з баром — як і раніше
    caption_row_h = round(11 * scale)  # новий рядок під баром: "Resets in 17 min"
    block_h = bar_row_h + caption_row_h
    label_font = _font(_FONT_PATH_BOLD, 11, scale)
    pct_font = _font(_FONT_PATH, 11, scale)
    caption_font = _font(_FONT_PATH, 9, scale)

    def compose(eyes_open, y_shift, leg_lifts, arm_shifts):
        img = Image.new('RGB', (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        _draw_mascot(draw, icon_x, icon_y, icon_size, eyes_open=eyes_open, color=mascot_color,
                     y_shift=y_shift, leg_lifts=leg_lifts, arm_shifts=arm_shifts)

        rows = [('5h', five, five_caption), ('7d', seven, seven_caption)]
        total_h = block_h * len(rows)
        y0 = (h - total_h) // 2
        for i, (label, pct, caption) in enumerate(rows):
            block_y = y0 + i * block_h
            bar_y = block_y + (bar_row_h - bar_h) // 2
            draw.text((bar_x, bar_y - round(1 * scale)), label, font=label_font, fill=(210, 210, 210))
            bx = bar_x + label_w
            _draw_bar(draw, bx, bar_y, bar_w, bar_h, pct, _severity_color(pct))
            pct_text = f'{int(round(pct))}%' if pct is not None else '—'
            draw.text((bx + bar_w + round(4 * scale), bar_y - round(1 * scale)), pct_text,
                       font=pct_font, fill=(230, 230, 230))
            if caption:
                cap_y = block_y + bar_row_h - round(2 * scale)
                draw.text((bx, cap_y), caption, font=caption_font, fill=(140, 145, 150))
        return img

    # 10-кадровий біговий цикл: ноги по черзі відриваються від землі (по діагоналях),
    # руки розгойдуються протифазно до ніг з того ж боку, тіло підстрибує в такт кроків.
    # Обчислено синусоїдами по фазі кадру — цикл зациклюється плавно (кадр9 -> кадр0).
    n = 10
    leg_amp = 2.6 * scale
    arm_amp = 2.0 * scale
    bob_amp = 1.6 * scale
    frames = []
    for i in range(n):
        phase = 2 * math.pi * i / n
        lift_a = max(0.0, math.sin(phase)) * leg_amp
        lift_b = max(0.0, math.sin(phase + math.pi)) * leg_amp
        leg_lifts = (lift_a, lift_b, lift_a, lift_b)
        arm_l = math.sin(phase * 2) * arm_amp
        arm_r = math.sin(phase * 2 + math.pi) * arm_amp
        y_shift = -abs(math.sin(phase * 2)) * bob_amp
        eyes_open = not (i == n // 2)  # коротке моргання раз за цикл
        img = compose(eyes_open, round(y_shift), leg_lifts, (round(arm_l), round(arm_r)))
        frames.append((img, 90))

    return frames


def render_idle_screen(epoch_time=None, scale=1):
    """Стандартна заставка, яка виводиться на екран клавіатури при натисканні "Стоп"."""
    return _centered_text_image([
        ('WOMIER', _FONT_PATH_BOLD, 26, (16, 185, 129)),
        ('SK80', _FONT_PATH, 18, (140, 145, 150)),
    ], scale=scale)


# name -> {render, sync_to_minute, interval, has_colors}
PRESETS = {
    'Годинник': {
        'render': render_clock,
        'sync_to_minute': True,   # оновлення точно синхронізується з :00 кожної хвилини
        'interval': 60,
        'has_colors': True,       # має панель вибору кольорів у GUI
    },
    'Ліміти Claude': {
        'render': render_claude_limits,
        'render_frames': render_claude_limits_frames,  # анімований варіант (біг Clawd)
        'sync_to_minute': False,
        'interval': 300,   # резервний інтервал, якщо change_key не задано
        'has_colors': False,
        'change_key': claude_limits.get_signature,  # шлемо на клавіатуру лише коли зміниться
    },
    'Незабаром...': {
        'render': render_placeholder,
        'sync_to_minute': False,
        'interval': 3600,
        'has_colors': False,
        'disabled': True,  # видно у списку, але Start недоступний
    },
}
