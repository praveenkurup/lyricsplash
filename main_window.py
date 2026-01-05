import configparser
import re
import os
import sys
import shutil
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QSystemTrayIcon,
    QMenu, QAction, QFormLayout, QLineEdit, QSpinBox, QComboBox,
    QPushButton, QMessageBox, QColorDialog, QCheckBox, QFontComboBox,
    QHBoxLayout, QScrollArea, QGroupBox
)
from PyQt5.QtGui import QFont, QIcon, QGuiApplication, QColor
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QGraphicsOpacityEffect
from PyQt5.QtCore import QPropertyAnimation
from pathlib import Path
import keyring
import threading
import subprocess
from version import APP_VERSION

class MainWindow(QWidget):
    # Signals for background operations
    update_check_finished = pyqtSignal(bool, object)  # (success_flag, data_or_message)
    update_download_finished = pyqtSignal(bool, object)  # (success_flag, path_or_error)
    startup_update_check_finished = pyqtSignal(bool, object)  # (success_flag, data_or_message)
    def __init__(self, signal_bus, appdata_location, service_name, service_username):
        super().__init__()
        # Core refs
        self.signal_bus = signal_bus
        self.appdata_location = appdata_location
        self.config_path = os.path.join(self.appdata_location, ".conf")

        # Load configuration
        self.load_config()

        # Runtime state
        self._standby_active = True
        self._pending_activation_win = None

        # Service credentials
        self.service_name = service_name
        self.service_username = service_username

        # Build settings UI first (styles, etc.)
        self.init_settings_ui()

        # Connect external signals
        self.signal_bus.update_ui.connect(self.update_display)
        self.signal_bus.auth_response.connect(self.on_auth_response, Qt.QueuedConnection)

    def resource_path(self, relative_path):
        """ Get absolute path to resource, works for dev and PyInstaller """
        if getattr(sys, 'frozen', False):  # Running in a bundle
            base_path = Path(sys._MEIPASS)
        else:  # Running live
            base_path = Path(__file__).parent
        return base_path / relative_path

    def animate_text_change(self, label: QLabel, new_text: str, duration: int = 250):
        if label.text() == new_text:
            return  # No change needed

        # Apply opacity effect if not already applied
        if not hasattr(label, "_opacity_effect"):
            effect = QGraphicsOpacityEffect(label)
            label.setGraphicsEffect(effect)
            label._opacity_effect = effect
        else:
            effect = label._opacity_effect

        anim = QPropertyAnimation(effect, b"opacity")
        anim.setDuration(duration)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)

        def on_fade_out_finished():
            label.setText(new_text)
            anim2 = QPropertyAnimation(effect, b"opacity")
            anim2.setDuration(duration)
            anim2.setStartValue(0.0)
            anim2.setEndValue(1.0)
            anim2.start()
            label._fade_in_anim = anim2  # prevent garbage collection

        anim.finished.connect(on_fade_out_finished)
        anim.start()
        label._fade_out_anim = anim  # prevent garbage collection

    def update_lyrics_text(self, text):
        self.lyrics_text = text
        self.animate_text_change(self.lyrics_label, text, self.lyrics_animation_duration)

    def update_info_text(self, text):
        self.info_text = text
        self.animate_text_change(self.info_label, text, self.info_animation_duration)

    def format_template(self, template, song_data):
        """Format a template string with song data"""
        if not song_data:
            return ""
        
        # Handle empty or None templates by returning empty string
        if not template or not template.strip():
            return ""
        
        # Clean up software name by removing .exe extension
        software = song_data.get('software', '')
        if software.lower().endswith('.exe'):
            software = software[:-4]  # Remove last 4 characters (.exe)
        
        # Convert is_playing boolean to user-friendly text
        is_playing = song_data.get('is_playing', False)
        playing_status = "Playing" if is_playing else "Paused"
        
        # Available placeholders
        placeholders = {
            'title': song_data.get('title', ''),
            'artist': song_data.get('artist', ''),
            'duration_sec': str(song_data.get('duration_sec', '')),
            'position_sec': str(song_data.get('position_sec', '')),
            'software': software,
            'is_playing': playing_status,
            'lyrics': song_data.get('lyrics', ''),
        }
        
        # Use a safer formatting approach that handles invalid placeholders gracefully
        def replace_placeholder(match):
            placeholder = match.group(1)  # Extract the key inside {}
            return placeholders.get(placeholder, match.group(0))  # Return replacement or original {key}
        
        # Replace all valid placeholders, leave invalid ones as-is
        result = re.sub(r'\{([^}]+)\}', replace_placeholder, template)
        return result

    def update_display(self, song_data: dict, standby: bool, error: bool):
        
        # Record whether we're in standby or live playback; used to avoid text resets on settings save
        self._standby_active = standby

        if error:
            info = song_data["info"]
            lyrics = song_data["lyrics"]
            self.animate_text_change(self.lyrics_label, lyrics, self.lyrics_animation_duration)
            self.animate_text_change(self.info_label, info, self.info_animation_duration)
            return

        if standby:
            # Show default text from current instance variables (which may be updated from settings)
            default_lyrics = self.lyrics_text
            default_info = self.info_text
            self.animate_text_change(self.lyrics_label, default_lyrics, self.lyrics_animation_duration)
            self.animate_text_change(self.info_label, default_info, self.info_animation_duration)
        else:
            # Format using templates
            lyrics_text = self.format_template(self.lyrics_format_template, song_data)
            info_text = self.format_template(self.info_format_template, song_data)
            
            # Always update the display, even if the template results in empty text
            # This allows users to intentionally set empty templates to hide text
            self.animate_text_change(self.lyrics_label, lyrics_text, self.lyrics_animation_duration)
            self.animate_text_change(self.info_label, info_text, self.info_animation_duration)

    def load_config(self):
        """Load configuration, fixing corrupted files automatically"""
        config = self._ensure_valid_config()
        self.config = config

        # Layout
        margins = config.get("layout", "layout_margins", fallback="5,5").split(",")
        self.layout_margins = [int(x.strip()) for x in margins]
        self.default_margin = config.getint("layout", "default_margin", fallback=0)
        # Minimum width and auto width
        self.layout_min_width = config.getint("layout", "layout_min_width", fallback=200)
        self.layout_width_auto = config.getboolean("layout", "layout_width_auto", fallback=True)
        self.show_outline = config.getboolean("layout", "show_outline", fallback=False)

        # Layout background color, transparency, border radius, and 4-side padding
        self.layout_bg_color = config.get("layout", "layout_bg_color", fallback="#000000")
        self.layout_bg_transparent = config.getboolean("layout", "layout_bg_transparent", fallback=True)
        self.layout_bg_opacity = config.getfloat("layout", "layout_bg_opacity", fallback=100.0)  # percent
        self.layout_border_radius = config.getint("layout", "layout_border_radius", fallback=0)
        self.layout_padding_top = config.getint("layout", "layout_padding_top", fallback=4)
        self.layout_padding_right = config.getint("layout", "layout_padding_right", fallback=4)
        self.layout_padding_bottom = config.getint("layout", "layout_padding_bottom", fallback=4)
        self.layout_padding_left = config.getint("layout", "layout_padding_left", fallback=4)
        # Lyrics label 4-side padding
        self.lyrics_padding_top = config.getint("lyrics", "padding_top", fallback=4)
        self.lyrics_padding_right = config.getint("lyrics", "padding_right", fallback=4)
        self.lyrics_padding_bottom = config.getint("lyrics", "padding_bottom", fallback=4)
        self.lyrics_padding_left = config.getint("lyrics", "padding_left", fallback=4)
        # Lyrics animation duration
        self.lyrics_animation_duration = config.getint("lyrics", "animation_duration", fallback=250)
        # Info label 4-side padding
        self.info_padding_top = config.getint("info", "padding_top", fallback=4)
        self.info_padding_right = config.getint("info", "padding_right", fallback=4)
        self.info_padding_bottom = config.getint("info", "padding_bottom", fallback=4)
        self.info_padding_left = config.getint("info", "padding_left", fallback=4)
        # Info animation duration
        self.info_animation_duration = config.getint("info", "animation_duration", fallback=250)

        # Lyrics
        if "lyrics" in config:
            lyr_section = config["lyrics"]
        elif "label" in config:
            lyr_section = config["label"]
        else:
            lyr_section = {}

        self.lyrics_text = lyr_section.get("lyrics_text") or lyr_section.get("label_text") or ""
        self.lyrics_color = lyr_section.get("lyrics_color") or lyr_section.get("label_color") or "#FFFFFF"
        self.lyrics_font_family = lyr_section.get("lyrics_font_family") or lyr_section.get("label_font_family") or "Arial"
        self.lyrics_font_size = int(lyr_section.get("lyrics_font_size") or lyr_section.get("label_font_size") or 12)
        self.lyrics_font_weight = int(lyr_section.get("lyrics_font_weight") or lyr_section.get("label_font_weight") or 50)
        self.lyrics_alignment = self._parse_alignment(
            lyr_section.get("lyrics_alignment") or lyr_section.get("label_alignment") or "right"
        )
        
        # Lyrics display format template (new feature)
        self.lyrics_format_template = lyr_section.get("format_template", "{lyrics}")

        # Info
        if "info" in config:
            inf_section = config["info"]
        elif "details" in config:
            inf_section = config["details"]
        else:
            inf_section = {}

        self.info_text = inf_section.get("info_text") or inf_section.get("detail_text") or ""
        self.info_color = inf_section.get("info_color") or inf_section.get("detail_color") or "#FFFFFF"
        self.info_font_family = inf_section.get("info_font_family") or inf_section.get("detail_font_family") or "Arial"
        self.info_font_size = int(inf_section.get("info_font_size") or inf_section.get("detail_font_size") or 10)
        self.info_font_weight = int(inf_section.get("info_font_weight") or inf_section.get("detail_font_weight") or 50)
        self.info_alignment = self._parse_alignment(
            inf_section.get("info_alignment") or inf_section.get("detail_alignment") or "right"
        )
        
        # Info display format template (new feature)
        self.info_format_template = inf_section.get("format_template", "{title} - {artist}")

        # Window
        self.move_enabled = config.getboolean("window", "move_enabled", fallback=False)
        self.always_on_top = config.getboolean("window", "always_on_top", fallback=True)
        self.window_x = config.getint("window", "window_x", fallback=0)
        self.window_y = config.getint("window", "window_y", fallback=0)

        # Excluded Programs
        if "excluded_programs" in config:
            excluded_programs_str = config["excluded_programs"].get("programs", "")
        else:
            excluded_programs_str = ""
        self.excluded_programs = [prog.strip().lower() for prog in excluded_programs_str.split(",") if prog.strip()]

    def _ensure_valid_config(self):
        """Ensure config file exists and is valid, fix if corrupted"""
        # Default configuration content
        default_config = {
            'layout': {
                'layout_margins': '5, 5',
                'default_margin': '0',
                'layout_width_percentage': '100',
                'show_outline': 'False',
                'layout_width_auto': 'True',
                'layout_bg_color': '#000000',
                'layout_bg_transparent': 'True',
                'layout_bg_opacity': '60',
                'layout_border_radius': '7',
                'layout_padding': '5',
                'layout_padding_top': '4',
                'layout_padding_right': '4',
                'layout_padding_bottom': '4',
                'layout_padding_left': '4',
                'layout_min_width': '300'
            },
            'lyrics': {
                'lyrics_text': '...',
                'lyrics_color': '#ffffff',
                'lyrics_font_family': 'Porcelain',
                'lyrics_font_size': '39',
                'lyrics_font_weight': '23',
                'lyrics_alignment': 'right',
                'padding_top': '4',
                'padding_right': '4',
                'padding_bottom': '4',
                'padding_left': '4',
                'animation_duration': '249',
                'format_template': '{lyrics}'
            },
            'info': {
                'info_text': 'Lyricsplash.',
                'info_color': '#ffffff',
                'info_font_family': 'Porcelain',
                'info_font_size': '31',
                'info_font_weight': '46',
                'info_alignment': 'right',
                'padding_top': '4',
                'padding_right': '4',
                'padding_bottom': '4',
                'padding_left': '4',
                'animation_duration': '250',
                'format_template': '{title} - {artist} - {software}'
            },
            'window': {
                'move_enabled': 'False',
                'always_on_top': 'True',
                'window_x': '-2',
                'window_y': '-41'
            },
            'excluded_programs': {
                'programs': ''
            }
        }
        
        def create_fresh_config():
            """Create a fresh config file with default values"""
            config = configparser.ConfigParser()
            for section_name, section_data in default_config.items():
                config.add_section(section_name)
                for key, value in section_data.items():
                    config.set(section_name, key, value)
            
            with open(self.config_path, 'w', encoding='utf-8') as configfile:
                config.write(configfile)
            return config
        
        def backup_corrupted_config():
            """Create a backup of the corrupted config file"""
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = f'{self.config_path}_backup_{timestamp}'
                shutil.copy2(self.config_path, backup_path)
                print(f"Corrupted config backed up to: {backup_path}")
                return backup_path
            except Exception as e:
                print(f"Failed to backup corrupted config: {e}")
                return None
        
        def extract_salvageable_values(corrupted_path):
            """Try to extract any salvageable values from corrupted config"""
            salvaged = {}
            try:
                with open(corrupted_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                current_section = None
                for line in lines:
                    line = line.strip()
                    
                    # Skip empty lines and comments
                    if not line or line.startswith('#') or line.startswith(';'):
                        continue
                    
                    # Check for section headers
                    if line.startswith('[') and line.endswith(']'):
                        section_name = line[1:-1].strip()
                        if section_name in default_config:
                            current_section = section_name
                            if current_section not in salvaged:
                                salvaged[current_section] = {}
                        continue
                    
                    # Check for key-value pairs
                    if current_section and '=' in line:
                        try:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            
                            # Only salvage if it's a valid key for this section
                            if key in default_config[current_section]:
                                salvaged[current_section][key] = value
                                print(f"Salvaged: [{current_section}] {key} = {value}")
                        except Exception:
                            continue
                
                return salvaged
            except Exception as e:
                print(f"Failed to salvage values from corrupted config: {e}")
                return {}
        
        config = configparser.ConfigParser()
        
        # Check if config file exists
        if not os.path.exists(self.config_path):
            print(f"Config file not found. Creating new .conf file at: {self.config_path}")
            config = create_fresh_config()
            print("Config file created successfully.")
            return config
        
        # Try to read the existing config file
        try:
            config.read(self.config_path, encoding='utf-8')
            print("Config file loaded successfully.")
        except (configparser.ParsingError, configparser.Error, UnicodeDecodeError) as e:
            print(f"Config file is corrupted: {e}")
            
            # Backup the corrupted file
            backup_path = backup_corrupted_config()
            
            # Try to salvage values
            salvaged_values = extract_salvageable_values(self.config_path)
            
            print("Creating fresh config file...")
            config = create_fresh_config()
            
            # Apply salvaged values if any
            if salvaged_values:
                print("Applying salvaged values...")
                for section_name, section_data in salvaged_values.items():
                    if config.has_section(section_name):
                        for key, value in section_data.items():
                            config.set(section_name, key, value)
                
                # Save the config with salvaged values
                with open(self.config_path, 'w', encoding='utf-8') as configfile:
                    config.write(configfile)
                print("Config file recreated with salvaged values.")
            else:
                print("Config file recreated with default values.")
            
            return config
        except Exception as e:
            print(f"Unexpected error reading config: {e}")
            print("Creating fresh config file...")
            config = create_fresh_config()
            print("Fresh config file created.")
            return config
        
        # Config file is valid, check for missing fields
        config_modified = False
        
        for section_name, section_data in default_config.items():
            # Add section if it doesn't exist
            if not config.has_section(section_name):
                config.add_section(section_name)
                config_modified = True
                print(f"Added missing section: [{section_name}]")
            
            # Add missing options within each section
            for key, default_value in section_data.items():
                if not config.has_option(section_name, key):
                    config.set(section_name, key, default_value)
                    config_modified = True
                    print(f"Added missing option: {key} = {default_value} in [{section_name}]")
        
        # Save the config file if any modifications were made
        if config_modified:
            try:
                with open(self.config_path, 'w', encoding='utf-8') as configfile:
                    config.write(configfile)
                print("Config file updated with missing fields.")
            except Exception as e:
                print(f"Failed to save updated config: {e}")
        else:
            print("Config file is complete, no updates needed.")
        
        return config


    def _parse_alignment(self, value):
        return {
            "right": Qt.AlignRight,
            "center": Qt.AlignCenter,
            "left": Qt.AlignLeft,
            "bottom": Qt.AlignBottom
        }.get(value.lower(), Qt.AlignRight)

    def window(self):
        self.win = QWidget()
        self.win.setAttribute(Qt.WA_TranslucentBackground)
        self.win.setStyleSheet("background-color: rgba(0,0,0,0);")
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.win.setWindowFlags(flags)

        screen_geo = QGuiApplication.primaryScreen().geometry()
        self.win.resize(screen_geo.width(), screen_geo.height())


        # Calculate layout width based on auto/min width
        if getattr(self, 'layout_width_auto', True):
            layout_width = None  # Will use sizeHint, but enforce min width
        else:
            layout_width = self.layout_min_width

        self.layout = QVBoxLayout()
        self.layout.setAlignment(Qt.AlignRight | Qt.AlignBottom)
        self.layout.setContentsMargins(0, 0, *self.layout_margins)

        # Create a container widget for the layout outline
        self.layout_container = QWidget()
        if layout_width is not None:
            self.layout_container.setFixedWidth(layout_width)
            self.layout_container.setMinimumWidth(self.layout_min_width)
        else:
            self.layout_container.setMinimumWidth(self.layout_min_width)
            self.layout_container.setMaximumWidth(16777215)  # Qt default max width
            self.layout_container.setSizePolicy(self.layout_container.sizePolicy().horizontalPolicy(), self.layout_container.sizePolicy().verticalPolicy())
        # Set background color, transparency, border radius (no QSS padding here)
        if self.layout_bg_transparent:
            bg_style = "background-color: rgba(0,0,0,0);"
        else:
            col = QColor(self.layout_bg_color)
            alpha = int(255 * (self.layout_bg_opacity / 100.0))
            bg_style = f"background-color: rgba({col.red()}, {col.green()}, {col.blue()}, {alpha});"
        border_radius_style = f"border-radius: {self.layout_border_radius}px;" if self.layout_border_radius else ""
        style = f"{bg_style} {border_radius_style}"
        if self.show_outline:
            self.layout_container.setStyleSheet(f"border: 2px solid {self.lyrics_color}; {style}")
        else:
            self.layout_container.setStyleSheet(style)

        container_layout = QVBoxLayout()
        container_layout.setContentsMargins(5, 5, 5, 5)  # Small margin inside the outline
        self.layout_container.setLayout(container_layout)
        # Set layout container margins to simulate padding (after setLayout)
        self.layout_container.layout().setContentsMargins(
            self.layout_padding_left,
            self.layout_padding_top,
            self.layout_padding_right,
            self.layout_padding_bottom
        )

        self.lyrics_label = QLabel(self.lyrics_text)
        self.lyrics_label.setStyleSheet(
            f"color: {self.lyrics_color}; font-weight: {self.lyrics_font_weight}px; background: transparent; border: none;"
            f"padding: {self.lyrics_padding_top}px {self.lyrics_padding_right}px {self.lyrics_padding_bottom}px {self.lyrics_padding_left}px;"
        )
        self.lyrics_label.setFont(QFont(self.lyrics_font_family, self.lyrics_font_size))
        self.lyrics_label.setAlignment(self.lyrics_alignment)

        self.info_label = QLabel(self.info_text)
        self.info_label.setStyleSheet(
            f"color: {self.info_color}; font-weight: {self.info_font_weight}px; background: transparent; border: none;"
            f"padding: {self.info_padding_top}px {self.info_padding_right}px {self.info_padding_bottom}px {self.info_padding_left}px;"
        )
        self.info_label.setFont(QFont(self.info_font_family, self.info_font_size))
        self.info_label.setAlignment(self.info_alignment)

        container_layout.addWidget(self.lyrics_label)
        container_layout.addWidget(self.info_label)
        self.layout.addWidget(self.layout_container)
        self.win.setLayout(self.layout)

        if self.window_x or self.window_y:
            self.win.move(self.window_x, self.window_y)

        # Tray icon & menu
        self.tray = QSystemTrayIcon(self.win)
        self.tray.setIcon(QIcon(str(self.resource_path("icon.ico"))))
        self.menu = QMenu()

        # Create toggle action
        self.toggle_action = QAction("Hide", self.win)  # Default to "Hide" since window starts visible
        self.toggle_action.triggered.connect(self.toggle_visibility)
        self.menu.addAction(self.toggle_action)

        for name, handler in [
            # ("Activation", self.show_activation_window),
            ("Settings", self.show_settings_window),
            ("Check for updates", self.show_update_dialog),
            ("Exit", self.quit_app)
        ]:
            act = QAction(name, self.win)
            act.triggered.connect(handler)
            self.menu.addAction(act)

        self.tray.setContextMenu(self.menu)
        self.tray.show()

        # Enable dragging if Move checked
        def on_press(e):
            if self.move_enabled and e.button() == Qt.LeftButton:
                self.win._drag_pos = e.globalPos()
                e.accept()

        def on_move(e):
            if getattr(self.win, "_drag_pos", None) and (e.buttons() & Qt.LeftButton) and self.move_enabled:
                delta = e.globalPos() - self.win._drag_pos
                self.win.move(self.win.pos() + delta)
                self.window_x, self.window_y = self.win.pos().x(), self.win.pos().y()
                self.win._drag_pos = e.globalPos()
                e.accept()

        self.win.mousePressEvent = on_press
        self.win.mouseMoveEvent = on_move

        return self.win

    def start(self):
        self.win.show()
        self.toggle_action.setText("Hide")

    def stop(self):
        self.win.hide()
        self.toggle_action.setText("Show")

    def toggle_visibility(self):
        if self.win.isVisible():
            self.stop()
        else:
            self.start()

    def quit_app(self):
        self.tray.hide()
        self.win.hide()
        QApplication.quit()

    def pick_color(self, btn):
        col = QColorDialog.getColor(btn.palette().button().color(), self.settings_win, "Select Color")
        if col.isValid():
            btn.setStyleSheet(f"background-color: {col.name()};")
            btn.setProperty("selected_color", col.name())
            self.save_settings()

    def create_collapsible_section(self, title, is_expanded=False):
        """Create a collapsible section with a header button and content area"""
        group_box = QGroupBox()
        
        # Create header button with modern styling
        header_btn = QPushButton(f"{'🔽' if is_expanded else '🔼'}      {title}")
        header_btn.setObjectName("section_header")
        header_btn.setFont(QFont("Calibri", 20, QFont.DemiBold))

        
        # Create content area
        content_widget = QWidget()
        content_widget.setObjectName("content_widget")
        content_layout = QFormLayout()
        content_layout.setContentsMargins(24, 20, 24, 24)
        content_layout.setVerticalSpacing(20)
        content_layout.setHorizontalSpacing(16)
        content_widget.setLayout(content_layout)
        content_widget.setVisible(is_expanded)
        
        # Toggle function
        def toggle_section():
            visible = content_widget.isVisible()
            content_widget.setVisible(not visible)
            header_btn.setText(f"{'🔼' if visible else '🔽'}      {title}")
            # Update header property for CSS styling
            header_btn.setProperty("collapsed", "true" if not visible else "false")
            header_btn.style().unpolish(header_btn)
            header_btn.style().polish(header_btn)
        
        header_btn.clicked.connect(toggle_section)
        
        # Set initial header property
        header_btn.setProperty("collapsed", "true" if not is_expanded else "false")
        
        # Main layout for the group
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(header_btn)
        main_layout.addWidget(content_widget)
        group_box.setLayout(main_layout)
        
        return group_box, content_layout
    
    def show_activation_window(self):
        if hasattr(self, "activation_win") and self.activation_win.isVisible():
            self.activation_win.raise_()
            return

        self.activation_win = QWidget()
        self.activation_win.setWindowTitle("Lyricsplash Activation")
        self.activation_win.setWindowIcon(QIcon(str(self.resource_path("icon.ico"))))
        self.activation_win.adjustSize()
        self.activation_win.setFont(QFont("Calibri", 18))
        
        # Modern gradient background with card-style design
        self.activation_win.setStyleSheet("""
            QWidget {
                border-radius: 12px;
                font-family: Calibri;
            }
            QWidget#card {
                background: rgba(255, 255, 255, 0.95);
                border-radius: 16px;
                border: 1px solid rgba(255, 255, 255, 0.3);
            }
            QLabel#title {
                color: black;
                font-size: 33px;
                font-weight: 600;
                margin: 0px;
                padding: 12px 0px;
            }
            QLabel#subtitle {
                color: #666666;
                font-size: 21px;
                font-weight: 400;
                margin: 0px;
                padding: 8px 0px;
            }
            QLabel#input_label {
                color: black;
                font-size: 20px;
                font-weight: 500;
                margin: 0px;
                padding: 8px 0px;
            }
            QLineEdit {
                padding: 18px;
                border: 2px solid #e2e8f0;
                border-radius: 10px;
                font-size: 21px;
                font-weight: 400;
                background: white;
                color: black;
                selection-background-color: #667eea;
                min-height: 20px;
                border-color: #666666;
            }
            QLineEdit:focus {
                border-color: black;
                outline: none;
            }
            QPushButton {
                padding: 14px 28px;
                border: none;
                border-radius: 8px;
                font-size: 21px;
                font-weight: 600;
                min-width: 110px;
            }
            QPushButton#primary {
                background: rgba(226, 232, 240, 0.8);
                color: black;
                border: 1px solid #cbd5e0;
            }
            QPushButton#secondary {
                background: rgba(226, 232, 240, 0.8);
                color: black;
                border: 1px solid #cbd5e0;
            }
            QPushButton#secondary:hover {
                background: white;
                border-color: #a0aec0;
            }
            QPushButton#primary:hover {
                background: white;
                border-color: #a0aec0;
            }
            QPushButton#danger {
                background: red;
                color: white;
                border: 1px solid white;
            }
            QPushButton:disabled {
                background: #cccccc;
                color: #888888;
                border: 1px solid #aaaaaa;
            }

        """)

        # Main container with padding
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(24, 24, 24, 24)
        main_layout.setSpacing(0)

        # Card container
        card = QWidget()
        card.setObjectName("card")
        card_layout = QVBoxLayout()
        card_layout.setContentsMargins(40, 40, 40, 40)
        card_layout.setSpacing(24)

        # Header section
        header_layout = QVBoxLayout()
        # header_layout.setSpacing(8)
        # header_layout.addSpacing(8)
        
        title = QLabel("Activate Lyricsplash")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(title)

        subtitle = QLabel("Enter your product key to activate Lyricsplash")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(subtitle)
        
        card_layout.addLayout(header_layout)

        activated = keyring.get_password(self.service_name, self.service_username)

        if not activated:
            # Input section
            input_layout = QVBoxLayout()
            input_layout.setSpacing(12)
            
            key_label = QLabel("Product Key")
            key_label.setObjectName("input_label")
            input_layout.addWidget(key_label)
            
            self.product_key_input = QLineEdit()
            self.product_key_input.setPlaceholderText("Enter your product key here...")
            input_layout.addWidget(self.product_key_input)
            
            card_layout.addLayout(input_layout)

            # Add some spacing before buttons
            card_layout.addSpacing(16)
        else:
            title = QLabel("Your Lyricsplash is already activated")
            title.setObjectName("title")
            title.setAlignment(Qt.AlignCenter)
            header_layout.addWidget(title)


        # Button section
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        
        close_btn = QPushButton("Cancel")
        close_btn.setObjectName("secondary")
        close_btn.clicked.connect(self.activation_win.close)
        btn_layout.addWidget(close_btn)
        
        btn_layout.addStretch()

        if not activated:
            activate_btn = QPushButton("Activate")
            activate_btn.setObjectName("primary")
            activate_btn.clicked.connect(self.validate_product_key)
            btn_layout.addWidget(activate_btn)
        else:
            activate_btn = QPushButton("Deactivate")
            activate_btn.setObjectName("danger")
            activate_btn.clicked.connect(self.deactivate)
            btn_layout.addWidget(activate_btn)

        card_layout.addLayout(btn_layout)

        card.setLayout(card_layout)
        main_layout.addWidget(card)
        self.activation_win.setLayout(main_layout)
        
        # Center the window
        self.activation_win.move(
            QGuiApplication.primaryScreen().geometry().center() - 
            self.activation_win.rect().center()
        )
        
        self.activation_win.show()

    # ================= Update Dialog ==================
    def show_update_dialog(self):
        if hasattr(self, 'update_win') and self.update_win.isVisible():
            self.update_win.raise_()
            return

        self.update_win = QWidget()
        self.update_win.setWindowTitle("Lyricsplash Update")
        self.update_win.setWindowIcon(QIcon(str(self.resource_path("icon.ico"))))
        self.update_win.adjustSize()
        self.update_win.setFont(QFont("Calibri", 18))
        
        # Modern gradient background with card-style design (same as activation)
        self.update_win.setStyleSheet("""
            QWidget {
                border-radius: 12px;
                font-family: Calibri;
            }
            QWidget#card {
                background: rgba(255, 255, 255, 0.95);
                border-radius: 16px;
                border: 1px solid rgba(255, 255, 255, 0.3);
            }
            QLabel#title {
                color: black;
                font-size: 33px;
                font-weight: 600;
                margin: 0px;
                padding: 12px 0px;
            }
            QLabel#subtitle {
                color: #666666;
                font-size: 21px;
                font-weight: 400;
                margin: 0px;
                padding: 8px 0px;
            }
            QLabel#status {
                color: black;
                font-size: 18px;
                font-weight: 500;
                margin: 0px;
                padding: 8px 0px;
            }
            QLabel#version {
                color: #666666;
                font-size: 16px;
                font-weight: 400;
                margin: 0px;
                padding: 4px 0px;
            }
            QPushButton {
                padding: 14px 28px;
                border: none;
                border-radius: 8px;
                font-size: 21px;
                font-weight: 600;
                min-width: 110px;
            }
            QPushButton#primary {
                background: rgba(226, 232, 240, 0.8);
                color: black;
                border: 1px solid #cbd5e0;
            }
            QPushButton#secondary {
                background: rgba(226, 232, 240, 0.8);
                color: black;
                border: 1px solid #cbd5e0;
            }
            QPushButton#secondary:hover {
                background: white;
                border-color: #a0aec0;
            }
            QPushButton#primary:hover {
                background: white;
                border-color: #a0aec0;
            }
            QPushButton:disabled {
                background: #cccccc;
                color: #888888;
                border: 1px solid #aaaaaa;
            }
            QHBoxLayout#btn_layout {
                display: flex;
                justify-content: center;
                align-items: center;
            }
        """)

        # Main container with padding
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(24, 24, 24, 24)
        main_layout.setSpacing(0)

        # Card container
        card = QWidget()
        card.setObjectName("card")
        card_layout = QVBoxLayout()
        card_layout.setContentsMargins(40, 40, 40, 40)
        card_layout.setSpacing(24)

        # Header section
        header_layout = QVBoxLayout()
        
        title = QLabel("Check for updates")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(title)

        subtitle = QLabel("Please visit the github page of lyricsplash to check for updates")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(subtitle)
        
        card_layout.addLayout(header_layout)

        # Version info section
        version_layout = QVBoxLayout()
        version_layout.setSpacing(8)
        
        current_version = QLabel(f"Current version: {APP_VERSION}")
        current_version.setObjectName("version")
        current_version.setAlignment(Qt.AlignCenter)
        version_layout.addWidget(current_version)
        
        self.update_status_lbl = QLabel("github://praveenk/lyricspalsh")
        self.update_status_lbl.setObjectName("status")
        self.update_status_lbl.setAlignment(Qt.AlignCenter)
        version_layout.addWidget(self.update_status_lbl)
        
        card_layout.addLayout(version_layout)

        # Button section
        self.btn_layout = QHBoxLayout()
        self.btn_layout.setObjectName("btn_layout")
        self.btn_layout.setSpacing(12)
        self.btn_layout.addStretch() 

        close_btn = QPushButton("Close")
        close_btn.setObjectName("secondary")
        close_btn.clicked.connect(self.update_win.close)
        self.btn_layout.addWidget(close_btn)

        self.btn_layout.addStretch() 

        card_layout.addLayout(self.btn_layout)

        card.setLayout(card_layout)
        main_layout.addWidget(card)
        self.update_win.setLayout(main_layout)
        
        # Center the window
        self.update_win.move(
            QGuiApplication.primaryScreen().geometry().center() - 
            self.update_win.rect().center()
        )
        
        self.update_win.show()

    def deactivate(self):
        keyring.delete_password(self.service_name, self.service_username)
        self.show_styled_message_box(self.activation_win, "Deactivation Successful", "Lyricsplash has been deactivated.", "info")
        self.signal_bus.request_action.emit("", False)
        self.activation_win.close()


    def show_styled_message_box(self, parent, title, message, msg_type="info"):
        """Show a custom styled message box with aligned icon and text"""
        msg_box = QMessageBox(parent)
        msg_box.setWindowTitle(title)
        msg_box.setText(message)

        # Set message box type
        if msg_type == "warning":
            msg_box.setIcon(QMessageBox.Warning)
        elif msg_type == "error":
            msg_box.setIcon(QMessageBox.Critical)
        else:
            msg_box.setIcon(QMessageBox.Information)

        # Align icon vertically with text
        # QMessageBox doesn't expose direct layout changes,
        # so we access its internal layout after creation
        layout = msg_box.layout()
        if layout:
            # Force middle alignment for icon cell
            layout.itemAtPosition(0, 0).widget().setAlignment(Qt.AlignVCenter | Qt.AlignHCenter)

        # Style
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: white;
                border: 2px solid #e2e8f0;
                border-radius: 12px;
                font-family: Calibri;
                font-size: 20px;
            }
            QMessageBox QLabel {
                color: #2d3748;
                font-size: 20px;
                font-weight: 500;
                padding: 0px 16px;
                margin: 5px 8px;
            }
            /* Make OK button border-only */
            QMessageBox QPushButton {
                background-color: transparent;
                color: #4a5568;
                border: 2px solid #cbd5e0;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                padding: 10px 20px;
                min-width: 80px;
                margin: 0px 4px;
            }
            /* For other buttons, you can add color class */
            QMessageBox QPushButton[colored="true"] {
                background-color: #667eea;
                color: white;
                border: 2px solid #667eea;
            }
            QMessageBox QPushButton[colored="true"]:hover {
                background-color: #5a6fd8;
                border-color: #5a6fd8;
            }
            QMessageBox QPushButton[colored="true"]:pressed {
                background-color: #4c5bc4;
                border-color: #4c5bc4;
            }
        """)

        msg_box.setMinimumSize(350, 150)
        
        # Mark all buttons except OK as colored
        for btn in msg_box.buttons():
            if btn.text().lower() != "ok":
                btn.setProperty("colored", True)
                btn.style().unpolish(btn)
                btn.style().polish(btn)

        return msg_box.exec_()


    def validate_product_key(self):
        """Validate the entered product key"""
        key = self.product_key_input.text().strip()
        if not key:
            self.show_styled_message_box(self.activation_win, "Empty Product Key", "Please enter a product key before validating.", "warning")
            return
        
        # Store the activation window reference for use in the slot
        self._pending_activation_win = self.activation_win
        self._stored_product_key = key  # Store the key for later use
        
        # Show verifying state immediately
        self.show_verifying_state()
        
        # Use QTimer to delay the actual validation so UI updates first
        QTimer.singleShot(100, self.start_validation)

    def show_verifying_state(self):
        """Show verifying state in the activation window"""
        if not hasattr(self, 'activation_win') or not self.activation_win.isVisible():
            return
            
        # Find the card widget and its layout
        card = self.activation_win.findChild(QWidget, "card")
        if not card:
            return
            
        card_layout = card.layout()
        if not card_layout:
            return
            
        # Clear ALL existing content from the card layout
        while card_layout.count():
            item = card_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
            elif item.layout():
                # If it's a layout, recursively delete its contents
                self._clear_layout(item.layout())
                
        # Recreate the header section
        header_layout = QVBoxLayout()
        
        title = QLabel("Activate Lyricsplash")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(title)

        subtitle = QLabel("Enter your product key to activate Lyricsplash")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(subtitle)
        
        card_layout.addLayout(header_layout)
                    
        # Create verifying content
        verifying_layout = QVBoxLayout()
        verifying_layout.setSpacing(16)
        verifying_layout.setAlignment(Qt.AlignCenter)
        
        verifying_label = QLabel("Verifying product key...")
        verifying_label.setObjectName("subtitle")
        verifying_label.setAlignment(Qt.AlignCenter)
        verifying_layout.addWidget(verifying_label)
        
        # Add verifying content to card
        card_layout.addLayout(verifying_layout)
        
        # Store references for later restoration
        self._verifying_layout = verifying_layout
        self._verifying_label = verifying_label

    def _clear_layout(self, layout):
        """Recursively clear all items from a layout"""
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().setParent(None)
                elif item.layout():
                    self._clear_layout(item.layout())

    def start_validation(self):
        """Start the actual validation process"""
        # Trigger async authentication
        self.signal_bus.request_action.emit(self._stored_product_key, True)

    def restore_activation_ui(self):
        """Restore the normal activation UI after verification"""
        if not hasattr(self, 'activation_win') or not self.activation_win.isVisible():
            return
            
        # Find the card widget and its layout
        card = self.activation_win.findChild(QWidget, "card")
        if not card:
            return
            
        card_layout = card.layout()
        if not card_layout:
            return
            
        # Clear ALL existing content from the card layout
        while card_layout.count():
            item = card_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
            elif item.layout():
                # If it's a layout, recursively delete its contents
                self._clear_layout(item.layout())
            
        # Check if already activated
        activated = keyring.get_password(self.service_name, self.service_username)
        
        # Recreate the header section
        header_layout = QVBoxLayout()
        
        title = QLabel("Activate Lyricsplash")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(title)

        subtitle = QLabel("Enter your product key to activate Lyricsplash")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(subtitle)
        
        card_layout.addLayout(header_layout)
        
        if not activated:
            # Recreate input section
            input_layout = QVBoxLayout()
            input_layout.setSpacing(12)
            
            key_label = QLabel("Product Key")
            key_label.setObjectName("input_label")
            input_layout.addWidget(key_label)
            
            self.product_key_input = QLineEdit()
            self.product_key_input.setPlaceholderText("Enter your product key here...")
            # Restore the previously entered key
            if hasattr(self, '_stored_product_key'):
                self.product_key_input.setText(self._stored_product_key)
            input_layout.addWidget(self.product_key_input)
            
            card_layout.addLayout(input_layout)
            card_layout.addSpacing(16)
        else:
            # Show activation status if already activated
            status_label = QLabel("Your Lyricsplash is already activated")
            status_label.setObjectName("subtitle")
            status_label.setAlignment(Qt.AlignCenter)
            card_layout.addWidget(status_label)
        
        # Recreate button section
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        
        close_btn = QPushButton("Cancel")
        close_btn.setObjectName("secondary")
        close_btn.clicked.connect(self.activation_win.close)
        btn_layout.addWidget(close_btn)
        
        btn_layout.addStretch()

        if not activated:
            activate_btn = QPushButton("Activate")
            activate_btn.setObjectName("primary")
            activate_btn.clicked.connect(self.validate_product_key)
            btn_layout.addWidget(activate_btn)
        else:
            activate_btn = QPushButton("Deactivate")
            activate_btn.setObjectName("danger")
            activate_btn.clicked.connect(self.deactivate)
            btn_layout.addWidget(activate_btn)

        card_layout.addLayout(btn_layout)


    def on_auth_response(self, response):
        # Called in the main thread when authentication result is ready
        win = self._pending_activation_win or getattr(self, 'activation_win', None)
        if not win:
            win = self
            
        # Restore the normal UI first
        self.restore_activation_ui()
            
        if response and isinstance(response, dict):
            status = response.get("status", "Unknown")
            message = response.get("message", "No message")
            msg_type = "error" if "error" in status.lower() or "fail" in status.lower() else "info"
            self.show_styled_message_box(win, status, message, msg_type)
            if response.get("close", False) and hasattr(self, 'activation_win'):
                self.activation_win.close()
        else:
            self.show_styled_message_box(win, "Error", "Invalid response received from authentication system.", "error")
        self._pending_activation_win = None

    def init_settings_ui(self):
        """Build settings UI once at startup."""
        self.settings_win = QWidget()
        self.settings_win.setWindowTitle("Lyricsplash Settings")
        self.settings_win.setWindowIcon(QIcon(str(self.resource_path("icon.ico"))))
        self.settings_win.setMinimumSize(1000, 650)  # Increased minimum width and height
        self.settings_win.setFont(QFont("Calibri", 16))
        
        # Embedded stylesheet (from settings_styles.qss)
        settings_styles = """
