import socket
import threading
import time

import server


class UvicornTestServer:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.server_address = self.socket.getsockname()
        config = server.uvicorn.Config(
            server.app,
            log_level="critical",
            lifespan="on",
        )
        self.server = server.uvicorn.Server(config)

    def start(self):
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while (
            not self.server.started
            and self.thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not self.server.started:
            self.shutdown()
            self.server_close()
            raise RuntimeError("FastAPI test server did not start.")
        return self.thread

    def serve(self):
        self.server.run(sockets=[self.socket])

    def shutdown(self):
        self.server.should_exit = True
        if hasattr(self, "thread") and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def server_close(self):
        self.socket.close()
