import json
import os
import sys
import threading
import queue
import time
from tkinter import colorchooser

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

import protocol
import claude_limits
from presets import PRESETS, CLOCK_STATE, CLOCK_COLOR_PRESETS, render_idle_screen, claude_limits_summary

CLAUDE_LIMITS_REFRESH_S = 300  # 5 хв


def _bundled_dir():
    """Директорія з "тільки для читання" ресурсами (іконка): при звичайному запуску —
    поруч зі скриптом, у PyInstaller onefile-збірці — тимчасова директорія розпакування."""
    return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def _persistent_dir():
    """Директорія для файлів, які мають зберігатися між запусками (gui_state.json).
    %APPDATA%\\WomierSK80 — стандартне місце для даних застосунку у Windows: не
    залежить від того, звідки запущено .exe чи скрипт, і не смітить у ту саму
    директорію, де лежить сам .exe."""
    base = os.getenv('APPDATA') or os.path.expanduser('~')
    d = os.path.join(base, 'WomierSK80')
    os.makedirs(d, exist_ok=True)
    return d


STATE_FILE = os.path.join(_persistent_dir(), 'gui_state.json')


def _load_app_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_app_state(preset_name, running):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'preset': preset_name, 'running': running}, f)
    except Exception:
        pass

ctk.set_appearance_mode('dark')
ctk.set_default_color_theme('dark-blue')

ACCENT = '#10B981'
ACCENT_HOVER = '#0EA271'
DANGER = '#DC2626'
DANGER_HOVER = '#B91C1C'
SIDEBAR_BG = '#15181D'
CARD_BG = '#1E2228'
CARD_BORDER = '#2A2F37'
PREVIEW_SCALE = 3  # 160x90 -> 480x270, рендериться напряму у цій роздільній здатності (без розмиття)
PREVIEW_TICK_MS = 1000
SWATCH_SIZE = (48, 30)


def _rgb_hex(rgb):
    return '#%02x%02x%02x' % rgb