/* ================================================================
   LYRICSPLASH SETTINGS WINDOW STYLES
   
   This file contains all the styling for the settings window.
   Each section below explains which PyQt5 elements are affected.
   ================================================================ */

/* ================================================================
   MAIN WIDGET - The root container of the settings window
   Affects: The main settings window background and default font
   ================================================================ */
@import url('https://fonts.googleapis.com/css2?family=Karla:ital,wght@0,200..800;1,200..800&display=swap');
QWidget {
    background: #f8f8f8;      /* Light gray background for entire window */
    font-family: "Karla", sans-serif;    /* Default font family for all text */
    border-radius: 6px;       /* Rounded corners for the window */
}

/* ================================================================
   SCROLL AREA - The scrollable container holding all settings
   Affects: The main scrollable area that contains all settings sections
   ================================================================ */
QScrollArea {
    border: none;             /* Remove default border around scroll area */
    background: transparent;  /* Make scroll area background transparent */
}

/* ================================================================
   SCROLL BAR - The vertical scroll bar when content overflows
   Affects: The scroll bar appearance and behavior
   ================================================================ */
QScrollBar:vertical {
    background: #e0e0e0;      /* Light gray background for scroll track */
    width: 8px;               /* Narrow scroll bar width */
    border-radius: 4px;       /* Rounded scroll bar */
    margin: 0px;              /* No margins around scroll bar */
}

