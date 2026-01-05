# signal_bus.py
from PyQt5.QtCore import QObject, pyqtSignal

class SignalBus(QObject):
    update_ui = pyqtSignal(dict, bool, bool)  # song_data dict, standby
    request_action = pyqtSignal(str, bool)   
    auth_response = pyqtSignal(object)