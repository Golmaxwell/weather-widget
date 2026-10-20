#!/usr/bin/env python3
"""Desktop weather widget for Ubuntu GNOME (runs on X11 / XWayland).

Click the card to open the city panel. Drag it to move. Right-click for actions.
Weather data is provided by Open-Meteo; no API key is required.
"""
import os
os.environ.setdefault("GDK_BACKEND", "x11")

import json
import math
import random
import threading
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

import cairo
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

CONFIG_DIR = Path.home() / ".config" / "weather-desk"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_CITIES = [
    {"name": "Gulu", "lat": 2.7746, "lon": 32.2990, "enabled": True, "pinned": True},
    {"name": "Kampala", "lat": 0.3476, "lon": 32.5825, "enabled": True, "pinned": True},
    {"name": "Hoima", "lat": 1.4356, "lon": 31.3436, "enabled": True, "pinned": True},
]
W, H = 310, 244
DEFAULT_ALPHA = 0.68
DEFAULT_ROTATE_SECONDS = 30
REFRESH_SECONDS = 600
RETRY_SECONDS = 30
URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
    "wind_speed_10m,weather_code,is_day"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&forecast_days=4&timezone=auto"
)


def load_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        cities = cfg.get("cities")
        if isinstance(cities, list) and cities:
            for city in cities:
                city.setdefault("enabled", True)
                city.setdefault("pinned", False)
            cfg.setdefault("current_city", cities[0]["name"])
            return cfg
    except (OSError, ValueError, TypeError):
        pass
    return {"cities": [dict(c) for c in DEFAULT_CITIES], "current_city": "Gulu"}


def kind(code):
    if code == 0:
        return "clear", "Clear"
    if code in (1, 2):
        return "partly", "Partly cloudy"
    if code == 3:
        return "cloud", "Overcast"
    if code in (45, 48):
        return "fog", "Fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow", "Snow"
    if code >= 95:
        return "storm", "Thunderstorm"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain", "Rain"
    return "cloud", "Cloudy"


SKY = {
    "clear": ((0.12, 0.45, 0.90), (0.40, 0.72, 0.96)),
    "partly": ((0.20, 0.48, 0.85), (0.50, 0.74, 0.93)),
    "cloud": ((0.35, 0.42, 0.52), (0.58, 0.64, 0.72)),
    "fog": ((0.45, 0.50, 0.56), (0.68, 0.72, 0.76)),
    "rain": ((0.22, 0.28, 0.38), (0.40, 0.48, 0.58)),
    "snow": ((0.40, 0.48, 0.60), (0.70, 0.76, 0.84)),
    "storm": ((0.12, 0.14, 0.22), (0.28, 0.30, 0.42)),
}
NIGHT = ((0.04, 0.06, 0.16), (0.14, 0.18, 0.34))