QScrollBar::handle:vertical {
    background: #b0b0b0;      /* Medium gray for scroll handle */
    border-radius: 4px;       /* Rounded scroll handle */
    min-height: 20px;         /* Minimum height for draggable handle */
}

QScrollBar::handle:vertical:hover {
    background: #909090;      /* Darker gray when hovering over handle */
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;              /* Hide scroll arrows (modern flat design) */
}

/* ================================================================
   LABELS - Default styling for all text labels
   Affects: All QLabel elements (field names, descriptions, etc.)
   ================================================================ */
QLabel {
    color: black;             /* Black text color for readability */
    font-size: 18px;          /* Standard text size for labels */
    font-weight: 500;         /* Medium font weight */
    margin: 0px;              /* No extra margins */
    padding: 0px;             /* No extra padding */
}

/* ================================================================
   SPECIAL LABELS - Specific styling for title and subtitle
   Affects: The main window title and subtitle at the top
   ================================================================ */
QLabel#title_label {
    font-size: 36px;          /* Large title text */
    font-weight: 700;         /* Bold font weight for prominence */
    color: black;             /* Black text for contrast */
    margin: 0px;              /* No extra margins */
    padding: 0px;             /* No extra padding */
    font-family: Calibri;     /* Consistent font family */
}

QLabel#subtitle_label {
    font-size: 22px;          /* Medium subtitle text */
    font-weight: 400;         /* Normal font weight */
    color: black;             /* Black text for readability */
    margin: 0px;              /* No extra margins */
    padding: 0px;             /* No extra padding */
    font-family: Calibri;     /* Consistent font family */
}

