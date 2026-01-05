# controller.py
import asyncio
import time
import re
import json
import os
import syncedlyrics
from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
import configparser
import subprocess
import keyring

# Optional audio-peek imports; only used if available
try:
    from pycaw.pycaw import AudioUtilities
    from comtypes import CLSCTX_ALL
    PYCAW_AVAILABLE = True
except Exception:
    PYCAW_AVAILABLE = False


class Controller:
    def __init__(self, signal_bus, main_window=None, appdata_location=None, service_name=None, service_username=None):
        current_folder = os.path.dirname(os.path.abspath(__file__))
        self.lyrics_folder = os.path.join(current_folder, "lyrics")
        self.last_position = 0
        self.last_update_time = time.time()
        self.signal_bus = signal_bus
        self.main_window = main_window
        self.AUTHENTICATED = False
        self.appdata_location = appdata_location
        self.service_name = service_name
        self.service_username = service_username

        # Connect the request_action signal to authentication method
        self.signal_bus.request_action.connect(self.handle_auth_request)
        
        # Initialize session cache attributes
        self._session_cache = None
        self._session_cache_time = 0
        
        # Ensure config file exists with all required fields
        self.ensure_config_file()

    def ensure_config_file(self):
        """
        Check if .conf file exists, create it if it doesn't.
        If it exists, ensure all required fields are present.
        Handle corrupted config files by backing up and recreating.
        """
        config_path = os.path.join(self.appdata_location, '.conf')

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
            },
        }
        
        def create_fresh_config():
            """Create a fresh config file with default values"""
            config = configparser.ConfigParser()
            for section_name, section_data in default_config.items():
                config.add_section(section_name)
                for key, value in section_data.items():
                    config.set(section_name, key, value)
            
            with open(config_path, 'w', encoding='utf-8') as configfile:
                config.write(configfile)
            return config
        
        def backup_corrupted_config():
            """Create a backup of the corrupted config file"""
            try:
                import shutil
                from datetime import datetime
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(self.appdata_location, f'.conf_backup_{timestamp}')
                shutil.copy2(config_path, backup_path)
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
        if not os.path.exists(config_path):
            print(f"Config file not found. Creating new .conf file at: {config_path}")
            config = create_fresh_config()
            print("Config file created successfully.")
            return
        
        # Try to read the existing config file
        try:
            config.read(config_path, encoding='utf-8')
            print("Config file loaded successfully.")
        except (configparser.ParsingError, configparser.Error, UnicodeDecodeError) as e:
            print(f"Config file is corrupted: {e}")
            
            # Backup the corrupted file
            backup_path = backup_corrupted_config()
            
            # Try to salvage values
            salvaged_values = extract_salvageable_values(config_path)
            
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
                with open(config_path, 'w', encoding='utf-8') as configfile:
                    config.write(configfile)
                print("Config file recreated with salvaged values.")
            else:
                print("Config file recreated with default values.")
            
            return
        except Exception as e:
            print(f"Unexpected error reading config: {e}")
            print("Creating fresh config file...")
            config = create_fresh_config()
            print("Fresh config file created.")
            return
        
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
                with open(config_path, 'w', encoding='utf-8') as configfile:
                    config.write(configfile)
                print("Config file updated with missing fields.")
            except Exception as e:
                print(f"Failed to save updated config: {e}")
        else:
            print("Config file is complete, no updates needed.")

    async def request_session_manager(self):
        return await GlobalSystemMediaTransportControlsSessionManager.request_async()

    async def get_now_playing(self, session):
        song_data = {}
        if not session:
            return None

        props = await session.try_get_media_properties_async()
        timeline = session.get_timeline_properties()
        playback_info = session.get_playback_info()

        if props:
            song_data = {
                "title": props.title,
                "artist": props.artist,
                "duration_sec": (timeline.end_time - timeline.start_time).total_seconds(),
                "position_sec": timeline.position.total_seconds(),
                "software": session.source_app_user_model_id,
                "status": str(playback_info.playback_status),
                "is_playing": playback_info.playback_status.name == "PLAYING",
                # keep session object if caller wants to inspect
                "_session_obj": session
            }
        return song_data

    def download_lyrics(self, song_name, json_output_path):
        os.makedirs(os.path.dirname(json_output_path), exist_ok=True)
        if not os.path.exists(json_output_path):
            pattern = re.compile(r"\[(\d+):(\d+\.\d+)\](.*)")
            data = []

            lyrics = syncedlyrics.search(song_name)
            if not lyrics:
                print(f"No lyrics found for {song_name}")
                return

            for line in lyrics.splitlines():
                match = pattern.match(line.strip())
                if match:
                    minutes, seconds, lyrics = match.groups()
                    total_seconds = int(minutes) * 60 + float(seconds)
                    data.append({
                        "time": round(total_seconds, 2),
                        "lyrics": lyrics.strip()
                    })

            with open(json_output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        with open(json_output_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_lyric_at_time(self, lyrics_data, current_time):
        if not lyrics_data:
            return "Sorry, no lyrics available for this song."
        previous_lyric = ""

        for entry in lyrics_data:
            if current_time < entry["time"]:
                return previous_lyric
            previous_lyric = entry["lyrics"]

        return previous_lyric

    async def wait_for_session(self):
        print("Waiting for media session to become available...")
        while True:
            try:
                manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
                session = manager.get_current_session()
                if session:
                    print("Media session detected!")
                    return session
            except Exception as e:
                print(f"Media session request failed: {e}")

            await asyncio.sleep(2)

    def is_program_excluded(self, app_id):
        if not self.main_window or not hasattr(self.main_window, 'excluded_programs'):
            return False

        if not app_id:
            return False

        app_name = app_id.lower()

        for excluded_program in self.main_window.excluded_programs:
            if excluded_program in app_name:
                return True
        return False

    def sanitize_filenames(self, filename: str, replacement: str = "_") -> str:
        invalid_chars = r'[<>:"/\\|?*\x00-\x1F]'
        reserved_names = {
            "CON", "PRN", "AUX", "NUL",
            *[f"COM{i}" for i in range(1, 10)],
            *[f"LPT{i}" for i in range(1, 10)]
        }

        sanitized = re.sub(invalid_chars, replacement, filename)
        sanitized = sanitized.rstrip(" .")
        name_only = sanitized.split('.')[0].upper()
        if name_only in reserved_names:
            sanitized = f"_{sanitized}"

        return sanitized

    async def select_best_session(self):
        """
        Enumerate sessions and return the session most likely producing audio:
         - Prefer sessions with playback_status == PLAYING and not excluded
         - If multiple candidates, optionally use pycaw audio peak info (if available) to choose the loudest
         - Fallback to manager.get_current_session()
        
        Now with caching to reduce API calls and improve performance.
        """
        # Cache sessions for 2 seconds to reduce API overhead
        current_time = time.time()
        if not hasattr(self, '_session_cache_time'):
            self._session_cache_time = 0
            self._session_cache = None
        
        if current_time - self._session_cache_time < 2.0 and self._session_cache:
            return self._session_cache
        
        try:
            manager = await self.request_session_manager()
        except Exception as e:
            print(f"[select_best_session] failed to request manager: {e}")
            return None

        # 1) Collect metadata for all sessions
        sessions = manager.get_sessions()
        candidates = []
        # gather async calls
        for s in sessions:
            try:
                playback_info = s.get_playback_info()
                status_name = playback_info.playback_status.name
                # try_get_media_properties_async returns an object with title/artist
                props = await s.try_get_media_properties_async()
                title = getattr(props, "title", None) or ""
                # Skip empty titles (some sessions are system/background without meaningful media)
                if self.is_program_excluded(s.source_app_user_model_id):
                    continue
                candidates.append({
                    "session": s,
                    "status": status_name,
                    "title": title,
                    "app_id": s.source_app_user_model_id
                })
            except Exception as e:
                # ignore problematic sessions
                # print(f"[select_best_session] can't query session: {e}")
                continue

        # 2) Prefer sessions with status PLAYING and non-empty title
        playing = [c for c in candidates if c["status"] == "PLAYING" and c["title"].strip()]
        if not playing:
            # fallback: maybe paused or only a single session; choose a non-excluded session with title
            playing = [c for c in candidates if c["title"].strip()]

        if not playing:
            # Final fallback to manager.get_current_session()
            try:
                fallback = manager.get_current_session()
                self._session_cache = fallback
                self._session_cache_time = current_time
                return fallback
            except Exception:
                return None

        # If only one playing, return it
        if len(playing) == 1:
            result = playing[0]["session"]
            self._session_cache = result
            self._session_cache_time = current_time
            return result

        # If multiple playing sessions, try to disambiguate
        # a) Prefer a session whose title is non-empty and not a system placeholder
        # b) If pycaw available, pick by audio peak level
        if PYCAW_AVAILABLE:
            try:
                # Map process names to peak volumes using pycaw
                sessions_peaks = self._get_process_peaks_by_appid([p["app_id"] for p in playing])
                # sessions_peaks: dict mapping app_id_lower -> peak_value
                # choose the playing session with highest peak
                best = None
                best_peak = -1.0
                for p in playing:
                    aid = (p["app_id"] or "").lower()
                    peak = sessions_peaks.get(aid, 0.0)
                    if peak > best_peak:
                        best_peak = peak
                        best = p
                if best:
                    result = best["session"]
                    self._session_cache = result
                    self._session_cache_time = current_time
                    return result
            except Exception as e:
                # if pycaw logic fails, fall back to simple heuristic
                print(f"[select_best_session] pycaw selection failed: {e}")

        # Fallback heuristic: choose the session with the longest title (heuristic to avoid empty/system entries)
        playing_sorted = sorted(playing, key=lambda x: len(x["title"]), reverse=True)
        result = playing_sorted[0]["session"]
        self._session_cache = result
        self._session_cache_time = current_time
        return result

    def _get_process_peaks_by_appid(self, appid_list):
        """
        Best-effort mapping using pycaw:
        - Get all audio sessions via AudioUtilities
        - For each session, get the process name if present, compare with appid fragments
        - Return dict: appid_lower -> peak_value (0.0-1.0)
        This is fuzzy: appid strings and process names may not align perfectly; we try substring matches.
        """
        out = {}
        if not PYCAW_AVAILABLE:
            return out

        sessions = AudioUtilities.GetAllSessions()
        # First, create a mapping from lowercase process name to peak
        proc_peak = {}
        for s in sessions:
            try:
                p = s.Process
                if not p:
                    continue
                name = p.name().lower()
                # get peak meter if available; AudioMeterInformation only exists on certain sessions
                meter = None
                try:
                    meter = s._ctl.QueryInterface  # attempt to access; not guaranteed across versions
                except Exception:
                    meter = None
                # Use simple heuristic: if s._ctl has GetPeakValue method (AudioMeterInformation)
                peak = 0.0
                try:
                    audio_meter = s._ctl.QueryInterface  # legacy; library internals vary
                    # Many pycaw builds expose ._ctl and ._ctl.GetPeakValue - we'll attempt safe access
                    if hasattr(s._ctl, "GetPeakValue"):
                        pval = s._ctl.GetPeakValue()
                        peak = float(pval)
                except Exception:
                    # Try alternate approach: some pycaw AudioSession has SimpleAudioVolume or other attributes
                    peak = 0.0
                proc_peak[name] = max(proc_peak.get(name, 0.0), peak)
            except Exception:
                continue

        # Map appids to process peaks by substring matching
        for aid in appid_list:
            al = (aid or "").lower()
            best_peak = 0.0
            for proc_name, peak in proc_peak.items():
                if proc_name in al or al in proc_name or proc_name.split(".")[0] in al:
                    if peak > best_peak:
                        best_peak = peak
            out[al] = best_peak

        return out
    
    def handle_auth_request(self, product_key="", popup=False):
        """
        Handle authentication request from the UI.
        The authenticate method now prepares all content before showing popup
        to eliminate loading delays in the popup content.
        """
        try:
            self.authenticate(product_key=product_key, popup=popup)
        except Exception as e:
            print(f"Error during authentication request: {e}")
            self.signal_bus.auth_response.emit({
                "status": "Error", 
                "message": f"Authentication failed: {str(e)}", 
                "close": False
            })

    def get_machine_guid(self):
        try:
            cmd = 'reg query HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Cryptography /v MachineGuid'
            output = subprocess.check_output(cmd, shell=True, text=True)
            for line in output.splitlines():
                if "MachineGuid" in line:
                    return line.split()[-1].strip()
        except Exception:
            return None

    
    def authenticate(self, popup=True, product_key=''):
        self.AUTHENTICATED = True
        return

    async def start(self):
        self.authenticate(False)
        while True:
            if self.AUTHENTICATED:
                self.signal_bus.update_ui.emit({}, True, False)
                # Instead of manager.get_current_session(), ask select_best_session
                session = await self.select_best_session()
                if not session:
                    # No session available right now — wait and retry
                    await asyncio.sleep(1)
                    continue

                current_song = None
                self.last_position = 0
                self.last_update_time = time.time()
                lyrics = None
                last_send_time = None
                latest_lyric = None
                try:
                    while True:
                        if not self.AUTHENTICATED:
                            break
                        now = time.time()
                        song_data = await self.get_now_playing(session)

                        # If session object becomes invalid (closed), break and reselect
                        if not song_data or song_data["status"] == "Closed":
                            self.signal_bus.update_ui.emit({}, True, False)
                            break

                        # Check for exclusions
                        if self.is_program_excluded(song_data.get("software", "")):
                            self.signal_bus.update_ui.emit({}, True, False)
                            await asyncio.sleep(1)
                            break  # reselect session instead of continue, since excluded

                        title = song_data["title"]
                        position_sec = song_data["position_sec"]
                        is_playing = song_data["is_playing"]

                        if not title or title.strip() == "":
                            self.signal_bus.update_ui.emit({}, False, False)
                            break

                        # Only re-evaluate best session periodically or when current session fails
                        # This reduces API calls and improves performance
                        session_check_interval = 10  # Check every 10 seconds instead of every loop
                        if not hasattr(self, 'last_session_check'):
                            self.last_session_check = now
                        
                        if now - self.last_session_check > session_check_interval:
                            self.last_session_check = now
                            best_session = await self.select_best_session()
                            if best_session and best_session.source_app_user_model_id != session.source_app_user_model_id:
                                # Switch to the newly selected session
                                session = best_session
                                song_data = await self.get_now_playing(session)
                                # reset some playback tracking
                                current_song = None
                                lyrics = None
                                last_send_time = None
                                latest_lyric = None

                        # Song change detection and lyric retrieval (same as before)
                        if title != current_song:
                            current_song = title
                            self.last_position = position_sec
                            self.last_send_time = position_sec
                            self.last_update_time = time.time()
                            print(f"\nNow playing: {title} - {song_data['artist']}")
                            print(f"Software: {song_data['software']}")
                            print(f"Duration: {song_data['duration_sec']} seconds")
                            json_path = os.path.join(self.appdata_location, "lyrics", f"{self.sanitize_filenames(title)}.json")
                            if not os.path.exists(json_path):
                                # Show "Getting lyrics..." in the lyrics field
                                loading_data = song_data.copy()
                                loading_data['lyrics'] = "Getting lyrics..."
                                self.signal_bus.update_ui.emit(loading_data, False, False)
                            lyrics = self.download_lyrics(title + f"- {song_data['artist']}", json_path)
                            # Show empty lyric while waiting for first lyric to appear
                            empty_data = song_data.copy()
                            empty_data['lyrics'] = " "
                            self.signal_bus.update_ui.emit(empty_data, False, False)

                        # position updates
                        if last_send_time != song_data["position_sec"]:
                            self.last_position = song_data["position_sec"]
                            last_send_time = song_data["position_sec"]

                        elapsed = now - self.last_update_time
                        if is_playing:
                            self.last_position += elapsed

                        self.last_update_time = now

                        current_lyric = self.get_lyric_at_time(lyrics, self.last_position)
                        if current_lyric != latest_lyric:
                            # Update song data with current lyric and send
                            display_data = song_data.copy()
                            display_data['lyrics'] = current_lyric
                            display_data['position_sec'] = self.last_position
                            self.signal_bus.update_ui.emit(display_data, False, False)
                            latest_lyric = current_lyric

                        minutes, seconds = divmod(self.last_position, 60)
                        print(f"\rCurrent playback position: {int(minutes):02d}:{int(seconds):02d} ({self.last_position:.2f}s)", end="")

                        # Increased sleep to reduce CPU usage - lyrics don't need millisecond precision
                        await asyncio.sleep(0.5)

                except Exception as e:
                    self.signal_bus.update_ui.emit({}, True, False)
                    print(f"\nError occurred: {e}")
                    print("Restarting session wait...")
