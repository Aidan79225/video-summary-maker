"""事實查核的錯誤。分成「這一則查不到」與「整篇做不下去」兩類。"""


class SourceUnavailable(Exception):
    """官方資料這次拿不到。只讓那一則變成無法查證，整篇不失敗。"""


class ModelUnavailable(Exception):
    """模型連不上或回了錯誤。整篇工作失敗，下次排程重試。"""


class ModelOutputInvalid(Exception):
    """模型的回應不是約定的 JSON。"""


class OperationCancelled(Exception):
    """使用者或服務要求停止。"""