/* ================================================================
   TEXT INPUT FIELDS - Styling for all text input areas
   Affects: QLineEdit elements (text boxes for lyrics text, 
            info text, excluded programs, format templates, etc.)
   ================================================================ */
QLineEdit {
    padding: 14px 18px;       /* Inner spacing for comfortable text entry */
    border: 1px solid #b0b0b0; /* Light gray border */
    border-radius: 6px;       /* Rounded corners */
    font-size: 17px;          /* Readable text size */
    background: white;        /* White background for input area */
    color: #202020;           /* Dark gray text color */
    font-weight: 400;         /* Normal font weight */
    min-height: 20px;         /* Minimum height for usability */
    margin: 4px 0px;          /* Small vertical margins */
}

QLineEdit:focus {
    border: 1px solid #404040; /* Darker border when focused/clicked */
    outline: none;            /* Remove default focus outline */
}

/* ================================================================
   NUMERIC INPUTS AND DROPDOWNS - Styling for number controls and lists
   Affects: QSpinBox (font sizes, weights, padding values, etc.)
            QComboBox (font selection, alignment dropdowns, etc.)
   ================================================================ */
QSpinBox, QComboBox {
    padding: 12px 18px;       /* Inner spacing for comfortable interaction */
    border: 1px solid #b0b0b0; /* Light gray border */
    border-radius: 6px;       /* Rounded corners */
    font-size: 17px;          /* Readable text size */
    background: white;        /* White background */
    color: #202020;           /* Dark gray text */
    min-width: 120px;         /* Minimum width for usability */
    min-height: 20px;         /* Minimum height for click targets */
    margin: 4px 0px;          /* Small vertical margins */
}

QSpinBox:focus, QComboBox:focus {
    border: 1px solid #404040; /* Darker border when focused */
}

QComboBox QAbstractItemView {
    background-color: white;
    color: black;
    alternate-background-color: white;          /* disable alternating row colors */
    selection-background-color: #e6e6e6;
    selection-color: black;
    outline: none;
}

QComboBox, QFontComboBox {
    background-color: white;                   /* optional: main combobox appearance */
    color: black;
}



/* Dropdown arrow styling for combo boxes */
QComboBox::drop-down {
    border: none;             /* Remove default dropdown border */
    width: 20px;              /* Width of dropdown button area */
}

QComboBox::down-arrow {
    /* Creates a CSS triangle pointing down */
    display: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 5px solid #404040;
    width: 0px;
    height: 0px;
}

/* ================================================================
   CHECKBOXES - Styling for boolean options
   Affects: QCheckBox elements (Move enabled, Always on top, 
            Auto layout width, Transparent background, etc.)
   ================================================================ */
QCheckBox {
    font-size: 17px;          /* Text size for checkbox labels */
    color: #202020;           /* Dark gray text color */
    spacing: 10px;            /* Space between checkbox and label text */
    padding: 6px 2px;         /* Inner padding around checkbox */
    margin: 4px 0px;          /* Small vertical margins */
}

/* Checkbox visual indicator (the actual checkbox square) */
QCheckBox::indicator {
    width: 18px;              /* Width of checkbox square */
    height: 18px;             /* Height of checkbox square */
    border: 1px solid #606060; /* Gray border for unchecked state */
    border-radius: 4px;       /* Slightly rounded checkbox corners */
    background: white;        /* White background when unchecked */
}

QCheckBox::indicator:checked {
    background: #404040;      /* Dark gray background when checked */
    border-color: #404040;    /* Matching border color when checked */
}

QCheckBox::indicator:unchecked {
    border: 1px solid #606060; /* Gray border when unchecked */
    background: white;        /* White background when unchecked */
}

/* ================================================================
   BUTTONS - Default styling for all clickable buttons
   Affects: QPushButton elements (general buttons, not specialized ones)
   ================================================================ */
