from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Limits:
    """
    Ограничения сервера. None — без ограничения.

    max_header_size    — URL + все заголовки, байт. Превышение → 431.
    max_body_size      — тело запроса, байт. Превышение → 413 (по Content-Length
                         ещё до чтения тела, для chunked — по мере поступления).
    read_timeout       — секунд от первого байта запроса до полного запроса
                         (защита от slowloris). Истёк → 408.
    keepalive_timeout  — секунд простоя keep-alive соединения. Истёк → закрываем молча.
    handler_timeout    — секунд на выполнение хендлера (с middleware). Истёк → 503.
    """

    max_header_size: Optional[int] = 64 * 1024
    max_body_size: Optional[int] = 1024 * 1024
    read_timeout: Optional[float] = 30.0
    keepalive_timeout: Optional[float] = 75.0
    handler_timeout: Optional[float] = 30.0