class WeatherPanel(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="Weather")
        self.app = app
        self.set_default_size(400, 540)
        self.set_resizable(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_keep_above(True)
        self.set_border_width(16)
        self.connect("key-press-event", self.on_key)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(outer)

        top = Gtk.Box(spacing=8)
        title = Gtk.Label()
        title.set_markup("<span size='large' weight='bold'>Weather</span>")
        title.set_xalign(0)
        top.pack_start(title, True, True, 0)
        refresh_btn = Gtk.Button(label="↻ Refresh")
        refresh_btn.connect("clicked", lambda *_: self.app.refresh())
        top.pack_start(refresh_btn, False, False, 0)
        close_btn = Gtk.Button(label="✕")
        close_btn.set_tooltip_text("Hide this panel (the desktop widget stays running)")
        close_btn.connect("clicked", lambda *_: self.hide())
        top.pack_start(close_btn, False, False, 0)
        outer.pack_start(top, False, False, 0)

        subtitle = Gtk.Label(label="Choose which cities appear and pin favourites.")
        subtitle.set_xalign(0)
        subtitle.set_line_wrap(True)
        outer.pack_start(subtitle, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(260)
        self.city_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.city_box.set_border_width(2)
        scroller.add(self.city_box)
        outer.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(spacing=8)
        add_btn = Gtk.Button(label="+ Add city")
        add_btn.connect("clicked", self.add_city_dialog)
        buttons.pack_start(add_btn, True, True, 0)
        settings_btn = Gtk.Button(label="Preferences")
        settings_btn.connect("clicked", self.preferences_dialog)
        buttons.pack_start(settings_btn, True, True, 0)
        outer.pack_start(buttons, False, False, 0)

        self.status = Gtk.Label(label="Weather data from Open-Meteo")
        self.status.set_xalign(0)
        self.status.get_style_context().add_class("dim-label")
        outer.pack_start(self.status, False, False, 0)
        self.rebuild_cities()

    def on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def rebuild_cities(self):
        for child in self.city_box.get_children():
            self.city_box.remove(child)
        for city in sorted(self.app.cities, key=lambda c: (not c.get("pinned", False), c["name"].lower())):
            row = Gtk.Box(spacing=8)
            row.set_border_width(7)
            enabled = Gtk.CheckButton(label=city["name"])
            enabled.set_active(bool(city.get("enabled", True)))
            enabled.set_tooltip_text("Show this city in the city panel")
            enabled.connect("toggled", self.on_enabled, city["name"])
            row.pack_start(enabled, True, True, 0)

            data = self.app.store.get(city["name"])
            if data:
                current = data.get("current", {})
                summary = "{}°".format(round(current.get("temperature_2m", 0)))
            else:
                summary = "—"
            temp = Gtk.Label(label=summary)
            row.pack_start(temp, False, False, 0)

            pin = Gtk.ToggleButton(label="★" if city.get("pinned") else "☆")
            pin.set_active(bool(city.get("pinned")))
            pin.set_tooltip_text("Pin/unpin this city")
            pin.connect("toggled", self.on_pin, city["name"])
            row.pack_start(pin, False, False, 0)

            choose = Gtk.Button(label="View")
            choose.set_tooltip_text("Show this city on the desktop card")
            choose.connect("clicked", self.choose_city, city["name"])
            row.pack_start(choose, False, False, 0)

            remove = Gtk.Button(label="−")
            remove.set_tooltip_text("Remove this city")
            remove.connect("clicked", self.remove_city, city["name"])
            row.pack_start(remove, False, False, 0)

            self.city_box.pack_start(row, False, False, 0)
        self.city_box.show_all()

    def on_enabled(self, button, name):
        city = self.app.city_by_name(name)
        if city:
            city["enabled"] = button.get_active()
            if not city["enabled"] and self.app.current_name == name:
                remaining = [c for c in self.app.cities if c.get("enabled")]
                if remaining:
                    self.app.set_city(remaining[0]["name"])
            self.app.save_config()
            GLib.idle_add(self.rebuild_cities)

    def on_pin(self, button, name):
        city = self.app.city_by_name(name)
        if city:
            city["pinned"] = button.get_active()
            button.set_label("★" if button.get_active() else "☆")
            self.app.save_config()
            self.app.queue_draw()
            self.status.set_text("Pinned cities are marked with ★.")

    def choose_city(self, _button, name):
        self.app.set_city(name)
        self.status.set_text("Showing {} on the desktop.".format(name))

    def remove_city(self, _button, name):
        if len(self.app.cities) <= 1:
            self.status.set_text("Keep at least one city in your list.")
            return
        self.app.cities = [c for c in self.app.cities if c["name"] != name]
        self.app.store.pop(name, None)
        self.app.updated.pop(name, None)
        self.app.failed.discard(name)
        if self.app.current_name == name:
            self.app.set_city(self.app.cities[0]["name"])
        self.app.save_config()
        self.rebuild_cities()

    def add_city_dialog(self, *_):
        dialog = Gtk.Dialog(title="Find a city", transient_for=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Search", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_border_width(14)
        box.set_spacing(8)
        box.pack_start(Gtk.Label(label="Enter a city name:"), False, False, 0)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Entebbe, Nairobi, London...")
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        response = dialog.run()
        query = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not query:
            return
        self.status.set_text("Searching for {}…".format(query))

        def work():
            try:
                url = ("https://geocoding-api.open-meteo.com/v1/search?name="
                       + urllib.parse.quote(query) + "&count=8&language=en&format=json")
                with urllib.request.urlopen(url, timeout=12) as reply:
                    results = json.load(reply).get("results", [])
                GLib.idle_add(self.show_city_results, query, results)
            except Exception as exc:
                GLib.idle_add(self.status.set_text, "Search failed: {}".format(exc))
        threading.Thread(target=work, daemon=True).start()

    def show_city_results(self, query, results):
        if not results:
            self.status.set_text("No matches for {}.".format(query))
            return False
        dialog = Gtk.Dialog(title="Choose a city", transient_for=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add city", Gtk.ResponseType.OK)
        combo = Gtk.ComboBoxText()
        for result in results:
            combo.append_text(", ".join(filter(None, (result.get("name"),
                              result.get("admin1"), result.get("country")))))
        combo.set_active(0)
        box = dialog.get_content_area()
        box.set_border_width(14)
        box.pack_start(Gtk.Label(label="Select the correct location:"), False, False, 8)
        box.pack_start(combo, False, False, 8)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            result = results[combo.get_active()]
            name = result["name"]
            if self.app.city_by_name(name):
                self.status.set_text("{} is already in the list.".format(name))
            else:
                self.app.cities.append({"name": name, "lat": result["latitude"],
                                        "lon": result["longitude"],
                                        "enabled": True, "pinned": False})
                self.app.save_config()
                self.app.refresh()
                self.rebuild_cities()
                self.status.set_text("Added {}.".format(name))
        dialog.destroy()
        return False

    def preferences_dialog(self, *_):
        dialog = Gtk.Dialog(title="Weather preferences", transient_for=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_border_width(16)
        box.set_spacing(10)

        rotate = Gtk.CheckButton(label="Automatically rotate the desktop card through pinned cities")
        rotate.set_active(self.app.rotate_enabled)
        box.pack_start(rotate, False, False, 0)

        seconds_row = Gtk.Box(spacing=8)
        seconds_row.pack_start(Gtk.Label(label="Seconds per city"), False, False, 0)
        seconds = Gtk.SpinButton.new_with_range(10, 300, 5)
        seconds.set_value(self.app.rotate_seconds)
        seconds_row.pack_start(seconds, False, False, 0)
        box.pack_start(seconds_row, False, False, 0)

        always = Gtk.CheckButton(label="Keep the desktop card above other windows")
        always.set_active(self.app.keep_above)
        box.pack_start(always, False, False, 0)

        box.pack_start(Gtk.Label(label="Card opacity", xalign=0), False, False, 0)
        opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.3, 1.0, 0.05)
        opacity.set_digits(2)
        opacity.set_value(self.app.alpha)
        box.pack_start(opacity, False, False, 0)

        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            self.app.rotate_enabled = rotate.get_active()
            self.app.rotate_seconds = int(seconds.get_value())
            self.app.keep_above = always.get_active()
            self.app.alpha = opacity.get_value()
            self.app.apply_stacking()
            self.app.save_config()
            self.app.reset_rotation()
            self.app.queue_draw()
        dialog.destroy()


class Widget(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_title("Weather Widget")
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_default_size(W, H)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.stick()
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.cfg = load_config()
        self.cities = self.cfg.get("cities", [dict(c) for c in DEFAULT_CITIES])
        if not self.cities:
            self.cities = [dict(c) for c in DEFAULT_CITIES]
        self.store = {}
        self.updated = {}
        self.failed = set()
        self.retry_source = 0
        self.current_name = self.cfg.get("current_city", self.cities[0]["name"])
        if not self.city_by_name(self.current_name):
            self.current_name = self.cities[0]["name"]
        self.rotate_enabled = bool(self.cfg.get("rotate_enabled", False))
        self.rotate_seconds = int(self.cfg.get("rotate_seconds", DEFAULT_ROTATE_SECONDS))
        self.keep_above = bool(self.cfg.get("keep_above", False))
        self.alpha = float(self.cfg.get("alpha", DEFAULT_ALPHA))
        self.apply_stacking()
        self.t = 0.0
        self.flash = 0.0
        self._drag = None
        self.drops = [[random.random() * W, random.random() * H, 6 + random.random() * 5]
                      for _ in range(45)]
        self.clouds = [[random.random() * (W + 160), 25 + random.random() * 60,
                        0.6 + random.random() * 0.7, 8 + random.random() * 14]
                       for _ in range(4)]

        self.place_initial()

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.BUTTON1_MOTION_MASK | Gdk.EventMask.SCROLL_MASK)
        self.connect("button-press-event", self.on_press)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("button-release-event", self.on_release)
        self.connect("scroll-event", self.on_scroll)
        self.connect("draw", self.on_draw)
        self.connect("destroy", lambda *_: Gtk.main_quit())
        self.panel = WeatherPanel(self)
        self.panel.connect("delete-event", lambda window, event: window.hide() or True)
        GLib.timeout_add(33, self.tick)
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)
        self.reset_rotation()

    # ----- state helpers -------------------------------------------------
    @property
    def data(self):
        return self.store.get(self.current_name)

    def city_by_name(self, name):
        return next((c for c in self.cities if c["name"] == name), None)

    def enabled_cities(self):
        return [c for c in self.cities if c.get("enabled", True)]

    def pinned_cities(self):
        pinned = [c for c in self.enabled_cities() if c.get("pinned")]
        return pinned or self.enabled_cities()

    def apply_stacking(self):
        if self.keep_above:
            self.set_keep_below(False)
            self.set_keep_above(True)
        else:
            self.set_keep_above(False)
            self.set_keep_below(True)

    def save_config(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.cfg.update({
            "cities": self.cities,
            "current_city": self.current_name,
            "rotate_enabled": self.rotate_enabled,
            "rotate_seconds": self.rotate_seconds,
            "keep_above": self.keep_above,
            "alpha": self.alpha,
        })
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=2))
        tmp.replace(CONFIG_FILE)

    # ----- position ------------------------------------------------------
    def place_default(self):
        try:
            geo = Gdk.Display.get_default().get_primary_monitor().get_geometry()
            self.move(geo.x + geo.width - W - 30, geo.y + 60)
        except Exception:
            self.move(40, 60)

    def place_initial(self):
        x, y = self.cfg.get("x"), self.cfg.get("y")
        screen = self.get_screen()
        if (isinstance(x, int) and isinstance(y, int)
                and -W + 60 < x < screen.get_width() - 60
                and 0 <= y < screen.get_height() - 60):
            self.move(x, y)
        else:
            self.place_default()

    def save_position(self):
        x, y = self.get_position()
        self.cfg["x"], self.cfg["y"] = int(x), int(y)
        self.save_config()

    def reset_position(self):
        self.place_default()
        GLib.timeout_add(200, lambda: self.save_position() or False)

    # ----- cities / rotation --------------------------------------------
    def set_city(self, name):
        if not self.city_by_name(name):
            return
        self.current_name = name
        self.save_config()
        self.queue_draw()
        if self.panel.get_visible():
            self.panel.rebuild_cities()

    def reset_rotation(self):
        if getattr(self, "rot_source", None):
            try:
                GLib.source_remove(self.rot_source)
            except Exception:
                pass
        self.rot_source = 0
        if self.rotate_enabled:
            self.rot_source = GLib.timeout_add_seconds(max(5, self.rotate_seconds), self.auto_next)

    def auto_next(self):
        choices = self.pinned_cities()
        if len(choices) > 1:
            names = [c["name"] for c in choices]
            try:
                idx = names.index(self.current_name)
            except ValueError:
                idx = -1
            self.set_city(names[(idx + 1) % len(names)])
        return True

    def go(self, step):
        choices = self.enabled_cities()
        if not choices:
            return
        names = [c["name"] for c in choices]
        try:
            idx = names.index(self.current_name)
        except ValueError:
            idx = 0
        self.set_city(names[(idx + step) % len(names)])

    # ----- input ---------------------------------------------------------
    def on_scroll(self, _w, event):
        self.go(1 if event.direction == Gdk.ScrollDirection.DOWN else -1)
        return True

    def on_press(self, _w, event):
        if event.button == 1:
            x, y = self.get_position()
            self._drag = {"rx": event.x_root, "ry": event.y_root, "wx": x, "wy": y, "moved": False}
            return True
        if event.button == 2:
            self.go(1)
            return True
        if event.button == 3:
            menu = Gtk.Menu()
            for label, callback in (
                ("Open weather panel", self.open_panel),
                ("Refresh now", self.refresh),
                ("Preferences", self.open_preferences),
                ("Reset position", self.reset_position),
                ("Quit weather widget", self.quit_app),
            ):
                item = Gtk.MenuItem(label=label)
                item.connect("activate", lambda _item, cb=callback: cb())
                menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def on_motion(self, _w, event):
        drag = self._drag
        if not drag:
            return False
        dx = event.x_root - drag["rx"]
        dy = event.y_root - drag["ry"]
        if not drag["moved"] and (abs(dx) > 4 or abs(dy) > 4):
            drag["moved"] = True
        if drag["moved"]:
            self.move(int(drag["wx"] + dx), int(drag["wy"] + dy))
        return True

    def on_release(self, _w, event):
        if event.button == 1 and self._drag:
            drag, self._drag = self._drag, None
            if drag["moved"]:
                self.save_position()
            else:
                self.open_panel()
            return True
        return False

    def open_panel(self):
        self.panel.rebuild_cities()
        self.panel.show_all()
        try:
            geo = Gdk.Display.get_default().get_primary_monitor().get_geometry()
            width, _height = self.panel.get_size()
            self.panel.move(geo.x + geo.width - width - 12, geo.y + 42)
        except Exception:
            pass
        self.panel.present()

    def open_preferences(self):
        self.open_panel()
        self.panel.preferences_dialog()

    def quit_app(self):
        self.save_config()
        Gtk.main_quit()

    # ----- data ----------------------------------------------------------
    def refresh(self):
        for city in list(self.cities):
            name, lat, lon = city["name"], city["lat"], city["lon"]

            def work(name=name, lat=lat, lon=lon):
                try:
                    url = URL.format(lat=lat, lon=lon)
                    request = urllib.request.Request(url, headers={"User-Agent": "WeatherDesk/2.0"})
                    with urllib.request.urlopen(request, timeout=12) as response:
                        data = json.load(response)
                    GLib.idle_add(self.set_data, name, data)
                except Exception as exc:
                    GLib.idle_add(self.show_fetch_error, name, str(exc))
            threading.Thread(target=work, daemon=True).start()
        return True

    def set_data(self, name, data):
        if not self.city_by_name(name):
            return False
        self.store[name] = data
        self.updated[name] = datetime.now()
        self.failed.discard(name)
        self.queue_draw()
        if self.panel.get_visible():
            self.panel.rebuild_cities()
        return False

    def show_fetch_error(self, name, message):
        print("Weather refresh failed for {}: {}".format(name, message))
        self.failed.add(name)
        self.queue_draw()
        if self.panel.get_visible():
            self.panel.status.set_text("Could not refresh {}. Check your internet connection.".format(name))
        if not self.retry_source:
            self.retry_source = GLib.timeout_add_seconds(RETRY_SECONDS, self.retry)
        return False

    def retry(self):
        self.retry_source = 0
        self.refresh()
        return False

    # ----- animation -----------------------------------------------------
    def tick(self):
        self.t += 0.033
        k = kind(self.data["current"]["weather_code"])[0] if self.data else "clear"
        for drop in self.drops:
            drop[1] += drop[2] * (0.5 if k == "snow" else 1.6)
            if k == "snow":
                drop[0] += math.sin(self.t * 2 + drop[2]) * 0.6
            if drop[1] > H:
                drop[0], drop[1] = random.random() * W, -10
        for cloud in self.clouds:
            cloud[0] += cloud[3] * 0.033
            if cloud[0] > W + 80:
                cloud[0] = -80
        if k == "storm":
            if self.flash > 0:
                self.flash -= 0.08
            elif random.random() < 0.012:
                self.flash = 1.0
        self.queue_draw()
        return True

    # ----- drawing -------------------------------------------------------
    def cloud(self, cr, x, y, scale, alpha):
        cr.set_source_rgba(1, 1, 1, alpha)
        for dx, dy, radius in ((0, 0, 14), (16, -8, 18), (34, 0, 14), (16, 4, 16)):
            cr.arc(x + dx * scale, y + dy * scale, radius * scale, 0, 2 * math.pi)
            cr.fill()

    def text(self, cr, x, y, text, size, bold=False, alpha=1.0, anchor="left"):
        cr.select_font_face("Ubuntu", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(size)
        width = cr.text_extents(text).x_advance
        if anchor == "center":
            x -= width / 2
        elif anchor == "right":
            x -= width
        cr.set_source_rgba(0, 0, 0, 0.35 * alpha)
        cr.move_to(x + 1, y + 1)
        cr.show_text(text)
        cr.set_source_rgba(1, 1, 1, alpha)
        cr.move_to(x, y)
        cr.show_text(text)
        return width

    def mini_icon(self, cr, cx, cy, weather):
        def sun(x, y, r):
            cr.set_source_rgba(1, 0.85, 0.3, 0.95)
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.fill()
        if weather == "clear":
            sun(cx, cy, 7)
            return
        if weather == "partly":
            sun(cx + 5, cy - 5, 5)
        self.cloud(cr, cx - 7, cy + 1, 0.42, 0.95)
        if weather in ("rain", "storm"):
            cr.set_source_rgba(0.75, 0.85, 1, 0.95)
            cr.set_line_width(1.4)
            for dx in (-6, 0, 6):
                cr.move_to(cx + dx, cy + 9)
                cr.line_to(cx + dx - 1.5, cy + 14)
                cr.stroke()
        elif weather == "snow":
            cr.set_source_rgba(1, 1, 1, 0.95)
            for dx in (-6, 0, 6):
                cr.arc(cx + dx, cy + 12, 1.4, 0, 2 * math.pi)
                cr.fill()

    def day_name(self, iso):
        try:
            return datetime.strptime(iso, "%Y-%m-%d").strftime("%a")
        except (ValueError, TypeError):
            return ""

    def status_text(self):
        if self.current_name in self.failed:
            stamp = self.updated.get(self.current_name)
            return "Offline" + (" · {}".format(stamp.strftime("%H:%M")) if stamp else "")
        stamp = self.updated.get(self.current_name)
        return "Updated {}".format(stamp.strftime("%H:%M")) if stamp else ""

    def on_draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        radius = 22
        cr.new_sub_path()
        for cx, cy, angle in ((W - radius, radius, -90), (W - radius, H - radius, 0),
                              (radius, H - radius, 90), (radius, radius, 180)):
            cr.arc(cx, cy, radius, math.radians(angle), math.radians(angle + 90))
        cr.close_path()
        cr.clip()

        city = self.city_by_name(self.current_name)
        if not self.data:
            cr.set_source_rgba(0.1, 0.12, 0.2, self.alpha)
            cr.paint()
            self.text(cr, 20, 108, "Loading {}…".format(self.current_name), 16)
            hint = ("No connection. Retrying…" if self.current_name in self.failed
                    else "Click for cities and settings")
            self.text(cr, 20, 226, hint, 10, alpha=0.8)
            return False

        current = self.data["current"]
        daily = self.data["daily"]
        weather, label = kind(current["weather_code"])
        is_day = current["is_day"]
        top, bottom = SKY[weather] if is_day else NIGHT
        gradient = cairo.LinearGradient(0, 0, 0, H)
        gradient.add_color_stop_rgba(0, *top, self.alpha)
        gradient.add_color_stop_rgba(1, *bottom, self.alpha)
        cr.set_source(gradient)
        cr.paint()

        sx, sy = W - 58, 74
        if weather in ("clear", "partly"):
            if is_day:
                cr.set_source_rgba(1, 0.85, 0.3, 0.9)
                cr.save()
                cr.translate(sx, sy)
                cr.rotate(self.t * 0.4)
                for i in range(12):
                    cr.rotate(math.pi / 6)
                    cr.set_line_width(3)
                    cr.move_to(26, 0)
                    cr.line_to(36 + 3 * math.sin(self.t * 2 + i), 0)
                    cr.stroke()
                cr.restore()
                cr.set_source_rgba(1, 0.85, 0.3, 0.9)
                cr.arc(sx, sy, 19, 0, 2 * math.pi)
                cr.fill()
            else:
                cr.set_source_rgba(0.95, 0.95, 0.85, 0.95)
                cr.arc(sx, sy, 18, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(*NIGHT[1], 1)
                cr.arc(sx + 8, sy - 5, 16, 0, 2 * math.pi)
                cr.fill()
                for i in range(14):
                    random.seed(i)
                    a = 0.3 + 0.6 * abs(math.sin(self.t * 1.5 + i))
                    cr.set_source_rgba(1, 1, 1, a)
                    cr.arc(random.random() * W, random.random() * 110, 1.2, 0, 2 * math.pi)
                    cr.fill()
                random.seed()
        if weather != "clear":
            count = {"partly": 2, "cloud": 4, "fog": 4}.get(weather, 4)
            for cloud in self.clouds[:count]:
                self.cloud(cr, cloud[0], cloud[1], cloud[2],
                           0.55 if weather == "partly" else 0.75)
        if weather in ("rain", "storm"):
            cr.set_source_rgba(0.75, 0.85, 1, 0.75)
            cr.set_line_width(1.5)
            for drop in self.drops:
                cr.move_to(drop[0], drop[1])
                cr.line_to(drop[0] - 2, drop[1] + 9)
                cr.stroke()
        elif weather == "snow":
            cr.set_source_rgba(1, 1, 1, 0.9)
            for drop in self.drops:
                cr.arc(drop[0], drop[1], 2, 0, 2 * math.pi)
                cr.fill()
        elif weather == "fog":
            for i in range(4):
                cr.set_source_rgba(1, 1, 1, 0.18)
                cr.rectangle(0, 110 + i * 18 + math.sin(self.t + i) * 6, W, 8)
                cr.fill()
        if self.flash > 0:
            cr.set_source_rgba(1, 1, 1, 0.35 * self.flash)
            cr.paint()

        # header
        name_width = self.text(cr, 20, 28, self.current_name.upper(), 12, alpha=0.9)
        if city and city.get("pinned"):
            self.text(cr, 20 + name_width + 6, 28, "★", 12, alpha=0.9)
        self.text(cr, W - 18, 28, self.status_text(), 10, alpha=0.75, anchor="right")

        # current conditions
        self.text(cr, 18, 100, "{}°".format(round(current["temperature_2m"])), 56, bold=True)
        self.text(cr, 20, 128, label, 16)
        self.text(cr, 20, 156, "Feels {}°   Humidity {}%".format(
            round(current["apparent_temperature"]), current["relative_humidity_2m"]), 11, alpha=0.92)
        self.text(cr, 20, 174, "Wind {} km/h   Rain {}%".format(
            round(current["wind_speed_10m"]), daily["precipitation_probability_max"][0]), 11, alpha=0.92)

        # 3-day forecast
        cr.set_source_rgba(1, 1, 1, 0.22)
        cr.set_line_width(1)
        cr.move_to(18, 184.5)
        cr.line_to(W - 18, 184.5)
        cr.stroke()
        column = (W - 36) / 3.0
        for i in range(3):
            d = i + 1
            try:
                code = daily["weather_code"][d]
                hi = round(daily["temperature_2m_max"][d])
                lo = round(daily["temperature_2m_min"][d])
                day = self.day_name(daily["time"][d])
            except (KeyError, IndexError, TypeError):
                continue
            cx = 18 + column * (i + 0.5)
            self.text(cr, cx, 200, day, 11, alpha=0.9, anchor="center")
            self.mini_icon(cr, cx, 214, kind(code)[0])
            self.text(cr, cx, 236, "{}° / {}°".format(hi, lo), 11, alpha=0.92, anchor="center")
        return False


if __name__ == "__main__":
    app = Widget()
    app.show_all()
    Gtk.main()