QPushButton {
    padding: 12px 20px;       /* Inner spacing for comfortable clicking */
    border: 1px solid #404040; /* Dark gray border */
    border-radius: 6px;       /* Rounded corners */
    font-size: 17px;          /* Readable text size */
    font-weight: 500;         /* Medium font weight */
    background: #f2f2f2;      /* Light gray background */
    color: #202020;           /* Dark gray text */
    min-width: 120px;         /* Minimum width for usability */
    margin: 4px 2px;          /* Small margins around buttons */
}

QPushButton:hover {
    background: #e6e6e6;      /* Slightly darker on hover */
}

QPushButton:pressed {
    background: #d0d0d0;      /* Even darker when clicked */
}

/* ================================================================
   COLOR PICKER BUTTONS - Special styling for color selection buttons
   Affects: Color picker buttons (lyrics color, info color, layout bg color)
   Background color is set dynamically in Python code
   ================================================================ */
QPushButton#color_button {
    border: 3px solid black;  /* Thick black border for visibility */
    border-radius: 4px;       /* Rounded corners */
    min-width: 50px;          /* Square-ish minimum width */
    min-height: 40px;         /* Square-ish minimum height */
    padding: 10px;            /* Inner padding for click area */
    margin: 4px 2px;          /* Small margins around button */
}

QPushButton#color_button:hover {
    border-color: black;      /* Keep black border on hover */
}

/* ================================================================
   INFO/HELP BUTTONS - Small circular buttons with "?" symbol
   Affects: Help buttons next to format template fields
   ================================================================ */
QPushButton#info_button {
    background: #f0f0f0;      /* Light gray background for visibility */
    color: #404040;           /* Dark gray text/symbol for contrast */
    border: 2px solid #404040; /* Dark border for definition */
    font-weight: 600;         /* Bold font for "?" symbol */
    min-width: 40px;          /* Small square button */
    max-width: 40px;          /* Fixed width to keep square */
    padding: 10px;            /* Inner padding */
    border-radius: 4px;       /* Rounded corners */
}

QPushButton#info_button:hover {
    background: #e0e0e0;      /* Slightly darker on hover */
    border-color: #202020;    /* Darker border on hover */
}

/* ================================================================
   SAVE BUTTONS - Special styling for save/action buttons
   Affects: Save button for excluded programs list
   ================================================================ */
QPushButton#save_button {
    background: #202020;      /* Very dark gray/black background */
    color: rgb(0, 0, 0);             /* White text for contrast */
    border: none;             /* No border for clean look */
    font-weight: 600;         /* Bold text */
    border-radius: 6px;       /* Rounded corners */
    padding: 12px 20px;       /* Comfortable padding for clicking */
    border: 2px solid black;
}

QPushButton#save_button:hover {
    background: #404040;      /* Lighter gray on hover */
}

QPushButton#save_button:pressed {
    background: #606060;      /* Even lighter when clicked */
}

/* ================================================================
   COLLAPSIBLE SECTIONS - Styling for expandable/collapsible groups
   These create the "Layout Settings", "Lyrics Settings", "Info Settings" sections
   ================================================================ */

/* Container for each collapsible section */
QGroupBox {
    border: none;             /* No default border */
    margin: 8px 0px;          /* Vertical spacing between sections */
    background: white;        /* White background for section */
    border-radius: 4px;       /* Rounded corners */
    border: 2px solid black;  /* Black border around entire section */
}

/* ================================================================
   SECTION HEADERS - Clickable headers that expand/collapse sections
   Affects: The header buttons for "Layout Settings", "Lyrics Settings", etc.
   ================================================================ */
QPushButton#section_header {
    text-align: left;        
    font-weight: 600;         
    background: white;       
    border: none;            
    border-radius: 4px 4px 0px 0px; 
    color: black;             
    font-size: 20px;         
    font-family: Calibri;     
    letter-spacing: 1px;   
    cursor: pointer;  
}

QPushButton#section_header:hover {
    background: rgba(0, 0, 0, 0.05); /* Very light gray on hover */
}


QPushButton#section_header:pressed {
    background: rgba(0, 0, 0, 0.1);  /* Slightly darker when clicked */
}

/* Header styling when section is collapsed (closed) */
QPushButton#section_header[collapsed="true"] {
    border-radius: 4px;       /* All corners rounded when collapsed */
}

/* Header styling when section is expanded (open) */
QPushButton#section_header[collapsed="false"] {
    border-radius: 4px 4px 0px 0px; /* Top corners only when expanded */
}

/* ================================================================
   SECTION CONTENT - The content area inside each collapsible section
   Affects: The area containing the actual settings controls
   ================================================================ */
QWidget#content_widget {
    background: white;        /* White background for content area */
    border-radius: 0px 0px 4px 4px; /* Rounded bottom corners only */
    padding: 0px;             /* No padding (handled by layout) */
    margin: 0px;              /* No margins */
}

/* ================================================================
   EXCLUDED PROGRAMS LABEL - Special spacing for excluded programs
   Affects: The "Excluded programs:" label specifically
   ================================================================ */
