# Enhanced main.py with better Windows integration
import sys
import asyncio
import threading
import os
import shutil
from pathlib import Path
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import Qt
from signal_bus import SignalBus
from controller import Controller
from main_window import MainWindow


def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and PyInstaller """
    if getattr(sys, 'frozen', False):
        # PyInstaller
        base_path = Path(sys._MEIPASS)
    else:
        # Dev environment
        base_path = Path(__file__).parent
    return base_path / relative_path

def setup_windows_taskbar():
    """Set up Windows-specific taskbar integration"""
    if sys.platform == "win32":
        try:
            import ctypes
            
            # Set the app user model ID so Windows groups all windows together
            app_id = "Lyricsplash.MusicApp.1.0"  # Use a unique identifier for your app
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
            
            print(f"✅ Windows App User Model ID set: {app_id}")
            
        except Exception as e:
            print(f"⚠️ Could not set Windows App User Model ID: {e}")

def run_controller_in_thread(controller):
    """Run the async controller in a separate thread with its own event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(controller.start())
    finally:
        loop.close()

if __name__ == "__main__":
    APP_NAME = "Lyricsplash"
    APPDATA_LOCATION = Path(os.getenv("APPDATA")) / APP_NAME
    APPDATA_LOCATION.mkdir(parents=True, exist_ok=True)

    SERVICE_NAME = "ZgMjar~gA)myvz_nW4P2}QHG~6%5qjBZ:MQiBn9,Ni45^9+}9+~x_1LX+:Zy4sJD+JDFiuUm2rtZ479]_^_EKM#NnUca=v^5N8vs"
    SERVICE_USERNAME = "iRM6:op]YeVpP1sWH@k5]V6Qag89mx5RBJsPn*ERakjP4^d?Kx0Jug*ce*z:=@_pcK.x*}]u4P}Vn)tG1WEgY0ibYaUv*VDi_2fU"

    # Create the application
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    # Set up Windows taskbar integration FIRST
    setup_windows_taskbar()
    
    # Set the application icon globally
    icon_path = str(resource_path("icon.ico"))
    if os.path.exists(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)
        print(f"✅ Application icon set: {icon_path}")
    else:
        print(f"⚠️ Icon file not found: {icon_path}")

    # Set application properties that help with taskbar grouping
    app.setApplicationName("Lyricsplash")
    app.setApplicationDisplayName("Lyricsplash")
    app.setApplicationVersion("1.0")
    app.setOrganizationName("Lyricsplash")
    
    # Create signal bus and windows
    signals = SignalBus()
    window = MainWindow(signal_bus=signals, appdata_location=APPDATA_LOCATION, service_name=SERVICE_NAME, service_username=SERVICE_USERNAME)
    controller = Controller(signals, main_window=window, appdata_location=APPDATA_LOCATION, service_name=SERVICE_NAME, service_username=SERVICE_USERNAME)

    # Set up and show the window
    main_win = window.window()  # Initialize the window
    
    # Ensure main window also has the icon
    if os.path.exists(icon_path):
        main_win.setWindowIcon(QIcon(icon_path))
    
    window.start()   # Show the window
    
    # Run controller in a separate thread with its own asyncio event loop
    controller_thread = threading.Thread(target=run_controller_in_thread, args=(controller,), daemon=True)
    controller_thread.start()

    try:
        sys.exit(app.exec_())
    finally:
        # Cleanup if needed
        pass