def _make_swatch_image(time_color, date_color):
    w, h = SWATCH_SIZE
    img = Image.new('RGB', (w, h), (10, 10, 12))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, int(h * 0.6)], fill=time_color)
    draw.rectangle([0, int(h * 0.6), w, h], fill=date_color)
    return img


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title('Womier SK80 — Керування екраном')
        self.geometry('920x820')
        self.minsize(760, 480)
        icon_path = os.path.join(_bundled_dir(), 'assets', 'app_icon.ico')
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        self.log_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.selected_preset = list(PRESETS.keys())[0]
        self.preset_buttons = {}
        self.swatch_buttons = []
        self.preview_frames = None
        self.preview_frame_idx = 0
        # спільні між фоновим циклом і кнопкою "Перевірити зараз", щоб не слати
        # на клавіатуру один і той самий кадр двічі й не зіткнутися на HID одночасно
        self.hid_lock = threading.Lock()
        self.last_sent_key = None
        self.claude_limits_error_streak = 0

        saved_state = _load_app_state()
        saved_preset = saved_state.get('preset')
        pending_autostart = False
        if saved_preset in PRESETS and not PRESETS[saved_preset].get('disabled'):
            self.selected_preset = saved_preset
            pending_autostart = bool(saved_state.get('running'))

        self._build_layout()
        self.protocol('WM_DELETE_WINDOW', self._hide_to_tray)
        self._select_preset(self.selected_preset)
        self.after(150, self._drain_log_queue)
        if pending_autostart:
            self.after(300, self.on_start)
        self.after(PREVIEW_TICK_MS, self._tick_preview)

        self.claude_limits_stop = threading.Event()
        threading.Thread(target=self._claude_limits_refresher, daemon=True).start()

        self.tray_icon = self._make_tray_icon()
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    # ---------- трей ----------

    def _make_tray_icon(self):
        icon_path = os.path.join(_bundled_dir(), 'assets', 'app_icon.png')
        image = Image.open(icon_path) if os.path.exists(icon_path) else Image.new('RGB', (32, 32), ACCENT)
        menu = pystray.Menu(
            pystray.MenuItem('Відкрити', self._on_tray_restore, default=True),
            pystray.MenuItem('Вихід', self._on_tray_quit),
        )
        return pystray.Icon('womier_sk80', image, 'Womier SK80', menu)

    def _on_tray_restore(self, icon=None, item=None):
        self.after(0, self._show_window)

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _hide_to_tray(self):
        self.withdraw()
        self.log('Згорнуто в трей — цикл (якщо активний) продовжує працювати. Повний вихід — через меню трея.')

    def _on_tray_quit(self, icon=None, item=None):
        self.log('Повний вихід із застосунку...')
        self.stop_event.set()
        self.claude_limits_stop.set()
        _save_app_state(self.selected_preset, False)
        try:
            self.tray_icon.stop()
        except Exception:
            pass
        threading.Thread(target=self._finish_quit, daemon=True).start()

    def _finish_quit(self):
        if self.worker is not None:
            self.worker.join(timeout=10)
        self._send_idle_screen()
        self.after(0, self.destroy)

    def _claude_limits_refresher(self):
        while not self.claude_limits_stop.is_set():
            self.log('Фонова перевірка лімітів Claude (кожні 5 хв)...')
            claude_limits.refresh_state()
            self._report_claude_limits_result()
            waited = 0
            while waited < CLAUDE_LIMITS_REFRESH_S and not self.claude_limits_stop.is_set():
                time.sleep(1)
                waited += 1

    def _report_claude_limits_result(self):
        """Спільна для фонової та ручної перевірки: логує результат і реагує на
        помилки (індикатор у статус-рядку, що очищується після відновлення)."""
        error = claude_limits.STATE.get('error')
        if error:
            self.claude_limits_error_streak += 1
            self.log(f'⚠ Помилка перевірки лімітів Claude ({self.claude_limits_error_streak}): {error}')
            self.after(0, lambda: self._show_claude_limits_error(error))
            return False

        if self.claude_limits_error_streak:
            self.log('Ліміти Claude знову доступні — помилку усунено.')
        self.claude_limits_error_streak = 0
        self.log(f'Ліміти Claude: {claude_limits_summary()}')
        self.after(0, self._clear_claude_limits_error)
        return True

    def _show_claude_limits_error(self, error):
        if not PRESETS.get(self.selected_preset, {}).get('change_key'):
            return
        self.status_dot.configure(text_color=DANGER)
        self.status_detail_label.configure(text=f'⚠ {error}', text_color=DANGER)

    def _clear_claude_limits_error(self):
        if not PRESETS.get(self.selected_preset, {}).get('change_key'):
            return
        self.status_detail_label.configure(text_color='#8A8F98')
        if self.worker is None or not self.worker.is_alive():
            self.status_dot.configure(text_color='#5A6070')
        else:
            self.status_dot.configure(text_color=ACCENT)

    # ---------- layout ----------

    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=190, corner_radius=0, fg_color=SIDEBAR_BG)
        sidebar.grid(row=0, column=0, sticky='nsw')
        sidebar.grid_propagate(False)

        header = ctk.CTkFrame(sidebar, fg_color='transparent')
        header.pack(fill='x', padx=20, pady=(28, 20))
        ctk.CTkLabel(header, text='Womier SK80', font=ctk.CTkFont(size=18, weight='bold')).pack(anchor='w')
        ctk.CTkLabel(header, text='Керування екраном', font=ctk.CTkFont(size=12),
                     text_color='#8A8F98').pack(anchor='w')

        ctk.CTkLabel(sidebar, text='ПРЕСЕТИ', font=ctk.CTkFont(size=11, weight='bold'),
                     text_color='#5A6070').pack(anchor='w', padx=20, pady=(8, 6))

        for name, preset in PRESETS.items():
            disabled = preset.get('disabled', False)
            btn = ctk.CTkButton(
                sidebar, text=name, anchor='w', height=38, corner_radius=8,
                fg_color='transparent', hover_color='#22262D',
                text_color=('#5A6070' if disabled else '#D5D8DD'),
                font=ctk.CTkFont(size=13),
                command=(lambda n=name: self._select_preset(n)) if not disabled else None,
                state='disabled' if disabled else 'normal',
            )
            btn.pack(fill='x', padx=12, pady=2)
            self.preset_buttons[name] = btn

        ctk.CTkLabel(sidebar, text='v1.0 · локальний застосунок', font=ctk.CTkFont(size=10),
                     text_color='#4A4F58').pack(side='bottom', pady=16)

    def _build_main(self):
        main = ctk.CTkScrollableFrame(self, fg_color='transparent')
        main.grid(row=0, column=1, sticky='nsew', padx=24, pady=24)
        main.grid_columnconfigure(0, weight=1)

        # --- превʼю ---
        preview_card = ctk.CTkFrame(main, fg_color=CARD_BG, corner_radius=14,
                                     border_width=1, border_color=CARD_BORDER)
        preview_card.grid(row=0, column=0, sticky='ew', pady=(0, 16))

        header_row = ctk.CTkFrame(preview_card, fg_color='transparent')
        header_row.pack(fill='x', padx=20, pady=(16, 8))
        self.preset_title_label = ctk.CTkLabel(
            header_row, text='', font=ctk.CTkFont(size=14, weight='bold'),
        )
        self.preset_title_label.pack(side='left')

        status_row = ctk.CTkFrame(header_row, fg_color='transparent')
        status_row.pack(side='right')
        self.status_dot = ctk.CTkLabel(status_row, text='●', font=ctk.CTkFont(size=12),
                                        text_color='#5A6070', width=14)
        self.status_dot.pack(side='left')
        self.status_label = ctk.CTkLabel(status_row, text='Зупинено', font=ctk.CTkFont(size=12, weight='bold'))
        self.status_label.pack(side='left', padx=(4, 8))
        self.status_detail_label = ctk.CTkLabel(status_row, text='', font=ctk.CTkFont(size=11),
                                                 text_color='#8A8F98')
        self.status_detail_label.pack(side='left')

        bezel = ctk.CTkFrame(preview_card, fg_color='#08090B', corner_radius=10)
        bezel.pack(padx=20, pady=(0, 20))
        self.preview_label = ctk.CTkLabel(bezel, text='', width=160 * PREVIEW_SCALE, height=90 * PREVIEW_SCALE)
        self.preview_label.pack(padx=10, pady=10)

        # --- кольори (лише для пресетів з has_colors) ---
        self.color_card = ctk.CTkFrame(main, fg_color=CARD_BG, corner_radius=14,
                                        border_width=1, border_color=CARD_BORDER)
        self.color_card.grid(row=1, column=0, sticky='ew', pady=(0, 16))
        ctk.CTkLabel(self.color_card, text='КОЛЬОРИ', font=ctk.CTkFont(size=11, weight='bold'),
                     text_color='#5A6070').pack(anchor='w', padx=20, pady=(14, 6))

        swatch_row = ctk.CTkFrame(self.color_card, fg_color='transparent')
        swatch_row.pack(anchor='w', padx=16, pady=(0, 6))
        for label, tcolor, dcolor in CLOCK_COLOR_PRESETS:
            img = _make_swatch_image(tcolor, dcolor)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=SWATCH_SIZE)
            b = ctk.CTkButton(
                swatch_row, image=ctk_img, text='', width=SWATCH_SIZE[0] + 8, height=SWATCH_SIZE[1] + 8,
                fg_color='transparent', hover_color='#2A2F37', corner_radius=6,
                command=lambda t=tcolor, d=dcolor: self._apply_clock_colors(t, d),
            )
            b.pack(side='left', padx=4)
            self.swatch_buttons.append(b)

        custom_row = ctk.CTkFrame(self.color_card, fg_color='transparent')
        custom_row.pack(anchor='w', padx=16, pady=(6, 16))

        ctk.CTkLabel(custom_row, text='Свій колір:', font=ctk.CTkFont(size=12),
                     text_color='#8A8F98').pack(side='left', padx=(0, 10))
        self.time_color_button = ctk.CTkButton(
            custom_row, text='Час', width=90, height=28, corner_radius=6,
            fg_color=_rgb_hex(CLOCK_STATE['time_color']), text_color='#0B0F0C',
            hover_color=_rgb_hex(CLOCK_STATE['time_color']),
            command=lambda: self._pick_custom_color('time_color'),
        )
        self.time_color_button.pack(side='left', padx=4)
        self.date_color_button = ctk.CTkButton(
            custom_row, text='Дата', width=90, height=28, corner_radius=6,
            fg_color=_rgb_hex(CLOCK_STATE['date_color']), text_color='#0B0F0C',
            hover_color=_rgb_hex(CLOCK_STATE['date_color']),
            command=lambda: self._pick_custom_color('date_color'),
        )
        self.date_color_button.pack(side='left', padx=4)

        # --- кнопки ---
        button_row = ctk.CTkFrame(main, fg_color='transparent')
        button_row.grid(row=2, column=0, sticky='ew', pady=(0, 16))
        self.start_button = ctk.CTkButton(
            button_row, text='▶  Старт', command=self.on_start, width=130, height=38,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, font=ctk.CTkFont(size=13, weight='bold'),
        )
        self.start_button.pack(side='left', padx=(0, 10))
        self.stop_button = ctk.CTkButton(
            button_row, text='⏹  Стоп', command=self.on_stop, width=130, height=38,
            fg_color=DANGER, hover_color=DANGER_HOVER, state='disabled',
            font=ctk.CTkFont(size=13, weight='bold'),
        )
        self.stop_button.pack(side='left', padx=(0, 10))
        self.check_now_button = ctk.CTkButton(
            button_row, text='🔄  Перевірити зараз', command=self.on_check_now, width=170, height=38,
            fg_color='#2A2F37', hover_color='#363C46', font=ctk.CTkFont(size=13),
        )
        self.check_now_button.pack(side='left')

        # --- журнал ---
        log_card = ctk.CTkFrame(main, fg_color=CARD_BG, corner_radius=14,
                                 border_width=1, border_color=CARD_BORDER)
        log_card.grid(row=3, column=0, sticky='nsew')
        log_header = ctk.CTkFrame(log_card, fg_color='transparent')
        log_header.pack(fill='x', padx=16, pady=(12, 4))
        ctk.CTkLabel(log_header, text='ЖУРНАЛ', font=ctk.CTkFont(size=11, weight='bold'),
                     text_color='#5A6070').pack(side='left')
        ctk.CTkButton(log_header, text='Очистити', width=80, height=22, fg_color='transparent',
                      hover_color='#22262D', text_color='#5A6070', font=ctk.CTkFont(size=11),
                      command=self._clear_log).pack(side='right')
        self.log_box = ctk.CTkTextbox(log_card, height=220, state='disabled', fg_color='transparent',
                                       font=ctk.CTkFont(family='Consolas', size=12))
        self.log_box.pack(fill='both', expand=True, padx=16, pady=(0, 16))

    # ---------- кольори ----------

    def _apply_clock_colors(self, time_color, date_color):
        CLOCK_STATE['time_color'] = time_color
        CLOCK_STATE['date_color'] = date_color
        self.time_color_button.configure(fg_color=_rgb_hex(time_color), hover_color=_rgb_hex(time_color))
        self.date_color_button.configure(fg_color=_rgb_hex(date_color), hover_color=_rgb_hex(date_color))
        self._rerender_preview()

    def _pick_custom_color(self, key):
        current = _rgb_hex(CLOCK_STATE[key])
        result = colorchooser.askcolor(color=current, title='Обери колір', parent=self)
        if result and result[0]:
            rgb = tuple(int(c) for c in result[0])
            if key == 'time_color':
                self._apply_clock_colors(rgb, CLOCK_STATE['date_color'])
            else:
                self._apply_clock_colors(CLOCK_STATE['time_color'], rgb)

    # ---------- пресети / превʼю ----------

    def _select_preset(self, name):
        self.selected_preset = name
        for n, btn in self.preset_buttons.items():
            is_active = (n == name)
            btn.configure(
                fg_color=ACCENT if is_active else 'transparent',
                text_color='#0B0F0C' if is_active else ('#5A6070' if PRESETS[n].get('disabled') else '#D5D8DD'),
                hover_color=ACCENT_HOVER if is_active else '#22262D',
            )
        self.preset_title_label.configure(text=f'Прев’ю — {name}')

        if PRESETS[name].get('has_colors'):
            self.color_card.grid()
        else:
            self.color_card.grid_remove()

        if PRESETS[name].get('change_key'):
            self.check_now_button.pack(side='left')
        else:
            self.check_now_button.pack_forget()

        self.preview_frames = None  # почати анімацію (якщо є) з чистого аркуша для нового пресету
        self.preview_frame_idx = 0
        self._rerender_preview()

    def _rerender_preview(self):
        img = PRESETS[self.selected_preset]['render'](time.time(), scale=PREVIEW_SCALE)
        self._set_preview_image(img)

    def _set_preview_image(self, pil_img):
        if getattr(self, 'preview_ctk_image', None) is None:
            self.preview_ctk_image = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=pil_img.size)
            self.preview_label.configure(image=self.preview_ctk_image, text='')
        else:
            self.preview_ctk_image.configure(light_image=pil_img, dark_image=pil_img)

    def _tick_preview(self):
        next_delay = PREVIEW_TICK_MS
        try:
            preset = PRESETS[self.selected_preset]
            render_frames = preset.get('render_frames')
            if render_frames:
                if not self.preview_frames:
                    self.preview_frames = render_frames(time.time(), scale=PREVIEW_SCALE)
                    self.preview_frame_idx = 0
                img, delay_ms = self.preview_frames[self.preview_frame_idx]
                self._set_preview_image(img)
                self.preview_frame_idx += 1
                if self.preview_frame_idx >= len(self.preview_frames):
                    self.preview_frame_idx = 0
                    self.preview_frames = None  # наступний цикл підхопить свіжі дані
                next_delay = max(30, delay_ms)
            else:
                self._rerender_preview()
        except Exception:
            pass
        self.after(next_delay, self._tick_preview)

    # ---------- журнал ----------

    def log(self, msg):
        self.log_queue.put(f'[{time.strftime("%H:%M:%S")}] {msg}')

    def _clear_log(self):
        self.log_box.configure(state='normal')
        self.log_box.delete('1.0', 'end')
        self.log_box.configure(state='disabled')

    def _drain_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.log_box.configure(state='normal')
            self.log_box.insert('end', msg + '\n')
            self.log_box.see('end')
            self.log_box.configure(state='disabled')
        self.after(200, self._drain_log_queue)

    # ---------- дії ----------

    def on_check_now(self):
        threading.Thread(target=self._check_now, daemon=True).start()

    def _check_now(self):
        self.log('Ручна перевірка лімітів Claude...')
        claude_limits.refresh_state()
        ok = self._report_claude_limits_result()
        self.after(0, self._rerender_preview)
        if not ok:
            return

        preset_name = self.selected_preset
        preset = PRESETS.get(preset_name, {})
        change_key_fn = preset.get('change_key')
        if not change_key_fn:
            return

        current_key = change_key_fn()
        if current_key is None or current_key == self.last_sent_key:
            self.log('Ліміти без змін — на екран нічого не надсилаю.')
            return

        if protocol.driver_process_running():
            self.log('Womier-SK80 Driver запущений — оновлення на екран не надіслано.')
            return

        with self.hid_lock:
            try:
                elapsed = self._send_preset(preset, time.time())
                self.last_sent_key = current_key
                detail = f'останнє: {time.strftime("%H:%M:%S")} · {elapsed:.2f}с · {current_key}'
                self.after(0, lambda: self.status_detail_label.configure(text=detail))
                self.log(f'Ліміти змінились {current_key} — оновлено на екрані, {elapsed:.2f}с')
            except protocol.DeviceUnavailable as e:
                self.log(f'Пристрій не знайдено: {e}')
            except Exception as e:
                self.log(f'ПОМИЛКА: {e}')

    def _set_sidebar_locked(self, locked):
        """Забороняє перемикати пресет, поки цикл активний — інакше стара й нова
        відправки могли б одночасно змагатись за один і той самий HID-пристрій."""
        for name, btn in self.preset_buttons.items():
            if PRESETS[name].get('disabled'):
                continue
            btn.configure(state='disabled' if locked else 'normal')

    def on_start(self):
        if self.worker is not None and self.worker.is_alive():
            self.log('Цикл вже працює — спочатку натисни "Стоп".')
            return
        preset_name = self.selected_preset
        preset = PRESETS[preset_name]
        if preset.get('disabled'):
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=self._run_loop, args=(preset_name, preset), daemon=True)
        self.worker.start()
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self._set_sidebar_locked(True)
        self.status_dot.configure(text_color=ACCENT)
        self.status_label.configure(text=f'Працює — {preset_name}')
        self.log(f'Старт: {preset_name}')
        _save_app_state(preset_name, True)

    def on_stop(self):
        self.stop_event.set()
        self.start_button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        self._set_sidebar_locked(False)
        self.status_dot.configure(text_color='#5A6070')
        self.status_label.configure(text='Зупинено')
        self.status_detail_label.configure(text='')
        self.log('Зупинено')
        _save_app_state(self.selected_preset, False)
        threading.Thread(target=self._finish_stop_and_send_idle, daemon=True).start()

    def _finish_stop_and_send_idle(self):
        # чекаємо, поки робочий потік справді звільнить HID-пристрій, перш ніж відкривати його знову
        if self.worker is not None:
            self.worker.join(timeout=10)
        self._send_idle_screen()

    def _send_idle_screen(self):
        if protocol.driver_process_running():
            self.log('Womier-SK80 Driver запущений — заставку не надіслано.')
            return
        try:
            img = render_idle_screen()
            pixel_data = protocol.image_to_rgb565(img)
            elapsed = protocol.send_pixel_data(pixel_data)
            self.log(f'Заставку надіслано, {elapsed:.2f}с')
        except protocol.DeviceUnavailable as e:
            self.log(f'Пристрій не знайдено: {e}')
        except Exception as e:
            self.log(f'ПОМИЛКА (заставка): {e}')


    def _send_preset(self, preset, render_time):
        """Рендерить і надсилає один пресет (звичайний кадр або анімацію render_frames),
        повертає час виконання (сек)."""
        render_frames = preset.get('render_frames')
        if render_frames:
            frames = render_frames(render_time)
            pixel_frames = [protocol.image_to_rgb565(img) for img, _ in frames]
            delays = [d for _, d in frames]
            return protocol.send_frames(pixel_frames, delays, stop_check=self.stop_event.is_set)
        img = preset['render'](render_time)
        pixel_data = protocol.image_to_rgb565(img)
        return protocol.send_pixel_data(pixel_data, stop_check=self.stop_event.is_set)

    def _wait_interruptible(self, seconds):
        remaining = seconds
        while remaining > 0 and not self.stop_event.is_set():
            step = min(remaining, 0.5)
            time.sleep(step)
            remaining -= step

    def _run_loop(self, preset_name, preset):
        if preset.get('change_key'):
            self._run_loop_change_detect(preset_name, preset)
        else:
            self._run_loop_interval(preset_name, preset)
        self.log('Цикл зупинено')

    def _run_loop_change_detect(self, preset_name, preset, check_interval=10):
        """Дані (напр. ліміти Claude) перевіряються постійно у фоні (окремим потоком),
        а тут ми лише порівнюємо їх з тим, що востаннє надіслали на клавіатуру, і
        шлемо новий кадр ТІЛЬКИ якщо значення реально змінилися."""
        change_key_fn = preset['change_key']

        # одразу після Старту вантажимо актуальні дані, а не чекаємо, поки
        # фоновий 5-хвилинний потік сам колись їх освіжить
        self.log('Завантажую актуальні ліміти Claude перед відображенням...')
        claude_limits.refresh_state()
        self._report_claude_limits_result()

        while not self.stop_event.is_set():
            if protocol.driver_process_running():
                self.log('Womier-SK80 Driver запущений — пропускаю перевірку, закрий драйвер.')
                self._wait_interruptible(30)
                continue

            current_key = change_key_fn()
            if current_key is not None and current_key != self.last_sent_key:
                with self.hid_lock:
                    try:
                        elapsed = self._send_preset(preset, time.time())
                        self.last_sent_key = current_key
                        detail = f'останнє: {time.strftime("%H:%M:%S")} · {elapsed:.2f}с · {current_key}'
                        self.status_detail_label.configure(text=detail)
                        self.log(f'Ліміти змінились {current_key} — надіслано, {elapsed:.2f}с')
                    except protocol.DeviceUnavailable as e:
                        self.log(f'Пристрій не знайдено: {e}')
                    except Exception as e:
                        self.log(f'ПОМИЛКА: {e}')

            self._wait_interruptible(check_interval)

    def _run_loop_interval(self, preset_name, preset):
        est_duration = 3.6
        sync_to_minute = preset['sync_to_minute']
        interval = preset['interval']

        # миттєве надсилання обраного пресету одразу при натисканні "Старт"
        if not self.stop_event.is_set() and not protocol.driver_process_running():
            try:
                t0 = time.time()
                est_duration = self._send_preset(preset, time.time())
                self.log(f'Надіслано, {est_duration:.2f}с')
                self.status_detail_label.configure(
                    text=f'останнє: {time.strftime("%H:%M:%S")} · {est_duration:.2f}с')
            except protocol.DeviceUnavailable as e:
                self.log(f'Пристрій не знайдено: {e}')
            except Exception as e:
                self.log(f'ПОМИЛКА: {e}')

        next_target = (int(time.time()) // interval + 1) * interval if sync_to_minute else time.time()

        while not self.stop_event.is_set():
            target_start = (next_target - est_duration) if sync_to_minute else next_target
            wait = target_start - time.time()
            self._wait_interruptible(wait)
            if self.stop_event.is_set():
                break

            if protocol.driver_process_running():
                self.log('Womier-SK80 Driver запущений — пропускаю цикл, закрий драйвер.')
                time.sleep(5)
                if not sync_to_minute:
                    next_target = time.time()
                continue

            try:
                render_time = next_target if sync_to_minute else time.time()
                elapsed = self._send_preset(preset, render_time)
                est_duration = elapsed
                if sync_to_minute:
                    offset = time.time() - render_time
                    self.log(f'Надіслано, {elapsed:.2f}с, відхилення {offset:+.2f}с від :00')
                    detail = f'останнє: {time.strftime("%H:%M:%S")} · {elapsed:.2f}с · {offset:+.2f}с від :00'
                else:
                    self.log(f'Надіслано, {elapsed:.2f}с')
                    detail = f'останнє: {time.strftime("%H:%M:%S")} · {elapsed:.2f}с'
                self.status_detail_label.configure(text=detail)
            except protocol.DeviceUnavailable as e:
                self.log(f'Пристрій не знайдено: {e}')
            except Exception as e:
                self.log(f'ПОМИЛКА: {e}')

            next_target += interval


if __name__ == '__main__':
    app = App()
    app.mainloop()
