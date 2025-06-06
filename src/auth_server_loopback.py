import functools
import html
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Literal, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

#: Handler


class DataAuth:
    def __init__(self, code: str, state: str):
        self.code = code
        self.state = state


class ErrorServerLoopback:
    def __init__(self, message: str):
        self._tag: Literal["ErrorServerLoopback"] = "ErrorServerLoopback"
        self.message = message


class SuccessCallback:
    def __init__(self, data_auth: DataAuth):
        self._tag: Literal["Success"] = "Success"
        self.data_auth = data_auth


ResultCallback = Union[SuccessCallback, ErrorServerLoopback]


class _Handler(BaseHTTPRequestHandler):
    """Handles the OAuth callback, captures code and state, then signals server to stop."""

    def __init__(
        self,
        request: socket.socket,
        client_address: Tuple[str, int],
        server: HTTPServer,
        callback: Callable[[ResultCallback], None],
    ):
        self._callback = callback
        super().__init__(request, client_address, server)

    def _send_response_html(self, title: str, message: str) -> None:
        """Sends a formatted HTML response to the client."""
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        html_template = """
        <html>
            <head>
                <meta charset='utf-8'>
                <title>{title}</title>
                <style>
                    body {{ font-family: sans-serif; margin: 20px; }}
                </style>
            </head>
            <body>
                <h1>{title}</h1>
                <p>{message}</p>
                <p>You can close this window and return to Anki.</p>
            </body>
        </html>
        """
        final_html = html_template.format(
            title=html.escape(title), message=html.escape(message)
        )
        self.wfile.write(final_html.encode("utf-8"))

    def do_GET(self) -> None:
        path_parsed = urlparse(self.path)
        params_query = parse_qs(path_parsed.query)

        code = params_query.get("code", [None])[0]
        state = params_query.get("state", [None])[0]
        error = params_query.get("error", [None])[0]

        if isinstance(error, str):
            self._send_response_html(
                "Invalid Request",
                f"Something went wrong. {error}",
            )
            return self._callback(ErrorServerLoopback(message=error))

        if not isinstance(code, str) or not isinstance(state, str):
            self._send_response_html(
                "Invalid Request",
                "The request was invalid. Code and state parameters must be strings.",
            )
            return self._callback(
                ErrorServerLoopback(message="Invalid code and state parameters")
            )

        self._send_response_html(
            "Authentication Successful!", "Your authentication was successful."
        )
        return self._callback(
            SuccessCallback(data_auth=DataAuth(code=code, state=state))
        )

    # Suppress log messages to keep the console clean
    def log_message(self, format: str, *args: Any) -> None:
        return


#: ServerLoopback


class StateStarted:
    def __init__(self, server_http: HTTPServer):
        self._tag: Literal["Started"] = "Started"
        self.server_http = server_http


class StateListening:
    def __init__(
        self,
        server_http: HTTPServer,
        thread_server_http: threading.Thread,
        event_done: threading.Event,
        result_callback: Optional[ResultCallback],
    ):
        self._tag: Literal["Listening"] = "Listening"
        self.server_http = server_http
        self.thread_server_http = thread_server_http
        self.event_done = event_done
        self.result_callback = result_callback


class StateClosed:
    def __init__(self):
        self._tag: Literal["Closed"] = "Closed"


ServerState = Union[StateStarted, StateListening, StateClosed]


class SuccessListen:
    def __init__(self, data_auth: DataAuth):
        self._tag: Literal["Success"] = "Success"
        self.data_auth = data_auth


ResultListen = Union[SuccessListen, ErrorServerLoopback]


class ServerLoopback:
    """A local HTTP server implementation for handling OAuth authentication callbacks.

    This class creates a temporary HTTP server on localhost with a dynamically assigned port
    to handle OAuth 2.0 authentication callbacks.

    Attributes:
        uri_redirect (str): The complete redirect URI (e.g., 'http://127.0.0.1:{port}/callback')
                          that should be used in the OAuth authorization request.

    The server goes through three states:
        - Started: Initial state after instantiation, server is created but not listening
        - Listening: Server is actively listening for the callback
        - Closed: Server has been shut down and can no longer be used
    """

    uri_redirect: str
    _state: ServerState

    def __init__(self):
        def callback(result_callback: ResultCallback) -> None:
            if self._state._tag != "Listening":
                # Silently ignore callbacks in non-listening state
                return
            self._state.result_callback = result_callback
            self._state.event_done.set()

        try:
            server_http = HTTPServer(
                # We set the port to 0 to assign a free port
                ("127.0.0.1", 0),
                functools.partial(
                    _Handler,
                    callback=callback,
                ),
            )
        except Exception as e:
            self._state = StateClosed()
            raise RuntimeError(f"Failed to initialize HTTP server: {e}") from e

        self.uri_redirect = f"http://127.0.0.1:{server_http.server_port}/callback"
        self._state = StateStarted(server_http=server_http)

    def listen(self, timeout: Optional[float] = 120.0) -> ResultListen:
        if self._state._tag != "Started":
            raise RuntimeError(f"Invalid state: {self._state._tag}")

        thread_server_http = threading.Thread(
            target=self._state.server_http.serve_forever, daemon=True
        )
        event_done = threading.Event()

        self._state = StateListening(
            server_http=self._state.server_http,
            thread_server_http=thread_server_http,
            event_done=event_done,
            result_callback=None,
        )

        try:
            thread_server_http.start()
            # Block until `event_done` is set
            if not event_done.wait(timeout):
                return ErrorServerLoopback(
                    message="Timed out waiting for authentication callback"
                )

            if self._state.result_callback is None:
                # We get here is `listen` is cancelled
                return ErrorServerLoopback(message="Authentication data not set")

            if self._state.result_callback._tag == "Success":
                return SuccessListen(data_auth=self._state.result_callback.data_auth)
            else:
                return self._state.result_callback
        finally:
            self.close()

    def close(self):
        try:
            if self._state._tag == "Started":
                self._state.server_http.shutdown()
                self._state.server_http.server_close()

            if self._state._tag == "Listening":
                self._state.event_done.set()
                self._state.thread_server_http.join(timeout=2.0)
                self._state.server_http.shutdown()
                self._state.server_http.server_close()

        finally:
            self._state = StateClosed()
