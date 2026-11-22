"""Assemblage des trames HTTP_JSON, avec un tampon par connexion."""
import json


class MessageParser:
    START = b"<HTTP_JSON>"
    END = b"</HTTP_JSON>"
    MAX_FRAME_BYTES = 1024 * 1024

    def __init__(self):
        self.buffers = {}

    def clear(self):
        self.buffers.clear()

    def remove_source(self, source):
        self.buffers.pop(source, None)

    def feed(self, source, payload):
        buffer = self.buffers.get(source, b"") + payload
        messages, errors = [], []
        while True:
            start = buffer.find(self.START)
            if start < 0:
                buffer = buffer[-(len(self.START) - 1):]
                break
            buffer = buffer[start:]
            end = buffer.find(self.END, len(self.START))
            nested = buffer.find(self.START, len(self.START))
            if nested >= 0 and (end < 0 or nested < end):
                errors.append("Trame interrompue : resynchronisation")
                buffer = buffer[nested:]
                continue
            if end < 0:
                if len(buffer) > self.MAX_FRAME_BYTES:
                    errors.append("Trame incomplète trop volumineuse")
                    buffer = b""
                break
            body = buffer[len(self.START):end]
            buffer = buffer[end + len(self.END):]
            if len(body) > self.MAX_FRAME_BYTES:
                errors.append("Trame trop volumineuse")
                continue
            try:
                message = json.loads(body.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("Un objet JSON est attendu")
                messages.append(message)
            except (UnicodeDecodeError, ValueError) as exc:
                errors.append(f"JSON ignoré : {exc}")
        if buffer:
            self.buffers[source] = buffer
        else:
            self.buffers.pop(source, None)
        return messages, errors