QLabel#excluded_programs_label {
    margin-top: 16px;         /* Add 16px top margin for spacing */
}
"""
        self.settings_win.setStyleSheet(settings_styles)


        # Main container
        main_container = QWidget()
        main_container_layout = QVBoxLayout()
        main_container_layout.setContentsMargins(24, 24, 24, 24)
        main_container_layout.setSpacing(20)

        # Header
        header_layout = QVBoxLayout()
        header_layout.setSpacing(8)
        
        title_label = QLabel("Lyricsplash Settings")
        title_label.setObjectName("title_label")
        title_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(title_label)
        
        subtitle_label = QLabel("Customize your lyrics display experience")
        subtitle_label.setObjectName("subtitle_label")
        subtitle_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(subtitle_label)
        
        main_container_layout.addLayout(header_layout)

        # Scroll area for settings
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        scroll_widget = QWidget()
        scroll_widget.setStyleSheet("background: transparent;")
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(16)
        self.inputs = {}

        def add_input(name, widget, layout):
            self.inputs[name] = widget
            label = QLabel(name)
            label.setFont(QFont("Calibri", 18, QFont.Medium))
            label.setStyleSheet("""
                QLabel {
                    color: black;
                    font-weight: 500;
                    margin-bottom: 6px;
                    font-family: Calibri;
                }
            """)
            if hasattr(widget, 'setFont'):
                widget.setFont(QFont("Calibri", 16))
            layout.addRow(label, widget)

        # ==================== LAYOUT SECTION ====================
        layout_group, layout_form = self.create_collapsible_section("Layout Settings", False)
        main_layout.addWidget(layout_group)

        # Layout min width and auto width controls
        layout_min_width_spinbox = QSpinBox()
        layout_min_width_spinbox.setMinimum(50)
        layout_min_width_spinbox.setMaximum(2000)
        layout_min_width_spinbox.setSuffix(" px")
        layout_min_width_spinbox.setValue(getattr(self, 'layout_min_width', 200))
        layout_min_width_spinbox.valueChanged.connect(self.save_settings)
        add_input("Layout min width", layout_min_width_spinbox, layout_form)

        layout_width_auto_cb = QCheckBox("Auto layout width")
        layout_width_auto_cb.setChecked(getattr(self, 'layout_width_auto', True))
        layout_width_auto_cb.stateChanged.connect(self.save_settings)
        add_input("Layout width auto", layout_width_auto_cb, layout_form)

        # Background color picker
        layout_bg_col_btn = QPushButton()
        layout_bg_col_btn.setObjectName("color_button")
        layout_bg_col_btn.setFont(QFont("Calibri", 16))
        layout_bg_col_btn.setStyleSheet(f"background-color: {self.layout_bg_color};")
        layout_bg_col_btn.setProperty("selected_color", self.layout_bg_color)
        def pick_layout_bg_color():
            col = QColorDialog.getColor(QColor(self.layout_bg_color), self.settings_win, "Select Background Color")
            if col.isValid():
                layout_bg_col_btn.setStyleSheet(f"background-color: {col.name()};")
                layout_bg_col_btn.setProperty("selected_color", col.name())
                self.save_settings()
        layout_bg_col_btn.clicked.connect(pick_layout_bg_color)
        add_input("Layout bg color", layout_bg_col_btn, layout_form)

        # Transparency checkbox
        layout_bg_transparent_cb = QCheckBox("Transparent background")
        layout_bg_transparent_cb.setChecked(self.layout_bg_transparent)
        layout_bg_transparent_cb.stateChanged.connect(self.save_settings)
        add_input("Layout bg transparent", layout_bg_transparent_cb, layout_form)

        # Percentage transparency (opacity) spinbox
        layout_bg_opacity_spin = QSpinBox()
        layout_bg_opacity_spin.setMinimum(0)
        layout_bg_opacity_spin.setMaximum(100)
        layout_bg_opacity_spin.setSuffix("%")
        layout_bg_opacity_spin.setValue(int(self.layout_bg_opacity))
        layout_bg_opacity_spin.valueChanged.connect(self.save_settings)
        add_input("Layout bg opacity", layout_bg_opacity_spin, layout_form)

        # Border radius spinbox
        layout_border_radius_spin = QSpinBox()
        layout_border_radius_spin.setMinimum(0)
        layout_border_radius_spin.setMaximum(100)
        layout_border_radius_spin.setValue(self.layout_border_radius)
        layout_border_radius_spin.valueChanged.connect(self.save_settings)
        add_input("Layout border radius", layout_border_radius_spin, layout_form)

        # Layout padding spinboxes (top, right, bottom, left)
        layout_padding_top_spin = QSpinBox(); layout_padding_top_spin.setMinimum(0); layout_padding_top_spin.setMaximum(100); layout_padding_top_spin.setValue(self.layout_padding_top); layout_padding_top_spin.valueChanged.connect(self.save_settings)
        layout_padding_right_spin = QSpinBox(); layout_padding_right_spin.setMinimum(0); layout_padding_right_spin.setMaximum(100); layout_padding_right_spin.setValue(self.layout_padding_right); layout_padding_right_spin.valueChanged.connect(self.save_settings)
        layout_padding_bottom_spin = QSpinBox(); layout_padding_bottom_spin.setMinimum(0); layout_padding_bottom_spin.setMaximum(100); layout_padding_bottom_spin.setValue(self.layout_padding_bottom); layout_padding_bottom_spin.valueChanged.connect(self.save_settings)
        layout_padding_left_spin = QSpinBox(); layout_padding_left_spin.setMinimum(0); layout_padding_left_spin.setMaximum(100); layout_padding_left_spin.setValue(self.layout_padding_left); layout_padding_left_spin.valueChanged.connect(self.save_settings)
        self.inputs["Layout padding top"] = layout_padding_top_spin
        self.inputs["Layout padding right"] = layout_padding_right_spin
        self.inputs["Layout padding bottom"] = layout_padding_bottom_spin
        self.inputs["Layout padding left"] = layout_padding_left_spin
        layout_padding_row = QHBoxLayout()
        layout_padding_row.addWidget(layout_padding_top_spin)
        layout_padding_row.addWidget(layout_padding_right_spin)
        layout_padding_row.addWidget(layout_padding_bottom_spin)
        layout_padding_row.addWidget(layout_padding_left_spin)
        layout_padding_container = QWidget(); layout_padding_container.setLayout(layout_padding_row)
        layout_padding_label = QLabel("Layout padding (top, right, bottom, left):")
        layout_padding_label.setFont(QFont("Calibri", 18))
        layout_form.addRow(layout_padding_label, layout_padding_container)

        cb_outline = QCheckBox("")
        cb_outline.setChecked(getattr(self, 'show_outline', False))
        cb_outline.stateChanged.connect(self.save_settings)
        add_input("Show layout outline:", cb_outline, layout_form)

        # ==================== LYRICS SECTION ====================
        lyrics_group, lyrics_form = self.create_collapsible_section("Lyrics Settings", False)
        main_layout.addWidget(lyrics_group)

        lyric_text = QLineEdit(self.lyrics_text)
        lyric_text.textChanged.connect(self.save_settings)
        add_input("Standby lyrics text:", lyric_text, lyrics_form)

        lyric_col_btn = QPushButton()
        lyric_col_btn.setObjectName("color_button")
        lyric_col_btn.setFont(QFont("Calibri", 16))
        lyric_col_btn.setStyleSheet(f"background-color: {self.lyrics_color};")
        lyric_col_btn.setProperty("selected_color", self.lyrics_color)
        lyric_col_btn.clicked.connect(lambda: self.pick_color(lyric_col_btn))
        add_input("Lyrics color:", lyric_col_btn, lyrics_form)

        font_lyr = QFontComboBox()
        font_lyr.setCurrentFont(QFont(self.lyrics_font_family))
        font_lyr.currentFontChanged.connect(lambda _: self.save_settings())
        add_input("Lyrics font:", font_lyr, lyrics_form)

        size_lyr = QSpinBox()
        size_lyr.setValue(self.lyrics_font_size)
        size_lyr.valueChanged.connect(self.save_settings)
        add_input("Lyrics size:", size_lyr, lyrics_form)

        weight_lyr = QSpinBox()
        weight_lyr.setValue(self.lyrics_font_weight)
        weight_lyr.valueChanged.connect(self.save_settings)
        add_input("Lyrics weight:", weight_lyr, lyrics_form)

        align_lyr = QComboBox()
        align_lyr.addItems(["left", "center", "right", "bottom"])
        # Get lyrics alignment with proper fallback handling
        lyrics_alignment = "right"  # default
        if "lyrics" in self.config and "lyrics_alignment" in self.config["lyrics"]:
            lyrics_alignment = self.config["lyrics"]["lyrics_alignment"]
        elif "label" in self.config and "label_alignment" in self.config["label"]:
            lyrics_alignment = self.config["label"]["label_alignment"]
        idx = align_lyr.findText(lyrics_alignment)
        align_lyr.setCurrentIndex(idx if idx >= 0 else 2)
        align_lyr.currentTextChanged.connect(self.save_settings)
        align_lyr.view().window().setStyleSheet("background-color: white;")
        add_input("Lyrics alignment:", align_lyr, lyrics_form)

        # Set popup background for all QComboBox inputs in settings
        # Layout section
        layout_width_auto_cb = self.inputs.get("Layout width auto")
        if isinstance(layout_width_auto_cb, QComboBox):
            layout_width_auto_cb.view().window().setStyleSheet("background-color: white;")
        # Lyrics section
        font_lyr = self.inputs.get("Lyrics font:")
        if isinstance(font_lyr, QFontComboBox):
            font_lyr.view().window().setStyleSheet("background-color: white;")
        # Info section
        font_info = self.inputs.get("Info font:")
        if isinstance(font_info, QFontComboBox):
            font_info.view().window().setStyleSheet("background-color: white;")
        align_info = self.inputs.get("Info alignment:")
        if isinstance(align_info, QComboBox):
            align_info.view().window().setStyleSheet("background-color: white;")

        # Lyrics format template
        lyrics_format_template = QLineEdit(getattr(self, 'lyrics_format_template', '{lyrics}'))
        lyrics_format_template.setPlaceholderText("Available: {title}, {artist}, {software}, {is_playing}, {lyrics}")
        lyrics_format_template.textChanged.connect(self.save_settings)
        
        # Create info button for lyrics template
        lyrics_info_btn = QPushButton("Help")
        lyrics_info_btn.setObjectName("info_button")
        lyrics_info_btn.setFont(QFont("Calibri", 18, QFont.Bold))
        lyrics_info_btn.setToolTip("Template Help")
        lyrics_info_btn.clicked.connect(lambda: self.show_template_help("lyrics"))
        
        # Create horizontal layout for lyrics template input and info button
        lyrics_template_layout = QHBoxLayout()
        lyrics_template_layout.addWidget(lyrics_format_template)
        lyrics_template_layout.addWidget(lyrics_info_btn)
        lyrics_template_layout.setContentsMargins(0, 0, 0, 0)
        
        lyrics_template_container = QWidget()
        lyrics_template_container.setLayout(lyrics_template_layout)
        self.inputs["Lyrics display format:"] = lyrics_format_template
        lyrics_display_label = QLabel("Lyrics display format:")
        lyrics_display_label.setFont(QFont("Calibri", 18))
        lyrics_form.addRow(lyrics_display_label, lyrics_template_container)

        # Lyrics label padding spinboxes
        lyrics_padding_top_spin = QSpinBox(); lyrics_padding_top_spin.setMinimum(0); lyrics_padding_top_spin.setMaximum(100); lyrics_padding_top_spin.setValue(self.lyrics_padding_top); lyrics_padding_top_spin.valueChanged.connect(self.save_settings)
        lyrics_padding_right_spin = QSpinBox(); lyrics_padding_right_spin.setMinimum(0); lyrics_padding_right_spin.setMaximum(100); lyrics_padding_right_spin.setValue(self.lyrics_padding_right); lyrics_padding_right_spin.valueChanged.connect(self.save_settings)
        lyrics_padding_bottom_spin = QSpinBox(); lyrics_padding_bottom_spin.setMinimum(0); lyrics_padding_bottom_spin.setMaximum(100); lyrics_padding_bottom_spin.setValue(self.lyrics_padding_bottom); lyrics_padding_bottom_spin.valueChanged.connect(self.save_settings)
        lyrics_padding_left_spin = QSpinBox(); lyrics_padding_left_spin.setMinimum(0); lyrics_padding_left_spin.setMaximum(100); lyrics_padding_left_spin.setValue(self.lyrics_padding_left); lyrics_padding_left_spin.valueChanged.connect(self.save_settings)
        self.inputs["Lyrics padding top"] = lyrics_padding_top_spin
        self.inputs["Lyrics padding right"] = lyrics_padding_right_spin
        self.inputs["Lyrics padding bottom"] = lyrics_padding_bottom_spin
        self.inputs["Lyrics padding left"] = lyrics_padding_left_spin
        lyrics_padding_row = QHBoxLayout()
        lyrics_padding_row.addWidget(lyrics_padding_top_spin)
        lyrics_padding_row.addWidget(lyrics_padding_right_spin)
        lyrics_padding_row.addWidget(lyrics_padding_bottom_spin)
        lyrics_padding_row.addWidget(lyrics_padding_left_spin)
        lyrics_padding_container = QWidget(); lyrics_padding_container.setLayout(lyrics_padding_row)
        lyrics_padding_label = QLabel("Lyrics padding (top, right, bottom, left):")
        lyrics_padding_label.setFont(QFont("Calibri", 18))
        lyrics_form.addRow(lyrics_padding_label, lyrics_padding_container)

        # Lyrics animation duration spinbox
        lyrics_animation_duration_spin = QSpinBox()
        lyrics_animation_duration_spin.setMinimum(50)
        lyrics_animation_duration_spin.setMaximum(2000)
        lyrics_animation_duration_spin.setSuffix(" ms")
        lyrics_animation_duration_spin.setValue(getattr(self, 'lyrics_animation_duration', 250))
        lyrics_animation_duration_spin.valueChanged.connect(self.save_settings)
        add_input("Lyrics animation duration", lyrics_animation_duration_spin, lyrics_form)

        # ==================== INFO SECTION ====================
        info_group, info_form = self.create_collapsible_section("Info Settings", False)
        main_layout.addWidget(info_group)

        info_text = QLineEdit(self.info_text)
        info_text.textChanged.connect(self.save_settings)
        add_input("Standby info text:", info_text, info_form)

        info_col_btn = QPushButton()
        info_col_btn.setObjectName("color_button")
        info_col_btn.setFont(QFont("Calibri", 16))
        info_col_btn.setStyleSheet(f"background-color: {self.info_color};")
        info_col_btn.setProperty("selected_color", self.info_color)
        info_col_btn.clicked.connect(lambda: self.pick_color(info_col_btn))
        add_input("Info color:", info_col_btn, info_form)

        font_info = QFontComboBox()
        font_info.setCurrentFont(QFont(self.info_font_family))
        font_info.currentFontChanged.connect(lambda _: self.save_settings())
        font_info.view().window().setStyleSheet("background-color: white;")
        add_input("Info font:", font_info, info_form)

        size_info = QSpinBox()
        size_info.setValue(self.info_font_size)
        size_info.valueChanged.connect(self.save_settings)
        add_input("Info size:", size_info, info_form)

        weight_info = QSpinBox()
        weight_info.setValue(self.info_font_weight)
        weight_info.valueChanged.connect(self.save_settings)
        add_input("Info weight:", weight_info, info_form)

        align_info = QComboBox()
        align_info.addItems(["left", "center", "right", "bottom"])
        
        # Get info alignment with proper fallback handling
        info_alignment = "right"  # default
        if "info" in self.config and "info_alignment" in self.config["info"]:
            info_alignment = self.config["info"]["info_alignment"]
        elif "details" in self.config and "detail_alignment" in self.config["details"]:
            info_alignment = self.config["details"]["detail_alignment"]
            
        idx2 = align_info.findText(info_alignment)
        align_info.setCurrentIndex(idx2 if idx2 >= 0 else 2)
        align_info.currentTextChanged.connect(self.save_settings)
        align_info.view().window().setStyleSheet("background-color: white;")
        add_input("Info alignment:", align_info, info_form)

        # Info format template
        info_format_template = QLineEdit(getattr(self, 'info_format_template', '{title} - {artist}'))
        info_format_template.setPlaceholderText("Available: {title}, {artist}, {software}, {is_playing}, {lyrics}")
        info_format_template.textChanged.connect(self.save_settings)
        
        # Create info button for info template
        info_info_btn = QPushButton("Help")
        info_info_btn.setObjectName("info_button")
        info_info_btn.setFont(QFont("Calibri", 18, QFont.Bold))
        info_info_btn.setToolTip("Template Help")
        info_info_btn.clicked.connect(lambda: self.show_template_help("info"))
        
        # Create horizontal layout for info template input and info button
        info_template_layout = QHBoxLayout()
        info_template_layout.addWidget(info_format_template)
        info_template_layout.addWidget(info_info_btn)
        info_template_layout.setContentsMargins(0, 0, 0, 0)
        
        info_template_container = QWidget()
        info_template_container.setLayout(info_template_layout)
        self.inputs["Info display format:"] = info_format_template
        info_display_label = QLabel("Info display format:")
        info_display_label.setFont(QFont("Calibri", 18))
        info_form.addRow(info_display_label, info_template_container)

        # Info label padding spinboxes
        info_padding_top_spin = QSpinBox(); info_padding_top_spin.setMinimum(0); info_padding_top_spin.setMaximum(100); info_padding_top_spin.setValue(self.info_padding_top); info_padding_top_spin.valueChanged.connect(self.save_settings)
        info_padding_right_spin = QSpinBox(); info_padding_right_spin.setMinimum(0); info_padding_right_spin.setMaximum(100); info_padding_right_spin.setValue(self.info_padding_right); info_padding_right_spin.valueChanged.connect(self.save_settings)
        info_padding_bottom_spin = QSpinBox(); info_padding_bottom_spin.setMinimum(0); info_padding_bottom_spin.setMaximum(100); info_padding_bottom_spin.setValue(self.info_padding_bottom); info_padding_bottom_spin.valueChanged.connect(self.save_settings)
        info_padding_left_spin = QSpinBox(); info_padding_left_spin.setMinimum(0); info_padding_left_spin.setMaximum(100); info_padding_left_spin.setValue(self.info_padding_left); info_padding_left_spin.valueChanged.connect(self.save_settings)
        self.inputs["Info padding top"] = info_padding_top_spin
        self.inputs["Info padding right"] = info_padding_right_spin
        self.inputs["Info padding bottom"] = info_padding_bottom_spin
        self.inputs["Info padding left"] = info_padding_left_spin
        info_padding_row = QHBoxLayout()
        info_padding_row.addWidget(info_padding_top_spin)
        info_padding_row.addWidget(info_padding_right_spin)
        info_padding_row.addWidget(info_padding_bottom_spin)
        info_padding_row.addWidget(info_padding_left_spin)
        info_padding_container = QWidget(); info_padding_container.setLayout(info_padding_row)
        info_padding_label = QLabel("Info padding (top, right, bottom, left):")
        info_padding_label.setFont(QFont("Calibri", 18))
        info_form.addRow(info_padding_label, info_padding_container)

        # Info animation duration spinbox
        info_animation_duration_spin = QSpinBox()
        info_animation_duration_spin.setMinimum(50)
        info_animation_duration_spin.setMaximum(2000)
        info_animation_duration_spin.setSuffix(" ms")
        info_animation_duration_spin.setValue(getattr(self, 'info_animation_duration', 250))
        info_animation_duration_spin.valueChanged.connect(self.save_settings)
        add_input("Info animation duration", info_animation_duration_spin, info_form)

        # ==================== OTHER SETTINGS (Non-collapsible) ====================

        # Create a simple form layout for other settings
        other_form = QFormLayout()

        # Window Move & Always‑on‑Top
        cb_move = QCheckBox("")
        cb_move.setFont(QFont("Calibri", 16))
        cb_move.setChecked(self.move_enabled)
        cb_move.stateChanged.connect(self.save_settings)
        self.inputs["Move:"] = cb_move
        move_label = QLabel("Move:")
        move_label.setFont(QFont("Calibri", 18))
        other_form.addRow(move_label, cb_move)

        cb_top = QCheckBox("")
        cb_top.setFont(QFont("Calibri", 16))
        cb_top.setChecked(self.always_on_top)
        cb_top.stateChanged.connect(self.save_settings)
        self.inputs["Always on top:"] = cb_top
        top_label = QLabel("Always on top:")
        top_label.setFont(QFont("Calibri", 18))
        other_form.addRow(top_label, cb_top)

        # Excluded Programs
        excluded_programs_text = QLineEdit(", ".join(self.excluded_programs))
        excluded_programs_text.setFont(QFont("Calibri", 16))
        excluded_programs_text.setPlaceholderText("e.g., chrome, firefox, edge")
        
        # Create save button for excluded programs
        save_excluded_btn = QPushButton("Save")
        save_excluded_btn.setObjectName("save_button")
        save_excluded_btn.setFont(QFont("Calibri", 18, QFont.DemiBold))
        save_excluded_btn.clicked.connect(self.save_excluded_programs)
        
        # Create horizontal layout for excluded programs input and save button
        excluded_layout = QHBoxLayout()
        excluded_layout.addWidget(excluded_programs_text)
        excluded_layout.addWidget(save_excluded_btn)
        
        excluded_container = QWidget()
        excluded_container.setLayout(excluded_layout)
        self.inputs["Excluded programs:"] = excluded_container
        excluded_label = QLabel("Excluded programs:")
        excluded_label.setObjectName("excluded_programs_label")
        excluded_label.setFont(QFont("Calibri", 18))
        other_form.addRow(excluded_label, excluded_container)
        
        # Store reference to the text input for the save method
        self.excluded_programs_input = excluded_programs_text

        # Add other settings to main layout
        main_layout.addLayout(other_form)

        scroll_widget.setLayout(main_layout)
        scroll_area.setWidget(scroll_widget)
        
        main_container_layout.addWidget(scroll_area)
        main_container.setLayout(main_container_layout)
        
        window_layout = QVBoxLayout()
        window_layout.setContentsMargins(0, 0, 0, 0)
        window_layout.addWidget(main_container)
        self.settings_win.setLayout(window_layout)

    def show_settings_window(self):
        self.settings_win.show()
        self.settings_win.raise_()
        
        # Auto-adjust window size to content
        self.settings_win.adjustSize()
        self.settings_win.setMinimumSize(1250, 650)  # width=1250, height=650

        # Center the window
        self.settings_win.move(
            QGuiApplication.primaryScreen().geometry().center() - 
            self.settings_win.rect().center()
        )

    def show_template_help(self, template_type):
        """Show a help popup for template variables"""
        help_dialog = QWidget(flags=Qt.Tool | Qt.WindowStaysOnTopHint)
        help_dialog.setWindowTitle(f"Template Help - {template_type.title()}")
        help_dialog.setFixedSize(550, 480)
        help_dialog.setFont(QFont("Calibri", 16))
        
        # Modern help dialog styling
        help_dialog.setStyleSheet("""
            QWidget {
                background: white;
                border-radius: 8px;
                font-family: Calibri;
            }
            QWidget#card {
                background: rgba(255, 255, 255, 0.98);
                border-radius: 8px;
                border: none;
            }
            QLabel#title {
                color: black;
                font-size: 20px;
                font-weight: 600;
                margin: 0px;
                padding: 0px;
            }
            QLabel#content {
                color: black;
                font-size: 16px;
                line-height: 1.6;
                margin: 0px;
                padding: 0px;
                font-family: Calibri;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollBar:vertical {
                background: rgba(0, 0, 0, 0.1);
                width: 6px;
                border-radius: 3px;
                margin: 0px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background: rgba(0, 0, 0, 0.3);
                border-radius: 3px;
                min-height: 20px;
                border: none;
            }
            QPushButton {
                background: #f0f0f0;
                color: #404040;
                border: 2px solid #404040;
                border-radius: 6px;
                padding: 12px 24px;
                font-size: 18px;
                font-weight: 600;
                min-width: 100px;
                font-family: Calibri;
            }
            QPushButton:hover {
                background: #e0e0e0;
                border-color: #202020;
            }
        """)
        
        # Main container with padding
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(0)

        # Card container
        card = QWidget()
        card.setObjectName("card")
        card_layout = QVBoxLayout()
        card_layout.setContentsMargins(24, 24, 24, 24)
        card_layout.setSpacing(20)
        
        # Title
        title_label = QLabel(f"Template Variables - {template_type.title()}")
        title_label.setObjectName("title")
        title_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(title_label)
        
        # Variable descriptions with updated content
        help_text = """
