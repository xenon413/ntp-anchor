from abc import ABC, abstractmethod

class BaseTimeSync(ABC):
    @staticmethod
    @abstractmethod
    def start_time_service()->None:...

    @staticmethod
    @abstractmethod
    def sync_time()->None:...