<div style="font-family: Calibri; line-height: 1.6;">
<h3 style="color: #2d3748; margin-top: 0;">Available Variables:</h3>

<p><strong>{title}</strong> — Song title<br>
<em style="color: #718096;">Examples: "Perfect", "Bohemian Rhapsody"</em></p>

<p><strong>{artist}</strong> — Artist name<br>
<em style="color: #718096;">Examples: "Ed Sheeran", "Queen"</em></p>

<p><strong>{software}</strong> — Media player in use<br>
<em style="color: #718096;">Examples: "Spotify", "Chrome", "VLC"</em></p>

<p><strong>{is_playing}</strong> — Playback status<br>
<em style="color: #718096;">Values: "Playing" or "Paused"</em></p>

<p><strong>{lyrics}</strong> — Current lyric line<br>
<em style="color: #718096;">Example: "I found a love for me"</em></p>

<hr style="border: 1px solid #e2e8f0; margin: 16px 0;">

<h3 style="color: #2d3748;">Usage Examples:</h3>

<h4 style="color: #4a5568; margin-top: 12px;">Lyrics Template:</h4>
<ul style="margin: 8px 0; padding-left: 20px;">
<li><code style="background: #f7fafc; padding: 2px 4px; border-radius: 3px;">{lyrics}</code> — Shows just the lyrics</li>
<li><code style="background: #f7fafc; padding: 2px 4px; border-radius: 3px;">♪ {lyrics} ♪</code> — Lyrics with music notes</li>
<li><code style="background: #f7fafc; padding: 2px 4px; border-radius: 3px;">[{is_playing}] {lyrics}</code> — Status + lyrics</li>
</ul>

<h4 style="color: #4a5568; margin-top: 12px;">Info Template:</h4>
<ul style="margin: 8px 0; padding-left: 20px;">
<li><code style="background: #f7fafc; padding: 2px 4px; border-radius: 3px;">{title} - {artist}</code> — Song and artist</li>
<li><code style="background: #f7fafc; padding: 2px 4px; border-radius: 3px;">🎵 {title} by {artist}</code> — With emoji</li>
<li><code style="background: #f7fafc; padding: 2px 4px; border-radius: 3px;">{title} - {artist} ({software})</code> — Include player name</li>
<li><code style="background: #f7fafc; padding: 2px 4px; border-radius: 3px;">{artist}: {title} [{is_playing}]</code> — Artist + status</li>
</ul>

<hr style="border: 1px solid #e2e8f0; margin: 16px 0;">

<h4 style="color: #4a5568;">💡 Tips:</h4>
<ul style="margin: 8px 0; padding-left: 20px;">
<li>Combine variables with any text or emojis</li>
<li>Unknown variables (e.g., <code style="background: #f7fafc; padding: 2px 4px; border-radius: 3px;">{abc}</code>) will appear as typed</li>
<li>Feel free to get creative with symbols and formatting</li>
</ul>
</div>
"""
        
        help_label = QLabel(help_text)
        help_label.setObjectName("content")
        help_label.setWordWrap(True)
        help_label.setTextFormat(Qt.RichText)
        
        # Scroll area for the help text
        scroll_area = QScrollArea()
        scroll_area.setWidget(help_label)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        card_layout.addWidget(scroll_area)
        
        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(help_dialog.close)
        card_layout.addWidget(close_btn)
        
        card.setLayout(card_layout)
        main_layout.addWidget(card)
        help_dialog.setLayout(main_layout)
        
        # Position dialog relative to settings window
        if hasattr(self, 'settings_win'):
            help_dialog.move(
                self.settings_win.x() + 50,
                self.settings_win.y() + 50
            )
        
        help_dialog.show()
        # Keep reference to prevent garbage collection
        self._help_dialog = help_dialog

    def save_settings(self):
        try:
            # Update immediate values from inputs
            self.layout_min_width = self.inputs["Layout min width"].value()
            self.layout_width_auto = self.inputs["Layout width auto"].isChecked()
            self.show_outline = self.inputs["Show layout outline:"].isChecked()
            self.layout_bg_color = self.inputs["Layout bg color"].property("selected_color") or getattr(self, "layout_bg_color", "#000000")
            self.layout_bg_transparent = self.inputs["Layout bg transparent"].isChecked()
            self.layout_bg_opacity = self.inputs["Layout bg opacity"].value()
            self.layout_border_radius = self.inputs["Layout border radius"].value()
            self.layout_padding_top = self.inputs["Layout padding top"].value()
            self.layout_padding_right = self.inputs["Layout padding right"].value()
            self.layout_padding_bottom = self.inputs["Layout padding bottom"].value()
            self.layout_padding_left = self.inputs["Layout padding left"].value()
            self.lyrics_padding_top = self.inputs["Lyrics padding top"].value()
            self.lyrics_padding_right = self.inputs["Lyrics padding right"].value()
            self.lyrics_padding_bottom = self.inputs["Lyrics padding bottom"].value()
            self.lyrics_padding_left = self.inputs["Lyrics padding left"].value()
            self.lyrics_animation_duration = self.inputs["Lyrics animation duration"].value()
            self.info_padding_top = self.inputs["Info padding top"].value()
            self.info_padding_right = self.inputs["Info padding right"].value()
            self.info_padding_bottom = self.inputs["Info padding bottom"].value()
            self.info_padding_left = self.inputs["Info padding left"].value()
            self.info_animation_duration = self.inputs["Info animation duration"].value()
            self.lyrics_format_template = self.inputs["Lyrics display format:"].text()
            self.info_format_template = self.inputs["Info display format:"].text()
            
            # Update lyrics font settings from inputs
            self.lyrics_text = self.inputs["Standby lyrics text:"].text()
            self.lyrics_color = self.inputs["Lyrics color:"].property("selected_color") or getattr(self, "lyrics_color", "#FFFFFF")
            self.lyrics_font_family = self.inputs["Lyrics font:"].currentFont().family()
            self.lyrics_font_size = self.inputs["Lyrics size:"].value()
            self.lyrics_font_weight = self.inputs["Lyrics weight:"].value()
            self.lyrics_alignment = self._parse_alignment(self.inputs["Lyrics alignment:"].currentText())
            
            # Update info font settings from inputs
            self.info_text = self.inputs["Standby info text:"].text()
            self.info_color = self.inputs["Info color:"].property("selected_color") or getattr(self, "info_color", "#FFFFFF")
            self.info_font_family = self.inputs["Info font:"].currentFont().family()
            self.info_font_size = self.inputs["Info size:"].value()
            self.info_font_weight = self.inputs["Info weight:"].value()
            self.info_alignment = self._parse_alignment(self.inputs["Info alignment:"].currentText())

            # Read existing config to preserve unknown sections like [excluded_programs]
            cfg = configparser.ConfigParser()
            cfg.read(self.config_path, encoding='utf-8')

            # Ensure sections exist
            if "layout" not in cfg:
                cfg.add_section("layout")
            if "lyrics" not in cfg:
                cfg.add_section("lyrics")
            if "info" not in cfg:
                cfg.add_section("info")
            if "window" not in cfg:
                cfg.add_section("window")

            # Layout section
            cfg["layout"]["layout_margins"] = ", ".join(map(str, self.layout_margins))
            cfg["layout"]["default_margin"] = str(self.default_margin)
            cfg["layout"]["layout_min_width"] = str(self.layout_min_width)
            cfg["layout"]["layout_width_auto"] = str(self.layout_width_auto)
            cfg["layout"]["show_outline"] = str(self.show_outline)
            cfg["layout"]["layout_bg_color"] = self.layout_bg_color
            cfg["layout"]["layout_bg_transparent"] = str(self.layout_bg_transparent)
            cfg["layout"]["layout_bg_opacity"] = str(self.layout_bg_opacity)
            cfg["layout"]["layout_border_radius"] = str(self.layout_border_radius)
            cfg["layout"]["layout_padding_top"] = str(self.layout_padding_top)
            cfg["layout"]["layout_padding_right"] = str(self.layout_padding_right)
            cfg["layout"]["layout_padding_bottom"] = str(self.layout_padding_bottom)
            cfg["layout"]["layout_padding_left"] = str(self.layout_padding_left)

            # Lyrics section
            lyr_color = self.inputs["Lyrics color:"].property("selected_color") or getattr(self, "lyrics_color", "#FFFFFF")
            cfg["lyrics"]["lyrics_text"] = self.inputs["Standby lyrics text:"].text()
            cfg["lyrics"]["lyrics_color"] = lyr_color
            cfg["lyrics"]["lyrics_font_family"] = self.inputs["Lyrics font:"].currentFont().family()
            cfg["lyrics"]["lyrics_font_size"] = str(self.inputs["Lyrics size:"].value())
            cfg["lyrics"]["lyrics_font_weight"] = str(self.inputs["Lyrics weight:"].value())
            cfg["lyrics"]["lyrics_alignment"] = self.inputs["Lyrics alignment:"].currentText()
            # Lyrics label padding
            cfg["lyrics"]["padding_top"] = str(self.lyrics_padding_top)
            cfg["lyrics"]["padding_right"] = str(self.lyrics_padding_right)
            cfg["lyrics"]["padding_bottom"] = str(self.lyrics_padding_bottom)
            cfg["lyrics"]["padding_left"] = str(self.lyrics_padding_left)
            # Lyrics animation duration
            cfg["lyrics"]["animation_duration"] = str(self.lyrics_animation_duration)
            # Lyrics format template
            cfg["lyrics"]["format_template"] = self.inputs["Lyrics display format:"].text()

            # Info section
            info_color = self.inputs["Info color:"].property("selected_color") or getattr(self, "info_color", "#FFFFFF")
            cfg["info"]["info_text"] = self.inputs["Standby info text:"].text()
            cfg["info"]["info_color"] = info_color
            cfg["info"]["info_font_family"] = self.inputs["Info font:"].currentFont().family()
            cfg["info"]["info_font_size"] = str(self.inputs["Info size:"].value())
            cfg["info"]["info_font_weight"] = str(self.inputs["Info weight:"].value())
            cfg["info"]["info_alignment"] = self.inputs["Info alignment:"].currentText()
            # Info label padding
            cfg["info"]["padding_top"] = str(self.info_padding_top)
            cfg["info"]["padding_right"] = str(self.info_padding_right)
            cfg["info"]["padding_bottom"] = str(self.info_padding_bottom)
            cfg["info"]["padding_left"] = str(self.info_padding_left)
            # Info animation duration
            cfg["info"]["animation_duration"] = str(self.info_animation_duration)
            # Info format template
            cfg["info"]["format_template"] = self.inputs["Info display format:"].text()

            # Window section
            cfg["window"]["move_enabled"] = str(self.inputs["Move:"].isChecked())
            cfg["window"]["always_on_top"] = str(self.inputs["Always on top:"].isChecked())
            cfg["window"]["window_x"] = str(self.window_x)
            cfg["window"]["window_y"] = str(self.window_y)

            # Write back without dropping other sections (e.g., [excluded_programs])
            with open(self.config_path, "w", encoding='utf-8') as f:
                cfg.write(f)

            # Apply changes to UI immediately without reloading config to preserve real-time updates
            self.apply_config_to_ui()

        except Exception as ee:
            print(f"[ERROR saving settings]: {ee}")
            QMessageBox.critical(self.settings_win, "Error", f"Could not save settings:\n{ee}")

    def save_excluded_programs(self):
        """Save only the excluded programs to the config file"""
        try:
            # Read current config
            cfg = configparser.ConfigParser()
            cfg.read(self.config_path, encoding='utf-8')
            
            # Update only the excluded programs section
            if "excluded_programs" not in cfg:
                cfg.add_section("excluded_programs")
            
            cfg["excluded_programs"]["programs"] = self.excluded_programs_input.text().strip()
            
            # Save the config
            with open(self.config_path, "w", encoding='utf-8') as f:
                cfg.write(f)
            
            # Reload config to update the excluded_programs list
            self.load_config()
            
            # Show confirmation message
            QMessageBox.information(self.settings_win, "Success", "Excluded programs saved successfully!")
            
        except Exception as e:
            print(f"[ERROR saving excluded programs]: {e}")
            QMessageBox.critical(self.settings_win, "Error", f"Could not save excluded programs:\n{e}")

    def apply_config_to_ui(self):
        # Update all values from inputs if settings window is open
        if hasattr(self, 'inputs'):
            # Update animation durations to ensure real-time changes
            if "Lyrics animation duration" in self.inputs:
                self.lyrics_animation_duration = self.inputs["Lyrics animation duration"].value()
            if "Info animation duration" in self.inputs:
                self.info_animation_duration = self.inputs["Info animation duration"].value()
            
            # Update other settings that might have changed
            if "Lyrics color:" in self.inputs:
                self.lyrics_color = self.inputs["Lyrics color:"].property("selected_color") or getattr(self, "lyrics_color", "#FFFFFF")
            if "Info color:" in self.inputs:
                self.info_color = self.inputs["Info color:"].property("selected_color") or getattr(self, "info_color", "#FFFFFF")
            if "Standby lyrics text:" in self.inputs:
                self.lyrics_text = self.inputs["Standby lyrics text:"].text()
            if "Standby info text:" in self.inputs:
                self.info_text = self.inputs["Standby info text:"].text()
            if "Lyrics display format:" in self.inputs:
                self.lyrics_format_template = self.inputs["Lyrics display format:"].text()
            if "Info display format:" in self.inputs:
                self.info_format_template = self.inputs["Info display format:"].text()
            
            # Update lyrics font settings from inputs
            if "Lyrics font:" in self.inputs:
                self.lyrics_font_family = self.inputs["Lyrics font:"].currentFont().family()
            if "Lyrics size:" in self.inputs:
                self.lyrics_font_size = self.inputs["Lyrics size:"].value()
            if "Lyrics weight:" in self.inputs:
                self.lyrics_font_weight = self.inputs["Lyrics weight:"].value()
            if "Lyrics alignment:" in self.inputs:
                self.lyrics_alignment = self._parse_alignment(self.inputs["Lyrics alignment:"].currentText())
                
            # Update info font settings from inputs
            if "Info font:" in self.inputs:
                self.info_font_family = self.inputs["Info font:"].currentFont().family()
            if "Info size:" in self.inputs:
                self.info_font_size = self.inputs["Info size:"].value()
            if "Info weight:" in self.inputs:
                self.info_font_weight = self.inputs["Info weight:"].value()
            if "Info alignment:" in self.inputs:
                self.info_alignment = self._parse_alignment(self.inputs["Info alignment:"].currentText())
                
        # Recalculate layout width based on updated percentage or auto
        screen_geo = QGuiApplication.primaryScreen().geometry()
        if getattr(self, 'layout_width_auto', True):
            self.layout_container.setMinimumWidth(self.layout_min_width)
            self.layout_container.setMaximumWidth(16777215)
            self.layout_container.setSizePolicy(self.layout_container.sizePolicy().horizontalPolicy(), self.layout_container.sizePolicy().verticalPolicy())
        else:
            self.layout_container.setFixedWidth(self.layout_min_width)
            self.layout_container.setMinimumWidth(self.layout_min_width)
        # Update container outline and background (no QSS padding)
        if self.layout_bg_transparent:
            bg_style = "background-color: rgba(0,0,0,0);"
        else:
            alpha = int(255 * (self.layout_bg_opacity / 100.0))
            col = QColor(self.layout_bg_color)
            bg_style = f"background-color: rgba({col.red()}, {col.green()}, {col.blue()}, {alpha});"
        border_radius_style = f"border-radius: {self.layout_border_radius}px;" if self.layout_border_radius else ""
        style = f"{bg_style} {border_radius_style}"
        if self.show_outline:
            self.layout_container.setStyleSheet(f"border: 2px solid {self.lyrics_color}; {style}")
        else:
            self.layout_container.setStyleSheet(style)
        # Set layout container margins to simulate padding
        self.layout_container.layout().setContentsMargins(
            self.layout_padding_left,
            self.layout_padding_top,
            self.layout_padding_right,
            self.layout_padding_bottom
        )

        # Update styles, fonts, and alignment without forcing text reset during live playback
        if getattr(self, "_standby_active", True):
            # Only update the standby text when we're actually in standby
            # Use the updated text from settings inputs if available
            if hasattr(self, 'inputs') and "Standby lyrics text:" in self.inputs:
                current_lyrics_text = self.inputs["Standby lyrics text:"].text()
            else:
                current_lyrics_text = self.lyrics_text
            self.lyrics_label.setText(current_lyrics_text)
        self.lyrics_label.setStyleSheet(
            f"color: {self.lyrics_color}; font-weight: {self.lyrics_font_weight}px; background: transparent; border: none;"
            f"padding: {self.lyrics_padding_top}px {self.lyrics_padding_right}px {self.lyrics_padding_bottom}px {self.lyrics_padding_left}px;"
        )
        self.lyrics_label.setFont(QFont(self.lyrics_font_family, self.lyrics_font_size))
        self.lyrics_label.setAlignment(self.lyrics_alignment)

        if getattr(self, "_standby_active", True):
            # Use the updated text from settings inputs if available
            if hasattr(self, 'inputs') and "Standby info text:" in self.inputs:
                current_info_text = self.inputs["Standby info text:"].text()
            else:
                current_info_text = self.info_text
            self.info_label.setText(current_info_text)
        self.info_label.setStyleSheet(
            f"color: {self.info_color}; font-weight: {self.info_font_weight}px; background: transparent; border: none;"
            f"padding: {self.info_padding_top}px {self.info_padding_right}px {self.info_padding_bottom}px {self.info_padding_left}px;"
        )
        self.info_label.setFont(QFont(self.info_font_family, self.info_font_size))
        self.info_label.setAlignment(self.info_alignment)

        self.layout.setContentsMargins(0, 0, *self.layout_margins)
        
        # Update move_enabled and always_on_top from settings inputs if available
        if hasattr(self, 'inputs') and "Move:" in self.inputs:
            self.move_enabled = self.inputs["Move:"].isChecked()
        else:
            self.move_enabled = self.config.getboolean("window", "move_enabled", fallback=False)
            
        if hasattr(self, 'inputs') and "Always on top:" in self.inputs:
            self.always_on_top = self.inputs["Always on top:"].isChecked()
        else:
            self.always_on_top = self.config.getboolean("window", "always_on_top", fallback=True)

        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.win.setWindowFlags(flags)
        # if settings window still open, keep it open
        self.win.show()